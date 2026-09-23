from __future__ import annotations

import pytest

from novel_agent.text_normalizer import load_source_document


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "gb18030"])
def test_common_chinese_encodings(tmp_path, encoding: str) -> None:
    path = tmp_path / f"sample-{encoding}.txt"
    content = "第一章 风起\n这是一段中文正文。\n第二章 雪落\n故事继续。"
    path.write_bytes(content.encode(encoding))

    document = load_source_document(path)

    assert "第一章" in document.text
    assert "故事继续" in document.text
    assert document.replacement_char_count == 0
    assert len(document.lines) == 4


def test_empty_file_is_rejected(tmp_path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("  \n", encoding="utf-8")

    with pytest.raises(ValueError, match="为空"):
        load_source_document(path)
