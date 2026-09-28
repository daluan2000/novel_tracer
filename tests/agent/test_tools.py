from __future__ import annotations

import json

from novel_agent.agent.tools import build_tools
from novel_agent.corpus.repository import NovelCorpus


def test_tools_return_structured_success_and_error(corpus: NovelCorpus) -> None:
    tools = {tool.name: tool for tool in build_tools(corpus)}
    success = json.loads(tools["search_novel"].invoke({"keyword": "庙会", "top_k": 2}))
    failure = json.loads(
        tools["read_context"].invoke(
            {"chunk_id": "missing", "before": 1, "after": 1}
        )
    )

    assert success
    assert "snippet" in success[0]
    assert "missing" in failure["error"]


def test_model_tools_bound_and_compact_large_results(corpus: NovelCorpus) -> None:
    tools = {tool.name: tool for tool in build_tools(corpus)}
    hit = corpus.search("吕树", top_k=1)[0]

    context = json.loads(
        tools["read_context"].invoke(
            {"chunk_id": hit.chunk_id, "before": 5, "after": 5}
        )
    )
    section = json.loads(
        tools["read_section"].invoke(
            {"section_id": hit.section_id, "start_chunk": 0, "limit": 5}
        )
    )
    structure = json.loads(tools["get_book_structure"].invoke({}))

    assert len(context) <= 3
    assert len(section) <= 3
    assert set(context[0]) == {
        "chunk_id",
        "section_id",
        "section_title",
        "start_line",
        "end_line",
        "text",
    }
    assert "sections" not in structure
    assert structure["section_limit"] == 0
