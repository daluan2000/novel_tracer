from __future__ import annotations

import pytest

from novel_agent.corpus.repository import NovelCorpus


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


def test_empty_search_is_rejected(corpus: NovelCorpus) -> None:
    with pytest.raises(ValueError, match="不能为空"):
        corpus.search("  ")
