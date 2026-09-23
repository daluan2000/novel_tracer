from __future__ import annotations

import bisect
import re

from novel_agent.models import (
    HeadingCandidate,
    NovelChunk,
    Section,
    SourceDocument,
    StructureReport,
)


def _line_for_char(document: SourceDocument, position: int) -> int:
    if not document.lines:
        return 1
    starts = [line.start_char for line in document.lines]
    bounded = min(max(position, 0), max(len(document.text) - 1, 0))
    return bisect.bisect_right(starts, bounded)


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _choose_boundary(text: str, minimum: int, target: int, maximum: int) -> int:
    window = text[minimum:maximum]
    candidates: list[int] = []
    for pattern in (r"\n\s*\n", r"[。！？!?]\s*", r"\n"):
        candidates.extend(minimum + match.end() for match in re.finditer(pattern, window))
    if not candidates:
        return maximum
    return min(candidates, key=lambda value: abs(value - target))


def split_span(
    text: str,
    start: int,
    end: int,
    *,
    min_chars: int = 1_000,
    target_chars: int = 1_800,
    max_chars: int = 2_500,
    overlap_chars: int = 180,
) -> list[tuple[int, int]]:
    start, end = _trim_span(text, start, end)
    if start >= end:
        return []

    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        remaining = end - cursor
        if remaining <= max_chars:
            chunk_start, chunk_end = _trim_span(text, cursor, end)
            if chunk_start < chunk_end:
                spans.append((chunk_start, chunk_end))
            break

        minimum = min(cursor + min_chars, end)
        target = min(cursor + target_chars, end)
        maximum = min(cursor + max_chars, end)
        boundary = _choose_boundary(text, minimum, target, maximum)
        chunk_start, chunk_end = _trim_span(text, cursor, boundary)
        if chunk_start < chunk_end:
            spans.append((chunk_start, chunk_end))

        next_cursor = max(cursor + 1, boundary - overlap_chars)
        if next_cursor < end:
            paragraph_break = text.rfind("\n\n", cursor + 1, next_cursor + 1)
            if paragraph_break > cursor:
                next_cursor = paragraph_break + 2
        cursor = next_cursor
    return spans


def _make_section(
    document: SourceDocument,
    *,
    section_id: str,
    title: str | None,
    title_type: str | None,
    level_hint: int | None,
    detected: bool,
    confidence: float,
    start_char: int,
    end_char: int,
) -> Section:
    return Section(
        section_id=section_id,
        title=title,
        title_type=title_type,
        level_hint=level_hint,
        detected=detected,
        confidence=confidence,
        start_char=start_char,
        end_char=end_char,
        start_line=_line_for_char(document, start_char),
        end_line=_line_for_char(document, max(start_char, end_char - 1)),
    )


def _chunk_for_section(
    document: SourceDocument,
    section: Section,
    start: int,
    end: int,
    chunk_number: int,
) -> NovelChunk:
    return NovelChunk(
        chunk_id=f"{section.section_id}_chunk_{chunk_number:03d}",
        section_id=section.section_id,
        section_title=section.title,
        section_detected=section.detected,
        start_char=start,
        end_char=end,
        start_line=_line_for_char(document, start),
        end_line=_line_for_char(document, max(start, end - 1)),
        text=document.text[start:end],
    )


def build_sections_and_chunks(
    document: SourceDocument,
    headings: list[HeadingCandidate],
    report: StructureReport,
) -> tuple[list[Section], list[NovelChunk]]:
    if not headings or report.fallback_used:
        spans = split_span(document.text, 0, len(document.text))
        sections: list[Section] = []
        chunks: list[NovelChunk] = []
        for index, (start, end) in enumerate(spans, start=1):
            section = _make_section(
                document,
                section_id=f"section_{index:04d}",
                title=None,
                title_type=None,
                level_hint=None,
                detected=False,
                confidence=0.0,
                start_char=start,
                end_char=end,
            )
            sections.append(section)
            chunks.append(_chunk_for_section(document, section, start, end, 1))
        return sections, chunks

    sections = []
    chunks = []
    section_number = 1

    first_heading = headings[0]
    if document.text[: first_heading.start_char].strip():
        section = _make_section(
            document,
            section_id=f"section_{section_number:04d}",
            title="卷首内容",
            title_type="front_matter",
            level_hint=0,
            detected=True,
            confidence=1.0,
            start_char=0,
            end_char=first_heading.start_char,
        )
        sections.append(section)
        for chunk_number, (start, end) in enumerate(
            split_span(document.text, 0, first_heading.start_char), start=1
        ):
            chunks.append(_chunk_for_section(document, section, start, end, chunk_number))
        section_number += 1

    for index, heading in enumerate(headings):
        next_start = headings[index + 1].start_char if index + 1 < len(headings) else len(document.text)
        section = _make_section(
            document,
            section_id=f"section_{section_number:04d}",
            title=heading.raw_text,
            title_type=heading.detector,
            level_hint=heading.level_hint,
            detected=True,
            confidence=min(1.0, heading.final_score / 10.0),
            start_char=heading.start_char,
            end_char=next_start,
        )
        sections.append(section)
        for chunk_number, (start, end) in enumerate(
            split_span(document.text, heading.end_char, next_start), start=1
        ):
            chunks.append(_chunk_for_section(document, section, start, end, chunk_number))
        section_number += 1
    return sections, chunks
