from __future__ import annotations

import pytest

from novel_agent.novel_loader import load_novel
from novel_agent.structure_detector import parse_chinese_number


def _body(marker: str) -> str:
    return (f"这是{marker}的正文内容，人物在这里交谈并推动故事发展。" * 30) + "\n\n"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("一", 1), ("十二", 12), ("一百零二", 102), ("一千零一", 1001), ("壹拾贰", 12)],
)
def test_parse_chinese_number(value: str, expected: int) -> None:
    assert parse_chinese_number(value) == expected


@pytest.mark.parametrize(
    "headings",
    [
        ["第1章 风起", "第2章 雪落", "第3章 重逢"],
        ["第一章 风起", "第十二章 雪落", "第二十三回 重逢"],
        ["1、风起", "2、雪落", "3、重逢"],
        ["（一）风起", "（二）雪落", "（三）重逢"],
        ["Chapter I Dawn", "Chapter II Snow", "Chapter III Return"],
    ],
)
def test_numbered_heading_families(tmp_path, headings: list[str]) -> None:
    text = "".join(f"{heading}\n\n{_body(str(index))}" for index, heading in enumerate(headings))
    path = tmp_path / "novel.txt"
    path.write_text(text, encoding="utf-8")

    document = load_novel(path)

    assert not document.structure.fallback_used
    assert document.structure.heading_count >= 3
    titles = [section.title for section in document.sections if section.title]
    assert all(heading in titles for heading in headings)


def test_unnumbered_headings_are_detected_when_layout_is_consistent(tmp_path) -> None:
    headings = ["风雪夜归人", "旧城", "迟来的信"]
    text = "".join(f"{heading}\n\n{_body(heading)}" for heading in headings)
    path = tmp_path / "plain.txt"
    path.write_text(text, encoding="utf-8")

    document = load_novel(path)

    assert not document.structure.fallback_used
    assert "plain" in document.structure.detectors_used
    assert headings == [section.title for section in document.sections]


def test_mixed_headings_are_combined(tmp_path) -> None:
    headings = ["序章", "第一章 出发", "2、旧城", "尾声"]
    text = "".join(f"{heading}\n\n{_body(heading)}" for heading in headings)
    path = tmp_path / "mixed.txt"
    path.write_text(text, encoding="utf-8")

    document = load_novel(path)

    assert document.structure.strategy == "mixed_headings"
    assert all(heading in [section.title for section in document.sections] for heading in headings)


def test_no_headings_falls_back_to_synthetic_sections(tmp_path) -> None:
    text = "\n\n".join("这是一段没有标题的连续正文。" * 90 for _ in range(6))
    path = tmp_path / "no-headings.txt"
    path.write_text(text, encoding="utf-8")

    document = load_novel(path)

    assert document.structure.fallback_used
    assert len(document.sections) >= 2
    assert all(section.title is None for section in document.sections)
    assert all(not section.detected for section in document.sections)


def test_dense_numbered_list_is_not_treated_as_chapters(tmp_path) -> None:
    items = "\n".join(f"{index}、列表项目{index}" for index in range(1, 20))
    text = _body("开头") + items + "\n" + _body("结尾")
    path = tmp_path / "list.txt"
    path.write_text(text, encoding="utf-8")

    document = load_novel(path)

    assert document.structure.fallback_used


def test_long_paragraph_is_split_without_losing_location(tmp_path) -> None:
    text = "没有自然段边界的长正文。" * 1000
    path = tmp_path / "long.txt"
    path.write_text(text, encoding="utf-8")

    document = load_novel(path)

    assert len(document.chunks) > 1
    assert all(chunk.start_char < chunk.end_char for chunk in document.chunks)
    assert document.chunks[0].start_char == 0
