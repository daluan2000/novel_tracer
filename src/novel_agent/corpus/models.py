from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SourceLine(BaseModel):
    line_no: int
    start_char: int
    end_char: int
    raw_text: str
    normalized_text: str


class SourceDocument(BaseModel):
    source_path: str
    encoding: str
    text: str
    lines: list[SourceLine]
    replacement_char_count: int = 0


class HeadingCandidate(BaseModel):
    line_no: int
    start_char: int
    end_char: int
    raw_text: str
    normalized_text: str
    detector: str
    number_value: int | None = None
    unit: str | None = None
    level_hint: int | None = None
    local_score: float = 0.0
    final_score: float = 0.0


class StructureReport(BaseModel):
    strategy: Literal["detected_headings", "mixed_headings", "fallback_chunks"]
    confidence: float = Field(ge=0.0, le=1.0)
    heading_count: int
    detectors_used: list[str] = Field(default_factory=list)
    rejected_candidate_count: int = 0
    fallback_used: bool
    warnings: list[str] = Field(default_factory=list)


class Section(BaseModel):
    section_id: str
    title: str | None
    title_type: str | None
    level_hint: int | None
    detected: bool
    confidence: float = Field(ge=0.0, le=1.0)
    start_char: int
    end_char: int
    start_line: int
    end_line: int


class NovelChunk(BaseModel):
    chunk_id: str
    section_id: str
    section_title: str | None
    section_detected: bool
    start_char: int
    end_char: int
    start_line: int
    end_line: int
    text: str


class NovelDocument(BaseModel):
    source: SourceDocument
    structure: StructureReport
    sections: list[Section]
    chunks: list[NovelChunk]


class SearchHit(BaseModel):
    chunk_id: str
    section_id: str
    section_title: str | None
    start_line: int
    end_line: int
    score: float
    matched_terms: list[str]
    snippet: str
