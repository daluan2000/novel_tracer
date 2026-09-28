from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict, Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jieba
import numpy as np
from rank_bm25 import BM25Okapi

from novel_agent.corpus.models import NovelDocument, SearchHit
from novel_agent.corpus.retrieval.models import EmbeddingProvider, RetrievalPassage
from novel_agent.corpus.retrieval.passages import build_retrieval_passages

INDEX_VERSION = "hybrid-v1"
LEXICAL_CANDIDATES = 30
DENSE_CANDIDATES = 30
RRF_K = 60
EMBEDDING_BATCH_SIZE = 10
QUERY_CACHE_SIZE = 128
EVENT_HISTORY_SIZE = 50

jieba.setLogLevel(logging.WARNING)


def _query_terms(query: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", query).strip().casefold()
    if not normalized:
        return []
    return list(dict.fromkeys([normalized, *normalized.split(" ")]))


def _tokens(text: str, *, unique: bool = False) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip().casefold()
    result = [
        token.strip()
        for token in jieba.cut_for_search(normalized)
        if len(token.strip()) >= 2
    ]
    for segment in re.findall(r"[\u3400-\u9fffA-Za-z0-9]+", normalized):
        if len(segment) >= 2:
            result.extend(segment[index : index + 2] for index in range(len(segment) - 1))
    if not result and normalized:
        result = [normalized]
    return list(dict.fromkeys(result)) if unique else result


def _safe_model_name(model_name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", model_name).strip("-.")
    return cleaned[:80] or "embedding"


def _is_timeout_error(error: BaseException) -> bool:
    current: BaseException | None = error
    for _ in range(4):
        if current is None:
            break
        if isinstance(current, TimeoutError) or "timeout" in type(current).__name__.lower():
            return True
        current = current.__cause__ or current.__context__
    return False


class RetrievalService:
    """Passage-level BM25 and dense retrieval with safe lexical fallback."""

    def __init__(self, document: NovelDocument):
        self.document = document
        self.passages = build_retrieval_passages(document)
        tokenized = [_tokens(passage.text + " " + (passage.section_title or "")) for passage in self.passages]
        self._token_sets = [set(tokens) for tokens in tokenized]
        self._bm25 = BM25Okapi(tokenized)
        self._lock = threading.RLock()
        self._dense_matrix: np.ndarray | None = None
        self._embedding_provider: EmbeddingProvider | None = None
        self._embedding_model: str | None = None
        self._status = "lexical_ready"
        self._active_mode = "lexical"
        self._error_code: str | None = None
        self._query_cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._source_hash = hashlib.sha256(document.source.text.encode("utf-8")).hexdigest()
        self._metrics: dict[str, Any] = {
            "document_request_count": 0,
            "document_text_count": 0,
            "document_input_characters": 0,
            "query_request_count": 0,
            "query_input_characters": 0,
            "query_cache_hit_count": 0,
            "index_cache_hit": None,
            "failed_request_count": 0,
            "fallback_count": 0,
            "last_request_elapsed_seconds": None,
        }
        self._events: list[dict[str, Any]] = []

    def _record_event(
        self,
        *,
        level: str,
        code: str,
        message: str,
        operation: str,
        fallback: str | None = None,
    ) -> None:
        """Store a bounded, provider-safe event for the status API."""

        with self._lock:
            self._events.append(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": level,
                    "code": code,
                    "message": message,
                    "operation": operation,
                    "fallback": fallback,
                }
            )
            if len(self._events) > EVENT_HISTORY_SIZE:
                del self._events[: len(self._events) - EVENT_HISTORY_SIZE]

    def _record_request(
        self,
        *,
        kind: str,
        text_count: int,
        character_count: int,
        elapsed_seconds: float,
        failed: bool,
    ) -> None:
        with self._lock:
            self._metrics[f"{kind}_request_count"] += 1
            self._metrics[f"{kind}_input_characters"] += character_count
            if kind == "document":
                self._metrics["document_text_count"] += text_count
            self._metrics["last_request_elapsed_seconds"] = round(elapsed_seconds, 3)
            if failed:
                self._metrics["failed_request_count"] += 1

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "active_mode": self._active_mode,
                "passage_count": len(self.passages),
                "embedding_model": self._embedding_model,
                "error_code": self._error_code,
                "metrics": dict(self._metrics),
                "events": [dict(event) for event in self._events],
            }

    def prepare_dense(self, *, model_name: str, configured: bool) -> bool:
        with self._lock:
            self._embedding_model = model_name or None
            self._dense_matrix = None
            self._embedding_provider = None
            self._query_cache.clear()
            self._active_mode = "lexical"
            if not configured:
                self._status = "degraded"
                self._error_code = "embedding_not_configured"
                self._metrics["fallback_count"] += 1
                self._record_event(
                    level="warning",
                    code="embedding_not_configured",
                    message="未配置 Embedding 模型或凭据，检索已使用 BM25。",
                    operation="index_build",
                    fallback="bm25",
                )
                return False
            self._status = "building"
            self._error_code = None
            return True

    def mark_dense_failed(self) -> None:
        with self._lock:
            already_recorded = (
                self._status == "degraded"
                and self._error_code == "embedding_build_failed"
            )
            self._status = "degraded"
            self._active_mode = "lexical"
            self._error_code = "embedding_build_failed"
            self._dense_matrix = None
            self._embedding_provider = None
            self._query_cache.clear()
            if not already_recorded:
                self._metrics["fallback_count"] += 1
                self._record_event(
                    level="error",
                    code="embedding_build_failed",
                    message="向量索引构建失败，检索已降级为 BM25。",
                    operation="index_build",
                    fallback="bm25",
                )

    def _cache_directory(self, cache_root: Path, model_name: str) -> Path:
        return cache_root / self._source_hash / f"{INDEX_VERSION}-{_safe_model_name(model_name)}"

    def _fingerprint(self, model_name: str) -> str:
        data = {
            "source_hash": self._source_hash,
            "index_version": INDEX_VERSION,
            "model": model_name,
            "passage_count": len(self.passages),
            "passage_settings": {"min": 250, "target": 500, "max": 700, "overlap": 80},
        }
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _load_cached_matrix(self, cache_root: Path, model_name: str) -> np.ndarray | None:
        directory = self._cache_directory(cache_root, model_name)
        metadata_path = directory / "metadata.json"
        matrix_path = directory / "embeddings.npy"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("fingerprint") != self._fingerprint(model_name):
                return None
            matrix = np.load(matrix_path, allow_pickle=False)
            if matrix.ndim != 2 or matrix.shape[0] != len(self.passages) or matrix.shape[1] == 0:
                return None
            if not np.isfinite(matrix).all():
                return None
            return matrix.astype(np.float32, copy=False)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _write_cache(self, cache_root: Path, model_name: str, matrix: np.ndarray) -> None:
        directory = self._cache_directory(cache_root, model_name)
        directory.mkdir(parents=True, exist_ok=True)
        matrix_tmp = directory / "embeddings.tmp"
        matrix_path = directory / "embeddings.npy"
        metadata_tmp = directory / "metadata.tmp"
        metadata_path = directory / "metadata.json"
        with matrix_tmp.open("wb") as handle:
            np.save(handle, matrix, allow_pickle=False)
        matrix_tmp.replace(matrix_path)
        metadata = {
            "fingerprint": self._fingerprint(model_name),
            "model": model_name,
            "passage_count": len(self.passages),
            "dimension": int(matrix.shape[1]),
            "index_version": INDEX_VERSION,
        }
        metadata_tmp.write_text(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        metadata_tmp.replace(metadata_path)

    @staticmethod
    def _normalize_matrix(vectors: list[list[float]]) -> np.ndarray:
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or not matrix.shape[0] or not matrix.shape[1]:
            raise ValueError("Embedding 服务返回了无效矩阵。")
        if not np.isfinite(matrix).all():
            raise ValueError("Embedding 服务返回了非有限数值。")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ValueError("Embedding 服务返回了零向量。")
        return matrix / norms

    def build_dense_index(
        self,
        provider: EmbeddingProvider,
        *,
        model_name: str,
        cache_root: Path,
    ) -> None:
        cache_directory = self._cache_directory(cache_root, model_name)
        cache_artifacts_exist = (
            (cache_directory / "metadata.json").exists()
            or (cache_directory / "embeddings.npy").exists()
        )
        matrix = self._load_cached_matrix(cache_root, model_name)
        with self._lock:
            self._metrics["index_cache_hit"] = matrix is not None
        if matrix is None and cache_artifacts_exist:
            self._record_event(
                level="warning",
                code="index_cache_corrupt",
                message="Embedding 索引缓存无效，正在重新构建。",
                operation="index_build",
            )
        if matrix is None:
            last_error: Exception | None = None
            for _attempt in range(3):
                try:
                    vectors: list[list[float]] = []
                    texts = [passage.text for passage in self.passages]
                    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
                        batch = texts[start : start + EMBEDDING_BATCH_SIZE]
                        started_at = time.perf_counter()
                        failed = False
                        try:
                            embedded = provider.embed_documents(batch)
                            if len(embedded) != len(batch):
                                raise ValueError("Embedding 数量与 Passage 数量不一致。")
                        except Exception:
                            failed = True
                            raise
                        finally:
                            self._record_request(
                                kind="document",
                                text_count=len(batch),
                                character_count=sum(len(text) for text in batch),
                                elapsed_seconds=time.perf_counter() - started_at,
                                failed=failed,
                            )
                        vectors.extend(embedded)
                    matrix = self._normalize_matrix(vectors)
                    self._write_cache(cache_root, model_name, matrix)
                    break
                except Exception as exc:  # Provider exceptions vary by SDK/vendor.
                    last_error = exc
                    timed_out = _is_timeout_error(exc)
                    self._record_event(
                        level="warning",
                        code=(
                            "embedding_timeout"
                            if timed_out
                            else "embedding_document_request_failed"
                        ),
                        message=(
                            f"Embedding 文档编码请求超时，已完成第 {_attempt + 1}/3 次尝试。"
                            if timed_out
                            else f"Embedding 文档编码请求失败，已完成第 {_attempt + 1}/3 次尝试。"
                        ),
                        operation="index_build",
                    )
            if matrix is None:
                self.mark_dense_failed()
                raise RuntimeError("向量索引构建失败。") from last_error

        with self._lock:
            self._dense_matrix = matrix
            self._embedding_provider = provider
            self._embedding_model = model_name
            self._status = "hybrid_ready"
            self._active_mode = "hybrid"
            self._error_code = None
            self._query_cache.clear()

    def _lexical_ranking(self, query: str, terms: list[str]) -> list[int]:
        query_tokens = _tokens(query, unique=True)
        scores = np.asarray(self._bm25.get_scores(query_tokens), dtype=np.float64)
        for index, passage in enumerate(self.passages):
            haystack = passage.text.casefold()
            title = (passage.section_title or "").casefold()
            counts = Counter({term: haystack.count(term) + title.count(term) for term in terms})
            if terms:
                scores[index] += counts[terms[0]] * 5.0
                scores[index] += sum(counts[term] for term in terms[1:] or terms)
                scores[index] += sum(title.count(term) * 2.0 for term in terms)
        candidates = [
            index
            for index, score in enumerate(scores)
            if score > 0
            or any(token in self._token_sets[index] for token in query_tokens)
            or any(
                term
                in (
                    self.passages[index].text
                    + (self.passages[index].section_title or "")
                ).casefold()
                for term in terms
            )
        ]
        candidates.sort(key=lambda index: (-float(scores[index]), self.passages[index].start_char))
        return candidates[:LEXICAL_CANDIDATES]

    def _query_vector(self, query: str, provider: EmbeddingProvider) -> np.ndarray:
        with self._lock:
            cached = self._query_cache.get(query)
            if cached is not None:
                self._metrics["query_cache_hit_count"] += 1
                self._query_cache.move_to_end(query)
                return cached
        started_at = time.perf_counter()
        failed = False
        try:
            embedded = provider.embed_query(query)
        except Exception:
            failed = True
            raise
        finally:
            self._record_request(
                kind="query",
                text_count=1,
                character_count=len(query),
                elapsed_seconds=time.perf_counter() - started_at,
                failed=failed,
            )
        vector = self._normalize_matrix([embedded])[0]
        with self._lock:
            self._query_cache[query] = vector
            self._query_cache.move_to_end(query)
            while len(self._query_cache) > QUERY_CACHE_SIZE:
                self._query_cache.popitem(last=False)
        return vector

    def _dense_ranking(self, query: str) -> list[int]:
        with self._lock:
            matrix = self._dense_matrix
            provider = self._embedding_provider
        if matrix is None or provider is None:
            return []
        try:
            vector = self._query_vector(query, provider)
            if vector.shape[0] != matrix.shape[1]:
                with self._lock:
                    self._metrics["fallback_count"] += 1
                self._record_event(
                    level="error",
                    code="embedding_dimension_mismatch",
                    message="查询向量维度与索引不一致，本次检索已降级为 BM25。",
                    operation="query",
                    fallback="bm25",
                )
                return []
            scores = matrix @ vector
            count = min(DENSE_CANDIDATES, len(scores))
            if count == 0:
                return []
            indices = np.argpartition(-scores, count - 1)[:count]
            return sorted(indices.tolist(), key=lambda index: (-float(scores[index]), self.passages[index].start_char))
        except Exception as exc:  # A query-time provider failure must not break lexical search.
            with self._lock:
                self._metrics["fallback_count"] += 1
            timed_out = _is_timeout_error(exc)
            self._record_event(
                level="error",
                code="embedding_timeout" if timed_out else "embedding_query_failed",
                message=(
                    "查询向量生成超时，本次检索已降级为 BM25。"
                    if timed_out
                    else "查询向量生成失败，本次检索已降级为 BM25。"
                ),
                operation="query",
                fallback="bm25",
            )
            return []

    @staticmethod
    def _rrf(*rankings: list[int]) -> dict[int, float]:
        scores: dict[int, float] = {}
        for ranking in rankings:
            for rank, index in enumerate(ranking, start=1):
                scores[index] = scores.get(index, 0.0) + 1.0 / (RRF_K + rank)
        return scores

    @staticmethod
    def _snippet(passage: RetrievalPassage, terms: list[str], maximum: int = 600) -> str:
        text = passage.text.strip()
        if len(text) <= maximum:
            return text
        folded = text.casefold()
        positions = [folded.find(term) for term in terms if folded.find(term) >= 0]
        if not positions:
            return text[:maximum].rstrip()
        start = max(0, min(positions) - 120)
        return text[start : start + maximum].strip()

    def search(self, query: str, top_k: int) -> list[SearchHit]:
        terms = _query_terms(query)
        if not terms:
            raise ValueError("搜索关键词不能为空。")
        top_k = min(max(top_k, 1), 20)
        lexical = self._lexical_ranking(query, terms)
        with self._lock:
            status = self._status
            error_code = self._error_code
        dense = self._dense_ranking(query)
        if status in {"building", "degraded"}:
            with self._lock:
                self._metrics["fallback_count"] += 1
            if status == "building":
                code = "embedding_index_building"
                message = "向量索引仍在构建，本次检索使用 BM25。"
            else:
                code = "bm25_fallback"
                message = (
                    "Embedding 未配置，本次检索使用 BM25。"
                    if error_code == "embedding_not_configured"
                    else "向量索引不可用，本次检索使用 BM25。"
                )
            self._record_event(
                level="warning",
                code=code,
                message=message,
                operation="query",
                fallback="bm25",
            )
        fused = self._rrf(lexical, dense)
        ordered = sorted(
            fused,
            key=lambda index: (-fused[index], self.passages[index].start_char, self.passages[index].passage_id),
        )

        selected: list[int] = []
        parents: set[str] = set()
        section_counts: Counter[str] = Counter()
        for enforce_diversity in (True, False):
            for index in ordered:
                passage = self.passages[index]
                if passage.parent_chunk_id in parents:
                    continue
                if enforce_diversity and section_counts[passage.section_id] >= 2:
                    continue
                selected.append(index)
                parents.add(passage.parent_chunk_id)
                section_counts[passage.section_id] += 1
                if len(selected) >= top_k:
                    break
            if len(selected) >= top_k:
                break

        hits: list[SearchHit] = []
        for index in selected:
            passage = self.passages[index]
            haystack = (passage.text + " " + (passage.section_title or "")).casefold()
            matched = [term for term in terms if term in haystack]
            hits.append(
                SearchHit(
                    chunk_id=passage.parent_chunk_id,
                    section_id=passage.section_id,
                    section_title=passage.section_title,
                    start_line=passage.start_line,
                    end_line=passage.end_line,
                    score=round(fused[index] * 1000.0, 6),
                    matched_terms=matched,
                    snippet=self._snippet(passage, matched or terms),
                )
            )
        return hits
