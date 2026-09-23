from __future__ import annotations

import pytest

from novel_agent.config import ModelConfig


def test_deepseek_defaults_to_disabled_thinking(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_NAME", "deepseek-flash")
    monkeypatch.setenv("MODEL_THINKING_MODE", "")

    config = ModelConfig.from_env()

    assert config.thinking_mode == "disabled"


def test_non_deepseek_does_not_send_thinking_parameter(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_NAME", "gpt-4.1-mini")
    monkeypatch.setenv("MODEL_THINKING_MODE", "")

    config = ModelConfig.from_env()

    assert config.thinking_mode is None


def test_invalid_thinking_mode_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_NAME", "deepseek-flash")
    monkeypatch.setenv("MODEL_THINKING_MODE", "sometimes")

    with pytest.raises(RuntimeError, match="enabled、disabled"):
        ModelConfig.from_env()
