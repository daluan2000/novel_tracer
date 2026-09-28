from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class RetrievalPassage(BaseModel):
    passage_id: str
    parent_chunk_id: str
    section_id: str
    section_title: str | None
    start_char: int
    end_char: int
    start_line: int
    end_line: int
    text: str


class EmbeddingProvider(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class PassageReranker(Protocol):
    """Extension point for a later cross-encoder reranker."""

    def rerank(
        self,
        query: str,
        passages: list[RetrievalPassage],
    ) -> list[RetrievalPassage]: ...
