from __future__ import annotations

import json
from collections.abc import Callable

from langchain_core.tools import StructuredTool

from novel_agent.repository import NovelCorpus


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _safe_tool(operation: Callable[[], object]) -> str:
    try:
        return _json(operation())
    except (KeyError, ValueError) as exc:
        return _json({"error": str(exc)})


def _chunk_payload(chunk: object) -> dict[str, object]:
    return {
        "chunk_id": getattr(chunk, "chunk_id"),
        "section_id": getattr(chunk, "section_id"),
        "section_title": getattr(chunk, "section_title"),
        "start_line": getattr(chunk, "start_line"),
        "end_line": getattr(chunk, "end_line"),
        "text": getattr(chunk, "text"),
    }


def build_tools(corpus: NovelCorpus) -> list[StructuredTool]:
    def get_book_structure(section_offset: int = 0, section_limit: int = 0) -> str:
        """查看小说结构总览。默认不返回章节；需要目录时设置 section_limit，最多 20。"""

        def operation() -> dict[str, object]:
            summary = corpus.structure_summary()
            sections = summary.pop("sections")
            offset = max(section_offset, 0)
            limit = min(max(section_limit, 0), 20)
            summary["section_offset"] = offset
            summary["section_limit"] = limit
            if limit:
                summary["sections"] = sections[offset : offset + limit]
            return summary

        return _safe_tool(operation)

    def search_novel(keyword: str, top_k: int = 3) -> str:
        """在小说中搜索关键词，默认返回 3 条，最多 8 条。"""

        return _safe_tool(
            lambda: [
                hit.model_dump()
                for hit in corpus.search(keyword=keyword, top_k=min(max(top_k, 1), 8))
            ]
        )

    def read_context(chunk_id: str, before: int = 0, after: int = 0) -> str:
        """读取命中 Chunk；需要跨边界上下文时可读取前后各 1 个 Chunk。"""

        return _safe_tool(
            lambda: [
                _chunk_payload(chunk)
                for chunk in corpus.read_context(
                    chunk_id,
                    min(max(before, 0), 1),
                    min(max(after, 0), 1),
                )
            ]
        )

    def read_section(section_id: str, start_chunk: int = 0, limit: int = 2) -> str:
        """按范围读取 Section，默认 2 个 Chunk，最多 3 个。"""

        return _safe_tool(
            lambda: [
                _chunk_payload(chunk)
                for chunk in corpus.read_section(
                    section_id,
                    start_chunk=start_chunk,
                    limit=min(max(limit, 1), 3),
                )
            ]
        )

    return [
        StructuredTool.from_function(get_book_structure),
        StructuredTool.from_function(search_novel),
        StructuredTool.from_function(read_context),
        StructuredTool.from_function(read_section),
    ]
