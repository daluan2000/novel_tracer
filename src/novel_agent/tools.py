from __future__ import annotations

import json
from collections.abc import Callable

from langchain_core.tools import StructuredTool

from novel_agent.repository import NovelCorpus


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _safe_tool(operation: Callable[[], object]) -> str:
    """把工具结果统一编码成 JSON，并将可预期的输入错误返回给模型自行修正。"""

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


# corpus：当前 Agent 要查询的小说语料库。
# 返回值：注册给模型和 ToolNode 的工具列表。
def build_tools(corpus: NovelCorpus) -> list[StructuredTool]:
    """构造 Researcher 可调用的四个只读工具。

    corpus 被闭包捕获，不会暴露给模型；返回值会注册给模型和 ToolNode。
    """

    # section_offset：从第几个章节开始返回，0 表示第一个章节。
    # section_limit：返回多少个章节，0 表示不返回章节列表，最多 20 个。
    # 返回值：包含全书统计和可选章节列表的 JSON 字符串。
    def get_book_structure(section_offset: int = 0, section_limit: int = 0) -> str:
        """查看小说结构总览。默认不返回章节；需要目录时设置 section_limit，最多 20。"""

        def operation() -> dict[str, object]:
            summary = corpus.structure_summary()
            # 完整目录按模型请求分页，避免一次返回过多章节。
            sections = summary.pop("sections")
            offset = max(section_offset, 0)
            limit = min(max(section_limit, 0), 20)
            summary["section_offset"] = offset
            summary["section_limit"] = limit
            if limit:
                summary["sections"] = sections[offset : offset + limit]
            return summary

        return _safe_tool(operation)

    # keyword：要在小说标题和正文中查找的关键词。
    # top_k：最多返回多少条结果，默认 3 条，最多 8 条。
    # 返回值：包含命中位置、得分和正文摘要的 JSON 数组字符串。
    def search_novel(keyword: str, top_k: int = 3) -> str:
        """在小说中搜索关键词，默认返回 3 条，最多 8 条。"""

        return _safe_tool(
            lambda: [
                hit.model_dump()
                for hit in corpus.search(keyword=keyword, top_k=min(max(top_k, 1), 8))
            ]
        )

    # chunk_id：要读取的目标文本块 ID。
    # before：目标块之前读取多少个相邻块，本工具最多读取 1 个。
    # after：目标块之后读取多少个相邻块，本工具最多读取 1 个。
    # 返回值：目标块及前后相邻块完整内容的 JSON 数组字符串。
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

    # section_id：要读取的章节 ID。
    # start_chunk：从章节内第几个文本块开始读取，0 表示第一个文本块。
    # limit：本次读取多少个连续文本块，默认 2 个，最多 3 个。
    # 返回值：该章节中连续文本块完整内容的 JSON 数组字符串。
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

    # 包装成 Tool；注册发生在 graph.py 的 bind_tools 和 ToolNode。
    return [
        StructuredTool.from_function(get_book_structure),
        StructuredTool.from_function(search_novel),
        StructuredTool.from_function(read_context),
        StructuredTool.from_function(read_section),
    ]
