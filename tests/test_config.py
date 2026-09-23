from __future__ import annotations

import pytest

from novel_agent.config import ModelConfig, structured_output_retries


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


def test_structured_retries_default_and_boundaries(monkeypatch) -> None:
    monkeypatch.delenv("NOVEL_AGENT_STRUCTURED_RETRIES", raising=False)
    assert structured_output_retries() == 2

    for value in (0, 5):
        monkeypatch.setenv("NOVEL_AGENT_STRUCTURED_RETRIES", str(value))
        assert structured_output_retries() == value


@pytest.mark.parametrize("value", ["-1", "6", "many"])
def test_invalid_structured_retries_are_rejected(monkeypatch, value: str) -> None:
    monkeypatch.setenv("NOVEL_AGENT_STRUCTURED_RETRIES", value)

    with pytest.raises(RuntimeError, match="0 到 5"):
        structured_output_retries()
