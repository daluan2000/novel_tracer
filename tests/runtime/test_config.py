from __future__ import annotations

import pytest

from novel_agent.runtime.config import (
    EmbeddingConfig,
    ModelConfig,
    novel_agent_data_dir,
    structured_output_retries,
    graph_max_output_tokens,
    graph_request_retries,
    graph_request_timeout_seconds,
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


def test_graph_generation_limits_and_chat_defaults_are_independent(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    for name in ("NOVEL_AGENT_GRAPH_REQUEST_TIMEOUT_SECONDS", "NOVEL_AGENT_GRAPH_REQUEST_RETRIES", "NOVEL_AGENT_GRAPH_MAX_OUTPUT_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    assert graph_request_timeout_seconds(120) == 180
    assert graph_request_timeout_seconds(300) == 300
    assert graph_request_retries() == 1
    assert graph_max_output_tokens() == 6144
    config = ModelConfig(api_key="test-key", model_name="gpt-4.1-mini")
    model = config.create_model(max_retries=0, max_tokens=graph_max_output_tokens())
    assert model.max_tokens == 6144 and model.max_retries == 0
    assert config.request_timeout_seconds == 120
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_REQUEST_TIMEOUT_SECONDS", "240")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_REQUEST_RETRIES", "2")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_MAX_OUTPUT_TOKENS", "4096")
    assert graph_request_timeout_seconds() == 240
    assert graph_request_retries() == 2
    assert graph_max_output_tokens() == 4096


def test_qwen_graph_output_cap_uses_max_tokens_on_http_wire(monkeypatch):
    import json
    import httpx
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test-response", "object": "chat.completion", "created": 0,
            "model": "qwen3.7-flash", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr("novel_agent.runtime.config.ChatOpenAI", lambda **kwargs: ChatOpenAI(http_client=client, **kwargs))
        config = ModelConfig(api_key="test-key", model_name="qwen3.7-flash", base_url="https://example.invalid/v1", thinking_mode="disabled")
        model = config.create_model(max_retries=0, max_tokens=6144)
        model.invoke([HumanMessage(content="test")])
    assert requests[0]["max_tokens"] == 6144
    assert "max_completion_tokens" not in requests[0]
    assert requests[0]["enable_thinking"] is False


@pytest.mark.parametrize("key,value", [
    ("NOVEL_AGENT_GRAPH_REQUEST_TIMEOUT_SECONDS", "nan"),
    ("NOVEL_AGENT_GRAPH_REQUEST_TIMEOUT_SECONDS", "601"),
    ("NOVEL_AGENT_GRAPH_REQUEST_RETRIES", "-1"),
    ("NOVEL_AGENT_GRAPH_REQUEST_RETRIES", "4"),
    ("NOVEL_AGENT_GRAPH_REQUEST_RETRIES", "many"),
    ("NOVEL_AGENT_GRAPH_MAX_OUTPUT_TOKENS", "511"),
    ("NOVEL_AGENT_GRAPH_MAX_OUTPUT_TOKENS", "16385"),
    ("NOVEL_AGENT_GRAPH_MAX_OUTPUT_TOKENS", "many"),
])
def test_invalid_graph_generation_limits(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    operation = graph_request_timeout_seconds if "TIMEOUT" in key else graph_request_retries if "RETRIES" in key else graph_max_output_tokens
    with pytest.raises(RuntimeError):
        operation()
