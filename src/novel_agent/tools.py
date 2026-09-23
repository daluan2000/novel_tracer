from __future__ import annotations

import json
from collections.abc import Callable

from langchain_core.tools import StructuredTool

from novel_agent.repository import NovelCorpus


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _safe_tool(operation: Callable[[], object]) -> str:
    try:
        return _json({"ok": True, "data": operation(), "error": None})
    except (KeyError, ValueError) as exc:
        return _json({"ok": False, "data": None, "error": str(exc)})


def build_tools(corpus: NovelCorpus) -> list[StructuredTool]:
    def get_book_structure() -> str:
        """查看小说结构、标题识别置信度、Section 和 Chunk 数量。开始调查或需要定位范围时使用。"""

        return _safe_tool(corpus.structure_summary)

    def search_novel(keyword: str, top_k: int = 5) -> str:
        """在小说中搜索关键词。输入简短的人名、地点、事件词或多个空格分隔的关键词。"""

        return _safe_tool(
            lambda: [hit.model_dump() for hit in corpus.search(keyword=keyword, top_k=top_k)]
        )

    def read_context(chunk_id: str, before: int = 1, after: int = 1) -> str:
        """读取命中 Chunk 及其前后相邻 Chunk，用于避免断章取义。before/after 最大为 5。"""

        return _safe_tool(
            lambda: [chunk.model_dump() for chunk in corpus.read_context(chunk_id, before, after)]
        )

    def read_section(section_id: str, start_chunk: int = 0, limit: int = 3) -> str:
        """按范围读取一个 Section。适用于真实章节和无标题文本生成的合成 Section，limit 最大为 5。"""

        return _safe_tool(
            lambda: [
                chunk.model_dump()
                for chunk in corpus.read_section(section_id, start_chunk=start_chunk, limit=limit)
            ]
        )

    return [
        StructuredTool.from_function(get_book_structure),
        StructuredTool.from_function(search_novel),
        StructuredTool.from_function(read_context),
        StructuredTool.from_function(read_section),
    ]
