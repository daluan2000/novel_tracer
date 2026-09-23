from __future__ import annotations

import json

import pytest

from novel_agent.repository import NovelCorpus
from novel_agent.tools import build_tools


@pytest.fixture
def corpus(tmp_path) -> NovelCorpus:
    body = "吕树在庙会上遇到了吕小鱼，两人一起讨论晚饭。" * 40
    later = "吕小鱼发现吕树隐瞒了计划，因此非常生气。" * 40
    path = tmp_path / "sample.txt"
    path.write_text(f"第一章 庙会\n\n{body}\n\n第二章 隐瞒\n\n{later}", encoding="utf-8")
    return NovelCorpus.from_path(path)


def test_search_and_context(corpus: NovelCorpus) -> None:
    hits = corpus.search("吕树 吕小鱼", top_k=3)

    assert hits
    assert {"吕树", "吕小鱼"}.issubset(set(hits[0].matched_terms))
    context = corpus.read_context(hits[0].chunk_id)
    assert context
    assert any("吕小鱼" in chunk.text for chunk in context)


def test_quote_validation(corpus: NovelCorpus) -> None:
    hit = corpus.search("隐瞒", top_k=1)[0]

    assert corpus.validate_quote(hit.chunk_id, "吕树隐瞒了计划")
    assert not corpus.validate_quote(hit.chunk_id, "原文中不存在的句子")


def test_tools_return_structured_success_and_error(corpus: NovelCorpus) -> None:
    tools = {tool.name: tool for tool in build_tools(corpus)}
    success = json.loads(tools["search_novel"].invoke({"keyword": "庙会", "top_k": 2}))
    failure = json.loads(
        tools["read_context"].invoke({"chunk_id": "missing", "before": 1, "after": 1})
    )

    assert success["ok"] is True
    assert success["data"]
    assert failure["ok"] is False
    assert "missing" in failure["error"]


def test_empty_search_is_rejected(corpus: NovelCorpus) -> None:
    with pytest.raises(ValueError, match="不能为空"):
        corpus.search("  ")
