from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from novel_agent.corpus.loader import load_novel
from novel_agent.corpus.retrieval.passages import build_retrieval_passages
from novel_agent.corpus.retrieval.service import RetrievalService


_CHAPTER_ONE = "石猴学艺归来，回到花果山水帘洞，得知混世魔王抢占家园。他找到妖王交战，最终将混世魔王一刀两断。\n\n" * 20
_CHAPTER_TWO = "太白金星奉玉帝旨意来到花果山招安。猴王接受齐天大圣的名号，随后前往天宫任职。\n\n" * 20
_CHAPTER_THREE = "白骨精为了捉拿唐僧，先后三次变化成人接近师徒。孙悟空识破变化，因此有了三打白骨精的故事。\n\n" * 20

NOVEL_TEXT = f"""第一章 水帘洞

{_CHAPTER_ONE}

第二章 招安

{_CHAPTER_TWO}

第三章 尸魔

{_CHAPTER_THREE}
"""


class FakeEmbedding:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.document_calls = 0
        self.query_calls = 0

    @staticmethod
    def _vector(text: str) -> list[float]:
        if any(term in text for term in ("混世魔王", "水帘洞", "最早击败的妖王")):
            return [1.0, 0.0, 0.0]
        if any(term in text for term in ("太白金星", "招安", "天庭使者劝他归顺")):
            return [0.0, 1.0, 0.0]
        if any(term in text for term in ("白骨精", "三打", "尸魔伪装成人")):
            return [0.0, 0.0, 1.0]
        return [1.0, 1.0, 1.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        if self.fail:
            raise RuntimeError("fake provider failure")
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        if self.fail:
            raise RuntimeError("fake provider failure")
        return self._vector(text)


@pytest.fixture
def novel_document(tmp_path):
    path = tmp_path / "retrieval-sample.txt"
    path.write_text(NOVEL_TEXT, encoding="utf-8")
    return load_novel(path)


def _build_hybrid(document, cache_root: Path, provider: FakeEmbedding | None = None):
    service = RetrievalService(document)
    embedding = provider or FakeEmbedding()
    assert service.prepare_dense(model_name="fake-embedding", configured=True)
    assert service.status()["status"] == "building"
    service.build_dense_index(
        embedding,
        model_name="fake-embedding",
        cache_root=cache_root,
    )
    assert service.status()["status"] == "hybrid_ready"
    return service, embedding


def test_passages_preserve_parent_boundaries(tmp_path) -> None:
    body = "甲在山中行走。乙随后赶来。" * 100
    path = tmp_path / "long.txt"
    path.write_text(f"第一章 长路\n\n{body}", encoding="utf-8")
    document = load_novel(path)

    passages = build_retrieval_passages(document)

    assert len(passages) > len(document.chunks)
    assert all(0 < len(passage.text) <= 700 for passage in passages)
    assert all(passage.parent_chunk_id for passage in passages)
    assert all(
        document.source.text[passage.start_char : passage.end_char] == passage.text
        for passage in passages
    )


def test_dense_only_hit_maps_back_to_readable_parent(novel_document, tmp_path) -> None:
    service, _provider = _build_hybrid(novel_document, tmp_path)

    hits = service.search("天庭使者劝他归顺", top_k=3)

    assert hits[0].section_title == "第二章 招安"
    assert hits[0].matched_terms == []
    parent_ids = {chunk.chunk_id for chunk in novel_document.chunks}
    assert hits[0].chunk_id in parent_ids


def test_dense_cache_is_reused_and_corruption_rebuilds(novel_document, tmp_path) -> None:
    first_service, first = _build_hybrid(novel_document, tmp_path)
    assert first.document_calls == 1
    assert first_service.status()["metrics"]["document_request_count"] == 1
    assert first_service.status()["metrics"]["document_text_count"] == len(first_service.passages)

    cached_service, cached = _build_hybrid(novel_document, tmp_path)
    assert cached.document_calls == 0
    assert cached_service.status()["metrics"]["index_cache_hit"] is True
    assert cached_service.status()["metrics"]["document_request_count"] == 0

    matrix_path = next(tmp_path.rglob("embeddings.npy"))
    matrix_path.write_bytes(b"not-a-valid-numpy-file")
    _rebuilt_service, rebuilt = _build_hybrid(novel_document, tmp_path)
    assert rebuilt.document_calls == 1
    assert any(
        event["code"] == "index_cache_corrupt"
        for event in _rebuilt_service.status()["events"]
    )

    changed = FakeEmbedding()
    changed_service = RetrievalService(novel_document)
    changed_service.prepare_dense(model_name="another-model", configured=True)
    changed_service.build_dense_index(
        changed,
        model_name="another-model",
        cache_root=tmp_path,
    )
    assert changed.document_calls == 1


def test_embedding_failure_degrades_without_losing_lexical_search(
    novel_document, tmp_path
) -> None:
    service = RetrievalService(novel_document)
    service.prepare_dense(model_name="broken", configured=True)

    with pytest.raises(RuntimeError, match="构建失败"):
        service.build_dense_index(
            FakeEmbedding(fail=True),
            model_name="broken",
            cache_root=tmp_path,
        )

    assert service.status()["status"] == "degraded"
    assert service.status()["active_mode"] == "lexical"
    assert service.search("白骨精", top_k=1)[0].section_title == "第三章 尸魔"


def test_query_embedding_failure_falls_back_to_bm25(novel_document, tmp_path) -> None:
    provider = FakeEmbedding()
    service, _provider = _build_hybrid(novel_document, tmp_path, provider)
    provider.fail = True

    hits = service.search("混世魔王", top_k=1)

    assert hits[0].section_title == "第一章 水帘洞"
    status = service.status()
    assert status["metrics"]["query_request_count"] == 1
    assert status["metrics"]["failed_request_count"] == 1
    assert status["metrics"]["fallback_count"] == 1
    assert status["events"][-1]["code"] == "embedding_query_failed"
    assert "fake provider failure" not in json.dumps(status, ensure_ascii=False)


def test_query_embedding_usage_and_cache_hits(novel_document, tmp_path) -> None:
    service, provider = _build_hybrid(novel_document, tmp_path)

    service.search("天庭使者劝他归顺", top_k=2)
    service.search("天庭使者劝他归顺", top_k=2)

    metrics = service.status()["metrics"]
    assert provider.query_calls == 1
    assert metrics["query_request_count"] == 1
    assert metrics["query_input_characters"] == len("天庭使者劝他归顺")
    assert metrics["query_cache_hit_count"] == 1


def test_results_diversify_sections_before_backfilling(tmp_path) -> None:
    first = ("众人寻找星辰钥匙，却始终没有发现。\n\n" * 140).strip()
    second = ("守门人最终交出了星辰钥匙。\n\n" * 25).strip()
    path = tmp_path / "diversity.txt"
    path.write_text(
        f"第一章 寻找\n\n{first}\n\n第二章 发现\n\n{second}",
        encoding="utf-8",
    )
    service = RetrievalService(load_novel(path))

    hits = service.search("星辰钥匙", top_k=3)

    assert len(hits) == 3
    assert len({hit.section_id for hit in hits}) == 2


def test_fixed_retrieval_evaluation_recall_at_five(novel_document, tmp_path) -> None:
    service, _provider = _build_hybrid(novel_document, tmp_path)
    cases_path = Path(__file__).parents[1] / "fixtures" / "retrieval_eval.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]

    successful = 0
    for case in cases:
        hits = service.search(case["query"], top_k=5)
        if any(hit.section_title == case["expected_section"] for hit in hits):
            successful += 1

    recall_at_five = successful / len(cases)
    assert recall_at_five >= 0.8


def test_normalized_embeddings_are_unit_length() -> None:
    matrix = RetrievalService._normalize_matrix([[3.0, 4.0], [1.0, 0.0]])

    assert np.allclose(np.linalg.norm(matrix, axis=1), [1.0, 1.0])
