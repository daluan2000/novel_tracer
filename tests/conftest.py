from __future__ import annotations

import pytest

from novel_agent.corpus.repository import NovelCorpus


@pytest.fixture
def corpus(tmp_path) -> NovelCorpus:
    body = "吕树在庙会上遇到了吕小鱼，两人一起讨论晚饭。" * 40
    later = "吕小鱼发现吕树隐瞒了计划，因此非常生气。" * 40
    path = tmp_path / "sample.txt"
    path.write_text(
        f"第一章 庙会\n\n{body}\n\n第二章 隐瞒\n\n{later}",
        encoding="utf-8",
    )
    return NovelCorpus.from_path(path)
