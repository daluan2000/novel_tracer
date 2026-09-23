from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from novel_agent.models import NovelChunk, NovelDocument, SearchHit
from novel_agent.novel_loader import load_novel


class NovelCorpus:
    def __init__(self, document: NovelDocument):
        self.document = document
        self._chunk_by_id = {chunk.chunk_id: chunk for chunk in document.chunks}
        self._section_chunks: dict[str, list[NovelChunk]] = {}
        for chunk in document.chunks:
            self._section_chunks.setdefault(chunk.section_id, []).append(chunk)
        self._global_index = {chunk.chunk_id: index for index, chunk in enumerate(document.chunks)}

    @classmethod
    def from_path(cls, path: str | Path) -> "NovelCorpus":
        return cls(load_novel(path))

    def structure_summary(self) -> dict:
        document = self.document
        return {
            "source_path": document.source.source_path,
            "encoding": document.source.encoding,
            "character_count": len(document.source.text),
            "line_count": len(document.source.lines),
            "section_count": len(document.sections),
            "chunk_count": len(document.chunks),
            "structure": document.structure.model_dump(),
            "sections": [
                {
                    "section_id": section.section_id,
                    "title": section.title,
                    "detected": section.detected,
                    "confidence": section.confidence,
                    "start_line": section.start_line,
                    "end_line": section.end_line,
                    "chunk_count": len(self._section_chunks.get(section.section_id, [])),
                }
                for section in document.sections
            ],
        }

    @staticmethod
    def _query_terms(keyword: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", keyword).strip().casefold()
        if not normalized:
            return []
        terms = [term for term in normalized.split(" ") if term]
        return list(dict.fromkeys([normalized, *terms]))

    def search(self, keyword: str, top_k: int = 5) -> list[SearchHit]:
        terms = self._query_terms(keyword)
        if not terms:
            raise ValueError("搜索关键词不能为空。")
        top_k = min(max(top_k, 1), 20)
        phrase = terms[0]
        hits: list[SearchHit] = []

        for chunk in self.document.chunks:
            haystack = chunk.text.casefold()
            title = (chunk.section_title or "").casefold()
            counts = Counter({term: haystack.count(term) + title.count(term) for term in terms})
            matched = [term for term in terms if counts[term] > 0]
            if not matched:
                continue
            phrase_count = counts[phrase]
            individual_terms = terms[1:] or terms
            score = phrase_count * 5.0 + sum(counts[term] for term in individual_terms)
            if individual_terms and all(counts[term] > 0 for term in individual_terms):
                score += 3.0

            positions = [haystack.find(term) for term in matched if haystack.find(term) >= 0]
            position = min(positions) if positions else 0
            snippet_start = max(0, position - 80)
            snippet_end = min(len(chunk.text), position + max(len(term) for term in matched) + 140)
            snippet = chunk.text[snippet_start:snippet_end].strip()
            hits.append(
                SearchHit(
                    chunk_id=chunk.chunk_id,
                    section_id=chunk.section_id,
                    section_title=chunk.section_title,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    score=score,
                    matched_terms=matched,
                    snippet=snippet,
                )
            )

        hits.sort(key=lambda item: (-item.score, item.start_line, item.chunk_id))
        return hits[:top_k]

    def read_context(self, chunk_id: str, before: int = 1, after: int = 1) -> list[NovelChunk]:
        if chunk_id not in self._global_index:
            raise KeyError(f"不存在的 chunk_id：{chunk_id}")
        before = min(max(before, 0), 5)
        after = min(max(after, 0), 5)
        index = self._global_index[chunk_id]
        start = max(0, index - before)
        end = min(len(self.document.chunks), index + after + 1)
        return self.document.chunks[start:end]

    def read_section(self, section_id: str, start_chunk: int = 0, limit: int = 3) -> list[NovelChunk]:
        if section_id not in self._section_chunks:
            raise KeyError(f"不存在或没有正文 Chunk 的 section_id：{section_id}")
        if start_chunk < 0:
            raise ValueError("start_chunk 不能为负数。")
        limit = min(max(limit, 1), 5)
        return self._section_chunks[section_id][start_chunk : start_chunk + limit]

    def get_chunk(self, chunk_id: str) -> NovelChunk:
        if chunk_id not in self._chunk_by_id:
            raise KeyError(f"不存在的 chunk_id：{chunk_id}")
        return self._chunk_by_id[chunk_id]

    def validate_quote(self, chunk_id: str, quote: str) -> bool:
        if not quote.strip():
            return False
        chunk = self.get_chunk(chunk_id)
        return quote.strip() in chunk.text
