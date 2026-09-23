from __future__ import annotations

import re
from collections import defaultdict
from statistics import median
from typing import Protocol

from novel_agent.models import HeadingCandidate, SourceDocument, SourceLine, StructureReport


_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "贰": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_BIG_UNITS = {"万": 10_000, "萬": 10_000}
_CN_NUMBER = "零〇一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟萬"
_ENDING_PUNCTUATION = set("，。；！？,.!?;：:")


def parse_chinese_number(value: str) -> int | None:
    cleaned = re.sub(r"\s+", "", value)
    if not cleaned or any(ch not in _CN_DIGITS | _CN_UNITS | _CN_BIG_UNITS for ch in cleaned):
        return None
    if all(ch in _CN_DIGITS for ch in cleaned):
        digits = "".join(str(_CN_DIGITS[ch]) for ch in cleaned)
        return int(digits)

    total = 0
    section = 0
    number = 0
    for char in cleaned:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            section += (number or 1) * unit
            number = 0
        else:
            big_unit = _CN_BIG_UNITS[char]
            section = (section + number) * big_unit
            total += section
            section = 0
            number = 0
    return total + section + number


def parse_roman_number(value: str) -> int | None:
    roman = value.upper()
    if not roman or re.fullmatch(r"[IVXLCDM]+", roman) is None:
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for char in reversed(roman):
        current = values[char]
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    return total


def _candidate(
    line: SourceLine,
    *,
    detector: str,
    number_value: int | None = None,
    unit: str | None = None,
    level_hint: int | None = None,
    local_score: float,
) -> HeadingCandidate:
    return HeadingCandidate(
        line_no=line.line_no,
        start_char=line.start_char,
        end_char=line.end_char,
        raw_text=line.raw_text.strip(),
        normalized_text=line.normalized_text,
        detector=detector,
        number_value=number_value,
        unit=unit,
        level_hint=level_hint,
        local_score=local_score,
    )


class HeadingDetector(Protocol):
    name: str

    def detect(self, lines: list[SourceLine]) -> list[HeadingCandidate]: ...


class DiUnitHeadingDetector:
    name = "di_unit"
    pattern = re.compile(
        rf"^第\s*([0-9]+|[{_CN_NUMBER}]+)\s*(章|节|回|卷|部|篇|集|幕)"
        r"(?:\s*[：:、.．\-—]?\s*(.*))?$",
        re.IGNORECASE,
    )

    def detect(self, lines: list[SourceLine]) -> list[HeadingCandidate]:
        results: list[HeadingCandidate] = []
        for line in lines:
            text = line.normalized_text
            if not text or len(text) > 80:
                continue
            match = self.pattern.fullmatch(text)
            if not match:
                continue
            raw_number, unit = match.group(1), match.group(2)
            number = int(raw_number) if raw_number.isdigit() else parse_chinese_number(raw_number)
            level = 1 if unit in {"卷", "部", "篇", "集"} else 2
            results.append(
                _candidate(
                    line,
                    detector=self.name,
                    number_value=number,
                    unit=unit,
                    level_hint=level,
                    local_score=5.0,
                )
            )
        return results


class NumericHeadingDetector:
    name = "numeric"
    bracketed = re.compile(r"^[（(【\[]\s*(\d{1,5})\s*[）)】\]](?:\s*(.*))?$")
    delimited = re.compile(r"^(\d{1,5})\s*[、.．:：\-—]\s*(.*)$")
    plain = re.compile(r"^(\d{1,5})$")

    def detect(self, lines: list[SourceLine]) -> list[HeadingCandidate]:
        results: list[HeadingCandidate] = []
        for line in lines:
            text = line.normalized_text
            if not text or len(text) > 60:
                continue
            match = self.bracketed.fullmatch(text) or self.delimited.fullmatch(text) or self.plain.fullmatch(text)
            if match:
                results.append(
                    _candidate(
                        line,
                        detector=self.name,
                        number_value=int(match.group(1)),
                        level_hint=2,
                        local_score=2.5,
                    )
                )
        return results


