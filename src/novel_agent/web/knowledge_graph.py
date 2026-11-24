"""Novel-scoped graph HTTP adapter. All query semantics live in the corpus package."""
from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from novel_agent.corpus.knowledge_graph.models import BuildRequest, GraphError, MergeEntitiesRequest, RevisionRequest


def graph_router(service):
    router = APIRouter(prefix="/api/novels/{novel_id}/graph", tags=["knowledge-graph"])

    def call(operation):
        try:
            return operation()
        except KeyError as exc:
            raise GraphError("novel_not_found", "小说不存在。", 404) from exc
        except sqlite3.Error as exc:
            raise GraphError("graph_storage_unavailable", "图谱存储不可用，原文检索仍可使用。", 503) from exc

    def reader(novel_id, version):
        return call(lambda: service.reader(novel_id, version))

    def scope(novel_id, start, end, entity_types=None, relation_types=None):
        def chapter(value):
            if value is None:
                return None
            if not value.startswith(novel_id + ":"):
                raise GraphError("invalid_chapter", "章节不属于当前小说。", 400)
            return value[len(novel_id) + 1:]
        result = {"start_section_id": chapter(start), "end_section_id": chapter(end)}
        if entity_types is not None:
            result["entity_types"] = entity_types
        if relation_types is not None:
            result["relation_types"] = relation_types
        return result

    @router.get("/status")
    def status(novel_id: str):
        return call(lambda: service.status(novel_id))

    @router.post("/builds")
    def build(novel_id: str, body: BuildRequest):
        payload, status_code = call(lambda: service.build(novel_id, body.mode, body.job_id))
        return JSONResponse(payload, status_code=status_code)

    @router.post("/builds/{job_id}/stop")
    def stop(novel_id: str, job_id: str):
        return call(lambda: service.stop(novel_id, job_id))

    @router.get("/history")
    def history(novel_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        return call(lambda: service.history(novel_id, offset, limit))

    @router.post("/merges/preview")
    def preview_merge(novel_id: str, body: MergeEntitiesRequest):
        return call(lambda: service.merge_entities(novel_id, body, preview=True))

    @router.post("/merges")
    def merge(novel_id: str, body: MergeEntitiesRequest):
        return call(lambda: service.merge_entities(novel_id, body))

    @router.post("/history/{target_version}/restore")
    def restore(novel_id: str, target_version: str, body: RevisionRequest):
        return call(lambda: service.restore_revision(novel_id, body.version, target_version))

    @router.get("/corrections")
    def corrections(novel_id: str, version: str = Query(min_length=1, max_length=64)):
        return call(lambda: service.manual_corrections(novel_id, version))

    @router.post("/corrections/{correction_id}/skip")
    def skip(novel_id: str, correction_id: str, body: RevisionRequest):
        return call(lambda: service.skip_correction(novel_id, body.version, correction_id))

    @router.get("/entities")
    def entities(novel_id: str, version: str = Query(min_length=1, max_length=64),
                 q: str = Query(min_length=1, max_length=200), type: str | None = None,
                 offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                 startChapterId: str | None = None, endChapterId: str | None = None):
        return call(lambda: reader(novel_id, version).search(q, type, offset, limit, **scope(novel_id, startChapterId, endChapterId)))

    @router.get("/unresolved")
    def unresolved(novel_id: str, version: str = Query(min_length=1, max_length=64),
                   offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                   startChapterId: str | None = None, endChapterId: str | None = None):
        return call(lambda: reader(novel_id, version).unresolved(offset, limit, **scope(novel_id, startChapterId, endChapterId)))

    @router.get("/entities/{entity_id}")
    def entity(novel_id: str, entity_id: str, version: str = Query(min_length=1, max_length=64),
               offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
               startChapterId: str | None = None, endChapterId: str | None = None):
        return call(lambda: reader(novel_id, version).entity(entity_id, offset, limit, **scope(novel_id, startChapterId, endChapterId)))

    @router.get("/neighbors")
    def neighbors(novel_id: str, entity_id: str, version: str = Query(min_length=1, max_length=64),
                  depth: int = Query(1, ge=1, le=3),
                  entity_type: Annotated[list[str] | None, Query()] = None,
                  relation_type: Annotated[list[str] | None, Query()] = None,
                  startChapterId: str | None = None, endChapterId: str | None = None):
        return call(lambda: reader(novel_id, version).neighbors(entity_id, depth,
            **scope(novel_id, startChapterId, endChapterId, entity_type or [], relation_type or [])))

    @router.get("/paths")
    def paths(novel_id: str, from_id: str, to_id: str, version: str = Query(min_length=1, max_length=64),
              max_hops: int = Query(3, ge=1, le=3), limit: int = Query(5, ge=1, le=5),
              entity_type: Annotated[list[str] | None, Query()] = None,
              relation_type: Annotated[list[str] | None, Query()] = None,
              startChapterId: str | None = None, endChapterId: str | None = None):
        return call(lambda: reader(novel_id, version).paths(from_id, to_id, max_hops, limit,
            **scope(novel_id, startChapterId, endChapterId, entity_type or [], relation_type or [])))

    @router.get("/relations/{relation_id}/evidence")
    def evidence(novel_id: str, relation_id: str, version: str = Query(min_length=1, max_length=64),
                 offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                 startChapterId: str | None = None, endChapterId: str | None = None):
        return call(lambda: reader(novel_id, version).evidence(relation_id, offset, limit, **scope(novel_id, startChapterId, endChapterId)))

    return router
