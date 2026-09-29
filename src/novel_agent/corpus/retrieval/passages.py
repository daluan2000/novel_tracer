from __future__ import annotations

import bisect

from novel_agent.corpus.chunking import split_span
from novel_agent.corpus.models import NovelDocument
from novel_agent.corpus.retrieval.models import RetrievalPassage


def _line_for_char(document: NovelDocument, position: int) -> int:
    starts = [line.start_char for line in document.source.lines]
    if not starts:
        return 1
    bounded = min(max(position, 0), max(len(document.source.text) - 1, 0))
    return bisect.bisect_right(starts, bounded)


def build_retrieval_passages(
    document: NovelDocument,
    *,
    min_chars: int = 250,
    target_chars: int = 500,
    max_chars: int = 700,
    overlap_chars: int = 80,
) -> list[RetrievalPassage]:
    passages: list[RetrievalPassage] = []
    for chunk in document.chunks:
        spans = split_span(
            document.source.text,
            chunk.start_char,
            chunk.end_char,
            min_chars=min_chars,
            target_chars=target_chars,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
        for number, (start, end) in enumerate(spans, start=1):
            passages.append(
                RetrievalPassage(
                    passage_id=f"{chunk.chunk_id}_passage_{number:03d}",
                    parent_chunk_id=chunk.chunk_id,
                    section_id=chunk.section_id,
                    section_title=chunk.section_title,
                    start_char=start,
                    end_char=end,
                    start_line=_line_for_char(document, start),
                    end_line=_line_for_char(document, max(start, end - 1)),
                    text=document.source.text[start:end],
                )
            )
    return passages
