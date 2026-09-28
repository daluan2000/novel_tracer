from __future__ import annotations

from pathlib import Path

from novel_agent.corpus.chunking import build_sections_and_chunks
from novel_agent.corpus.models import NovelDocument
from novel_agent.corpus.normalization import load_source_document
from novel_agent.corpus.structure import detect_structure


def load_novel(path: str | Path) -> NovelDocument:
    source = load_source_document(path)
    headings, report = detect_structure(source)
    sections, chunks = build_sections_and_chunks(source, headings, report)
    if not chunks:
        raise ValueError("小说无法生成任何有效文本块，请检查文件内容。")
    return NovelDocument(
        source=source,
        structure=report,
        sections=sections,
        chunks=chunks,
    )
