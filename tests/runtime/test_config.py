from __future__ import annotations

import pytest

from novel_agent.runtime.config import (
    EmbeddingConfig,
    ModelConfig,
    novel_agent_data_dir,
    structured_output_retries,
)


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
    assert config.request_timeout_seconds == 120.0


@pytest.mark.parametrize("value", ["off", "false", "disabled"])
def test_qwen_disabled_thinking_uses_boolean_parameter(monkeypatch, value: str) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("MODEL_NAME", "qwen-plus")
    monkeypatch.setenv("MODEL_THINKING_MODE", value)

    config = ModelConfig.from_env()
    model = config.create_model()

    assert config.thinking_mode == "disabled"
    assert model.extra_body == {"enable_thinking": False}


def test_deepseek_disabled_thinking_uses_typed_parameter(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("MODEL_NAME", "deepseek-chat")
    monkeypatch.setenv("MODEL_THINKING_MODE", "off")

    model = ModelConfig.from_env().create_model()

    assert model.extra_body == {"thinking": {"type": "disabled"}}


def test_qwen_detection_also_supports_qianwen_compatible_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://maas.qianwenaiapi.com/compatible-mode/v1")
    monkeypatch.setenv("MODEL_NAME", "provider-model-alias")
    monkeypatch.setenv("MODEL_THINKING_MODE", "enabled")

    model = ModelConfig.from_env().create_model()

    assert model.extra_body == {"enable_thinking": True}


def test_invalid_thinking_mode_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_NAME", "deepseek-flash")
    monkeypatch.setenv("MODEL_THINKING_MODE", "sometimes")

    with pytest.raises(RuntimeError, match="enabled/disabled"):
        ModelConfig.from_env()


@pytest.mark.parametrize("value", ["4", "601", "forever", "nan"])
def test_invalid_model_timeout_is_rejected(monkeypatch, value: str) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", value)

    with pytest.raises(RuntimeError, match="5 到 600"):
        ModelConfig.from_env()


def test_embedding_timeout_configuration(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_REQUEST_TIMEOUT_SECONDS", "45")

    config = EmbeddingConfig.from_env()

    assert config.enabled is True
    assert config.request_timeout_seconds == 45.0


def test_runtime_data_directory_configuration(monkeypatch, tmp_path) -> None:
    configured = tmp_path / "novel-data"
    monkeypatch.setenv("NOVEL_AGENT_DATA_DIR", str(configured))

    assert novel_agent_data_dir() == configured


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