class ChineseListHeadingDetector:
    name = "chinese_numeric"
    bracketed = re.compile(rf"^[（(【\[]\s*([{_CN_NUMBER}]+)\s*[）)】\]](?:\s*(.*))?$")
    delimited = re.compile(rf"^([{_CN_NUMBER}]+)\s*[、.．:：\-—]\s*(.*)$")
    plain = re.compile(rf"^([{_CN_NUMBER}]+)$")

    def detect(self, lines: list[SourceLine]) -> list[HeadingCandidate]:
        results: list[HeadingCandidate] = []
        for line in lines:
            text = line.normalized_text
            if not text or len(text) > 60:
                continue
            match = self.bracketed.fullmatch(text) or self.delimited.fullmatch(text) or self.plain.fullmatch(text)
            if not match:
                continue
            number = parse_chinese_number(match.group(1))
            if number is not None:
                results.append(
                    _candidate(
                        line,
                        detector=self.name,
                        number_value=number,
                        level_hint=2,
                        local_score=2.5,
                    )
                )
        return results


class EnglishHeadingDetector:
    name = "english"
    pattern = re.compile(
        r"^(chapter|part|book|volume)\s+([0-9]+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)"
        r"(?:\s*[.：:\-—]?\s*(.*))?$",
        re.IGNORECASE,
    )
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }

    def detect(self, lines: list[SourceLine]) -> list[HeadingCandidate]:
        results: list[HeadingCandidate] = []
        for line in lines:
            text = line.normalized_text
            if not text or len(text) > 80:
                continue
            match = self.pattern.fullmatch(text)
            if not match:
                continue
            unit, raw_number = match.group(1).lower(), match.group(2).lower()
            if raw_number.isdigit():
                number = int(raw_number)
            else:
                number = self.words.get(raw_number, parse_roman_number(raw_number))
            level = 1 if unit in {"part", "book", "volume"} else 2
            results.append(
                _candidate(
                    line,
                    detector=self.name,
                    number_value=number,
                    unit=unit,
                    level_hint=level,
                    local_score=5.0,
                )
            )
        return results


class SpecialHeadingDetector:
    name = "special"
    pattern = re.compile(
        rf"^(序|序章|楔子|引子|前言|正文|终章|尾声|后记|附录|番外(?:\s*[0-9{_CN_NUMBER}]+)?)(?:\s+.*)?$",
        re.IGNORECASE,
    )
    english_pattern = re.compile(r"^(prologue|epilogue|preface|appendix)(?:\s+.*)?$", re.IGNORECASE)

    def detect(self, lines: list[SourceLine]) -> list[HeadingCandidate]:
        results: list[HeadingCandidate] = []
        for line in lines:
            text = line.normalized_text
            if text and len(text) <= 60 and (self.pattern.fullmatch(text) or self.english_pattern.fullmatch(text)):
                results.append(
                    _candidate(line, detector=self.name, level_hint=1, local_score=4.0)
                )
        return results


class PlainHeadingDetector:
    name = "plain"

    def detect(self, lines: list[SourceLine]) -> list[HeadingCandidate]:
        results: list[HeadingCandidate] = []
        for index, line in enumerate(lines):
            text = line.normalized_text
            if not 2 <= len(text) <= 30:
                continue
            if text[-1] in _ENDING_PUNCTUATION:
                continue
            if text.startswith(("“", "‘", '"', "—")) or text.endswith(("”", "’", '"')):
                continue
            punctuation_count = sum(ch in "，。；！？,.!?;：:、" for ch in text)
            if punctuation_count > 1:
                continue
            previous_blank = index == 0 or not lines[index - 1].normalized_text
            next_blank = index == len(lines) - 1 or not lines[index + 1].normalized_text
            next_is_body = index < len(lines) - 1 and len(lines[index + 1].normalized_text) >= 50
            if not (previous_blank or next_blank) or not (next_blank or next_is_body):
                continue
            results.append(
                _candidate(line, detector=self.name, level_hint=2, local_score=1.0)
            )
        return results


DEFAULT_DETECTORS: tuple[HeadingDetector, ...] = (
    DiUnitHeadingDetector(),
    NumericHeadingDetector(),
    ChineseListHeadingDetector(),
    EnglishHeadingDetector(),
    SpecialHeadingDetector(),
    PlainHeadingDetector(),
)


def _sequence_ratio(candidates: list[HeadingCandidate]) -> float:
    numbers = [item.number_value for item in candidates if item.number_value is not None]
    if len(numbers) < 2:
        return 0.0
    sequential = sum(current == previous + 1 for previous, current in zip(numbers, numbers[1:]))
    return sequential / (len(numbers) - 1)


def _blank_context(document: SourceDocument, candidate: HeadingCandidate) -> tuple[bool, bool]:
    index = candidate.line_no - 1
    before = index == 0 or not document.lines[index - 1].normalized_text
    after = index == len(document.lines) - 1 or not document.lines[index + 1].normalized_text
    return before, after


