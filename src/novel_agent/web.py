from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from novel_agent.config import ModelConfig, default_max_steps
from novel_agent.repository import NovelCorpus
from novel_agent.service import AgentExecutionResult, execute_agent, load_corpus
from novel_agent.tracing import run_metrics

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
NODE_LABELS = {
    "planner": "制定调查计划",
    "researcher": "选择检索动作",
    "tools": "读取小说原文",
    "observe": "校验并整理证据",
    "checker": "检查证据缺口",
    "replanner": "调整调查计划",
    "writer": "生成最终解读",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan": state.get("plan", []),
        "current_task_id": state.get("current_task_id"),
        "evidence": state.get("evidence", []),
        "hypotheses": state.get("hypotheses", []),
        "unresolved_questions": state.get("unresolved_questions", []),
        "suggested_queries": state.get("suggested_queries", []),
        "review": state.get("review"),
        "step_count": state.get("step_count", 0),
        "max_steps": state.get("max_steps", 0),
        "replan_count": state.get("replan_count", 0),
        "termination_reason": state.get("termination_reason"),
        "final_answer": state.get("final_answer"),
        "limitations": state.get("limitations", []),
        "metrics": run_metrics(state),
    }


def _event_detail(node: str, update: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {"label": NODE_LABELS.get(node, node)}
    if node == "planner":
        detail["task_count"] = len(snapshot["plan"])
    elif node == "researcher":
        calls: list[dict[str, Any]] = []
        for message in update.get("messages") or []:
            for call in getattr(message, "tool_calls", None) or []:
                calls.append({"name": call.get("name"), "args": call.get("args", {})})
        detail["tool_calls"] = calls
    elif node == "tools":
        detail["tools"] = [
            getattr(message, "name", None)
            for message in update.get("messages") or []
            if getattr(message, "name", None)
        ]
    elif node == "observe":
        detail["evidence_count"] = len(snapshot["evidence"])
    elif node == "checker":
        review = snapshot.get("review") or {}
        detail["sufficient"] = review.get("sufficient")
        detail["rationale"] = review.get("rationale", "")
    elif node == "replanner":
        detail["replan_count"] = snapshot["replan_count"]
    elif node == "writer":
        detail["answer_ready"] = bool(snapshot.get("final_answer"))
    return detail


@dataclass
class StoredNovel:
    novel_id: str
    filename: str
    path: Path
    corpus: NovelCorpus
    elapsed_seconds: float


class NovelStore:
    def __init__(self, upload_limit: int = MAX_UPLOAD_BYTES):
        self.upload_limit = upload_limit
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="novel-agent-")
        self.root = Path(self._temporary_directory.name)
        self._items: dict[str, StoredNovel] = {}

    def close(self) -> None:
        self._items.clear()
        self._temporary_directory.cleanup()

    async def add(self, upload: UploadFile) -> StoredNovel:
        filename = Path(upload.filename or "").name
        if Path(filename).suffix.lower() != ".txt":
            raise ValueError("仅支持 .txt 小说文件。")

        novel_id = uuid.uuid4().hex[:12]
        destination = self.root / f"{novel_id}.txt"
        total = 0
        try:
            with destination.open("wb") as target:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.upload_limit:
                        raise ValueError("文件不能超过 50 MiB。")
                    target.write(chunk)
            if total == 0:
                raise ValueError("上传文件不能为空。")
            loaded = await asyncio.to_thread(load_corpus, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

        item = StoredNovel(
            novel_id=novel_id,
            filename=filename,
            path=destination,
            corpus=loaded.corpus,
            elapsed_seconds=loaded.elapsed_seconds,
        )
        self._items[novel_id] = item
        return item

    def get(self, novel_id: str) -> StoredNovel:
        try:
            return self._items[novel_id]
        except KeyError as exc:
            raise KeyError("小说不存在或服务已重启，请重新上传。") from exc


@dataclass
class RunRecord:
    run_id: str
    novel_id: str
    question: str
    max_steps: int
    status: str = "queued"
    events: list[dict[str, Any]] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)

    def emit(
        self,
        event_type: str,
        *,
        node: str | None = None,
        detail: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.condition:
            if snapshot is not None:
                self.snapshot = snapshot
            event = {
                "sequence": len(self.events) + 1,
                "timestamp": _utc_now(),
                "type": event_type,
                "status": self.status,
                "node": node,
                "detail": detail or {},
                "snapshot": self.snapshot,
                "error": error,
            }
            self.events.append(event)
            self.condition.notify_all()

    def event_at(self, index: int, timeout: float = 15.0) -> dict[str, Any] | None:
        with self.condition:
            if index >= len(self.events) and self.status not in TERMINAL_STATUSES:
                self.condition.wait(timeout=timeout)
            return self.events[index] if index < len(self.events) else None


Executor = Callable[..., AgentExecutionResult]


class RunManager:
    def __init__(
        self,
        *,
        executor: Executor = execute_agent,
        model_factory: Callable[[], Any] | None = None,
    ):
        self.executor = executor
        self.model_factory = model_factory
        self._records: dict[str, RunRecord] = {}
        self._active_run_id: str | None = None
        self._lock = threading.Lock()

    def start(self, novel: StoredNovel, question: str, max_steps: int) -> RunRecord:
        with self._lock:
            if self._active_run_id:
                active = self._records[self._active_run_id]
                if active.status not in TERMINAL_STATUSES:
                    raise RuntimeError("已有 Agent 任务正在运行。")
            run_id = uuid.uuid4().hex[:12]
            record = RunRecord(
                run_id=run_id,
                novel_id=novel.novel_id,
                question=question,
                max_steps=max_steps,
            )
            self._records[run_id] = record
            self._active_run_id = run_id

        record.emit("status", detail={"label": "任务已进入队列"})
        worker = threading.Thread(
            target=self._run,
            args=(record, novel),
            daemon=True,
            name=f"novel-agent-{run_id}",
        )
        worker.start()
        return record

    def _run(self, record: RunRecord, novel: StoredNovel) -> None:
        record.status = "running"
        record.emit("status", detail={"label": "Agent 开始运行"})

        def on_update(node: str, update: dict[str, Any], state: dict[str, Any]) -> None:
            snapshot = _public_snapshot(state)
            record.emit(
                "update",
                node=node,
                detail=_event_detail(node, update, snapshot),
                snapshot=snapshot,
            )

        try:
            kwargs: dict[str, Any] = {}
            if self.model_factory is not None:
                kwargs["model"] = self.model_factory()
            result = self.executor(
                novel.corpus,
                record.question,
                max_steps=record.max_steps,
                thread_id=record.run_id,
                trace_path=Path("output/traces") / f"{record.run_id}.jsonl",
                on_update=on_update,
                should_cancel=lambda: record.cancel_requested,
                **kwargs,
            )
            snapshot = _public_snapshot(result.state)
            if result.cancelled:
                record.status = "cancelled"
                record.emit(
                    "cancelled",
                    detail={"label": "任务已在节点边界停止"},
                    snapshot=snapshot,
                )
            else:
                record.status = "completed"
                record.emit(
                    "complete",
                    detail={"label": "分析完成"},
                    snapshot=snapshot,
                )
        except Exception as exc:  # Worker errors are delivered through the event stream.
            record.status = "failed"
            record.emit(
                "error",
                detail={"label": "任务执行失败"},
                error=str(exc),
            )
        finally:
            with self._lock:
                if self._active_run_id == record.run_id:
                    self._active_run_id = None

    def get(self, run_id: str) -> RunRecord:
        try:
            return self._records[run_id]
        except KeyError as exc:
            raise KeyError("任务不存在或服务已重启。") from exc

    def cancel(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        with record.condition:
            if record.status not in TERMINAL_STATUSES:
                record.cancel_requested = True
                record.condition.notify_all()
        return record


class RunRequest(BaseModel):
    novel_id: str
    question: str = Field(min_length=1, max_length=4000)
    max_steps: int = Field(
        default_factory=lambda: min(max(default_max_steps(), 1), 100),
        ge=1,
        le=100,
    )


def _novel_info(item: StoredNovel) -> dict[str, Any]:
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
) -> FastAPI:
    store = NovelStore(upload_limit=upload_limit)
    runs = RunManager(executor=executor, model_factory=model_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        store.close()

    app = FastAPI(title="Novel Agent Web", version="0.2.0", lifespan=lifespan)
    app.state.novels = store
    app.state.runs = runs

    @app.get("/api/config")
    def config_status() -> dict[str, Any]:
        try:
            config = ModelConfig.from_env()
            return {
                "ready": True,
                "model_name": config.model_name,
                "default_max_steps": min(max(default_max_steps(), 1), 100),
                "error": None,
            }
        except (RuntimeError, ValueError) as exc:
            return {
                "ready": False,
                "model_name": os.getenv("MODEL_NAME", "gpt-4.1-mini"),
                "default_max_steps": min(max(default_max_steps(), 1), 100),
                "error": str(exc),
            }

    @app.post("/api/novels", status_code=201)
    async def upload_novel(file: UploadFile = File(...)) -> dict[str, Any]:
        try:
            return _novel_info(await store.add(file))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
            "items": sections[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "total": len(sections),
        }

    @app.get("/api/novels/{novel_id}/search")
    def search_novel(
        novel_id: str,
        q: str = Query(min_length=1, max_length=200),
        top_k: int = Query(5, ge=1, le=20),
    ) -> list[dict[str, Any]]:
        try:
            corpus = store.get(novel_id).corpus
            return [item.model_dump() for item in corpus.search(q, top_k)]
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
                    yield f"id: {event['sequence']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
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

    project_root = Path(__file__).resolve().parents[2]
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


if __name__ == "__main__":
    main()
