from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from novel_agent.corpus.repository import NovelCorpus


@pytest.fixture
def local_fixture_path() -> Callable[[str], Path]:
    """Skip only tests whose optional local reference data is unavailable."""

    def require_file(filename: str) -> Path:
        path = Path(__file__).parent / "fixtures" / filename
        if not path.is_file():
            pytest.skip(f"Local test fixture not found: tests/fixtures/{filename}")
        return path

    return require_file


@pytest.fixture(autouse=True)
def isolate_runtime_data(monkeypatch, tmp_path) -> None:
    """Keep tests isolated and ensure they never call a paid embedding API."""

    monkeypatch.setenv("EMBEDDING_MODEL", "")
    monkeypatch.setenv("NOVEL_AGENT_DATA_DIR", str(tmp_path / "runtime-data"))


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