def _remove_dense_runs(candidates: list[HeadingCandidate]) -> tuple[list[HeadingCandidate], int]:
    if len(candidates) < 3:
        return candidates, 0
    sorted_items = sorted(candidates, key=lambda item: item.start_char)
    rejected_lines: set[int] = set()
    run: list[HeadingCandidate] = [sorted_items[0]]
    for item in sorted_items[1:]:
        if item.start_char - run[-1].start_char < 180:
            run.append(item)
        else:
            if len(run) >= 3:
                rejected_lines.update(candidate.line_no for candidate in run)
            run = [item]
    if len(run) >= 3:
        rejected_lines.update(candidate.line_no for candidate in run)
    filtered = [item for item in sorted_items if item.line_no not in rejected_lines]
    return filtered, len(sorted_items) - len(filtered)


def detect_structure(
    document: SourceDocument,
    detectors: tuple[HeadingDetector, ...] = DEFAULT_DETECTORS,
) -> tuple[list[HeadingCandidate], StructureReport]:
    all_candidates = [candidate for detector in detectors for candidate in detector.detect(document.lines)]
    groups: dict[str, list[HeadingCandidate]] = defaultdict(list)
    for candidate in all_candidates:
        groups[candidate.detector].append(candidate)
    for items in groups.values():
        items.sort(key=lambda item: item.start_char)

    scored: list[HeadingCandidate] = []
    for detector_name, items in groups.items():
        sequence_ratio = _sequence_ratio(items)
        char_gaps = [b.start_char - a.start_char for a, b in zip(items, items[1:])]
        line_gaps = [b.line_no - a.line_no for a, b in zip(items, items[1:])]
        median_char_gap = median(char_gaps) if char_gaps else 0
        median_line_gap = median(line_gaps) if line_gaps else 0

        for candidate in items:
            score = candidate.local_score
            before_blank, after_blank = _blank_context(document, candidate)
            if before_blank and after_blank:
                score += 2.0
            elif before_blank or after_blank:
                score += 1.0
            if 2 <= len(candidate.normalized_text) <= 30:
                score += 1.0
            if len(items) >= 2:
                score += 2.0
            if sequence_ratio >= 0.6:
                score += 3.0
            elif sequence_ratio >= 0.3:
                score += 1.0
            if median_char_gap >= 300:
                score += 1.0
            if median_line_gap and median_line_gap <= 3:
                score -= 4.0
            if len(candidate.normalized_text) > 60:
                score -= 3.0
            if candidate.normalized_text and candidate.normalized_text[-1] in _ENDING_PUNCTUATION:
                score -= 3.0
            candidate.final_score = score
            threshold = 5.5 if detector_name == "plain" else 4.0
            plain_supported = (
                detector_name != "plain"
                or (len(items) >= 3 and median_char_gap >= 300 and median_line_gap >= 4)
            )
            if score >= threshold and plain_supported:
                scored.append(candidate)

    # Multiple detectors can recognize the same line. Prefer the highest-confidence result.
    best_by_line: dict[int, HeadingCandidate] = {}
    for candidate in scored:
        existing = best_by_line.get(candidate.line_no)
        if existing is None or candidate.final_score > existing.final_score:
            best_by_line[candidate.line_no] = candidate
    accepted = sorted(best_by_line.values(), key=lambda item: item.start_char)
    accepted, dense_rejected = _remove_dense_runs(accepted)

    rejected_count = max(0, len(all_candidates) - len(accepted))
    warnings: list[str] = []
    if dense_rejected:
        warnings.append("检测到密集标题候选，已按目录或列表噪声过滤。")

    if len(accepted) < 2:
        warnings.append("可靠标题不足，已使用段落感知的长度切分。")
        return [], StructureReport(
            strategy="fallback_chunks",
            confidence=0.0,
            heading_count=0,
            detectors_used=[],
            rejected_candidate_count=len(all_candidates),
            fallback_used=True,
            warnings=warnings,
        )

    detector_names = sorted({item.detector for item in accepted})
    average_score = sum(min(item.final_score, 10.0) for item in accepted) / len(accepted)
    confidence = min(0.99, max(0.5, average_score / 10.0))
    strategy = "mixed_headings" if len(detector_names) > 1 else "detected_headings"
    return accepted, StructureReport(
        strategy=strategy,
        confidence=confidence,
        heading_count=len(accepted),
        detectors_used=detector_names,
        rejected_candidate_count=rejected_count,
        fallback_used=False,
        warnings=warnings,
    )
