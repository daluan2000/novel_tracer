from __future__ import annotations

import re
from pathlib import Path

from charset_normalizer import from_bytes

from novel_agent.models import SourceDocument, SourceLine


class TextDecodeError(ValueError):
    """Raised when a TXT file cannot be decoded without silently losing text."""


_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def _decode_bytes(raw: bytes) -> tuple[str, str]:
    for bom, encoding in _BOM_ENCODINGS:
        if raw.startswith(bom):
            text = raw.decode(encoding, errors="strict")
            if text.startswith("\ufeff"):
                text = text[1:]
            return text, encoding

    try:
        return raw.decode("utf-8", errors="strict"), "utf-8"
    except UnicodeDecodeError:
        pass

    # Chinese TXT files are commonly GBK/GB18030. Short samples are frequently
    # misclassified as Big5 or another East Asian codec by statistical detectors,
    # producing valid but unreadable text, so prefer the strict Chinese superset.
    try:
        return raw.decode("gb18030", errors="strict"), "gb18030"
    except UnicodeDecodeError:
        pass

    best = from_bytes(raw).best()
    if best is not None and best.encoding:
        try:
            text = raw.decode(best.encoding, errors="strict")
            return text, best.encoding.lower()
        except (LookupError, UnicodeDecodeError):
            pass

    for encoding in ("utf-16-le", "utf-16-be"):
        try:
            return raw.decode(encoding, errors="strict"), encoding
        except UnicodeDecodeError:
            continue

    raise TextDecodeError("无法可靠解码 TXT；请确认文件编码或先转换为 UTF-8。")


def _normalize_for_matching(line: str) -> str:
    line = line.replace("\u3000", " ").replace("\t", " ")
    return re.sub(r"\s+", " ", line).strip()


def _build_lines(text: str) -> list[SourceLine]:
    lines: list[SourceLine] = []
    cursor = 0
    raw_lines = text.splitlines(keepends=True)
    if not raw_lines and text == "":
        return []
    if raw_lines and sum(len(item) for item in raw_lines) < len(text):
        raw_lines.append(text[sum(len(item) for item in raw_lines) :])
    elif not raw_lines:
        raw_lines = [text]

    for index, raw_with_newline in enumerate(raw_lines, start=1):
        raw_text = raw_with_newline.rstrip("\r\n")
        end_char = cursor + len(raw_with_newline)
        lines.append(
            SourceLine(
                line_no=index,
                start_char=cursor,
                end_char=end_char,
                raw_text=raw_text,
                normalized_text=_normalize_for_matching(raw_text),
            )
        )
        cursor = end_char
    return lines


def load_source_document(path: str | Path) -> SourceDocument:
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"TXT 文件不存在：{source_path}")

    raw = source_path.read_bytes()
    text, encoding = _decode_bytes(raw)
    if not text.strip():
        raise ValueError(f"TXT 文件为空：{source_path}")

    return SourceDocument(
        source_path=str(source_path),
        encoding=encoding,
        text=text,
        lines=_build_lines(text),
        replacement_char_count=text.count("\ufffd"),
    )
