"""FastAPI application assembly and HTTP endpoints."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from novel_agent.application.service import execute_agent
from novel_agent.runtime.config import (
    ModelConfig,
    default_max_steps,
    novel_agent_data_dir,
    structured_output_retries,
)
from novel_agent.web.events import TERMINAL_STATUSES
from novel_agent.web.runs import Executor, RunManager
from novel_agent.web.store import MAX_UPLOAD_BYTES, NovelStore, StoredNovel


class RunRequest(BaseModel):
    novel_id: str
    question: str = Field(min_length=1, max_length=4000)
    max_steps: int = Field(
        default_factory=lambda: min(max(default_max_steps(), 1), 100),
        ge=1,
        le=100,
    )


class EmbeddingToggleRequest(BaseModel):
    enabled: bool


def novel_info(item: StoredNovel) -> dict[str, Any]:
    summary = item.corpus.structure_summary()
    return {
        "novel_id": item.novel_id,
        "filename": item.filename,
        "encoding": summary["encoding"],
        "character_count": summary["character_count"],
        "line_count": summary["line_count"],
        "section_count": summary["section_count"],
        "chunk_count": summary["chunk_count"],
        "structure": summary["structure"],
        "elapsed_seconds": round(item.elapsed_seconds, 3),
    }


def create_app(
    *,
    executor: Executor = execute_agent,
    model_factory: Callable[[], Any] | None = None,
    upload_limit: int = MAX_UPLOAD_BYTES,
    data_root: str | Path | None = None,
) -> FastAPI:
    store = NovelStore(
        upload_limit=upload_limit,
        data_root=data_root if data_root is not None else novel_agent_data_dir(),
    )
    runs = RunManager(executor=executor, model_factory=model_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await store.open()
        try:
            yield
        finally:
            await store.close()

    app = FastAPI(title="Novel Agent Web", version="0.2.0", lifespan=lifespan)
    app.state.novels = store
    app.state.runs = runs

    @app.get("/api/config")
    def config_status() -> dict[str, Any]:
        embedding = store.embedding_config()
        try:
            retries = structured_output_retries()
            config = ModelConfig.from_env()
            return {
                "ready": True,
                "model_name": config.model_name,
                "default_max_steps": min(max(default_max_steps(), 1), 100),
                "structured_output_retries": retries,
                "embedding_configured": embedding.enabled,
                "embedding_model": embedding.model_name or None,
                "error": None,
            }
        except (RuntimeError, ValueError) as exc:
            return {
                "ready": False,
                "model_name": os.getenv("MODEL_NAME", "gpt-4.1-mini"),
                "default_max_steps": min(max(default_max_steps(), 1), 100),
                "structured_output_retries": None,
                "embedding_configured": embedding.enabled,
                "embedding_model": embedding.model_name or None,
                "error": str(exc),
            }

    @app.post("/api/novels", status_code=201)
    async def upload_novel(file: UploadFile = File(...)) -> dict[str, Any]:
        try:
            return novel_info(await store.add(file))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/novels")
    def list_novels() -> dict[str, Any]:
        items = store.list()
        return {
            "items": [novel_info(item) for item in items],
            "total": len(items),
        }

    @app.get("/api/novels/{novel_id}/sections")
    def list_sections(
        novel_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        try:
            sections = store.get(novel_id).corpus.structure_summary()["sections"]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        return {
            "items": [
                {
                    **section,
                    "chapter_id": f"{novel_id}:{section['section_id']}",
                }
                for section in sections[offset : offset + limit]
            ],
            "offset": offset,
            "limit": limit,
            "total": len(sections),
        }

    @app.get("/api/novels/{novel_id}/retrieval-status")
    def retrieval_status(novel_id: str) -> dict[str, Any]:
        try:
            return store.get(novel_id).corpus.retrieval_status()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc

    @app.put("/api/novels/{novel_id}/embedding", status_code=202)
    async def set_novel_embedding(
        novel_id: str, request: EmbeddingToggleRequest
    ) -> dict[str, Any]:
        try:
            return await store.set_embedding_enabled(novel_id, request.enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/novels/{novel_id}/search")
    def search_novel(
        novel_id: str,
        q: str = Query(min_length=1, max_length=200),
        top_k: int = Query(5, ge=1, le=20),
        start_section_id: str | None = Query(None, alias="startChapterId"),
        end_section_id: str | None = Query(None, alias="endChapterId"),
    ) -> list[dict[str, Any]]:
        try:
            corpus = store.get(novel_id).corpus
            chapter_prefix = f"{novel_id}:"

            def resolve_chapter_id(chapter_id: str | None, boundary: str) -> str | None:
                if chapter_id is None:
                    return None
                if not chapter_id.startswith(chapter_prefix):
                    raise ValueError(f"{boundary}章节无效或不属于当前小说。")
                section_id = chapter_id.removeprefix(chapter_prefix)
                if not section_id:
                    raise ValueError(f"{boundary}章节无效或不属于当前小说。")
                return section_id

            return [
                item.model_dump()
                for item in corpus.search(
                    q,
                    top_k,
                    start_section_id=resolve_chapter_id(start_section_id, "起始"),
                    end_section_id=resolve_chapter_id(end_section_id, "结束"),
                )
            ]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/novels/{novel_id}/chunks/{chunk_id}/context")
    def read_context(
        novel_id: str,
        chunk_id: str,
        before: int = Query(1, ge=0, le=5),
        after: int = Query(1, ge=0, le=5),
    ) -> list[dict[str, Any]]:
        try:
            chunks = store.get(novel_id).corpus.read_context(chunk_id, before, after)
            return [chunk.model_dump() for chunk in chunks]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc

    @app.post("/api/runs", status_code=202)
    def start_run(request: RunRequest) -> dict[str, Any]:
        question = request.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="问题不能为空。")
        try:
            novel = store.get(request.novel_id)
            record = runs.start(novel, question, request.max_steps)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": record.run_id, "status": record.status}

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str) -> StreamingResponse:
        try:
            record = runs.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc

        async def generate():
            index = 0
            while True:
                event = await asyncio.to_thread(record.event_at, index)
                if event is not None:
                    yield (
                        f"id: {event['sequence']}\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )
                    index += 1
                    continue
                if record.status in TERMINAL_STATUSES:
                    break
                yield ": keep-alive\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    def cancel_run(run_id: str) -> dict[str, Any]:
        try:
            record = runs.cancel(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        return {
            "run_id": record.run_id,
            "status": record.status,
            "cancel_requested": record.cancel_requested,
        }

    @app.post("/api/runs/{run_id}/retry", status_code=202)
    def retry_run(run_id: str) -> dict[str, Any]:
        try:
            existing = runs.get(run_id)
            novel = store.get(existing.novel_id)
            record = runs.retry(run_id, novel)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "run_id": record.run_id,
            "status": record.status,
            "failed_node": record.last_retry_node,
            "manual_retry_count": record.manual_retry_count,
        }

    project_root = Path(__file__).resolve().parents[3]
    frontend_dist = project_root / "frontend" / "dist"
    if frontend_dist.is_dir():
        assets = frontend_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            candidate = (frontend_dist / full_path).resolve()
            if candidate.is_file() and frontend_dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        def frontend_missing() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={"detail": "前端尚未构建；开发时请运行 npm run dev。"},
            )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
