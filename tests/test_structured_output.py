from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from novel_agent.structured_output import StructuredOutputError, invoke_structured


class ExampleOutput(BaseModel):
    answer: str


class SequenceRunnable:
    def __init__(self, *responses: Any):
        self.responses = list(responses)
        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> Any:
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def envelope(*, parsed: Any = None, content: str = "", error: Exception | None = None):
    return {
        "raw": AIMessage(content=content),
        "parsed": parsed,
        "parsing_error": error,
    }


def invoke(runnable: SequenceRunnable, **kwargs: Any):
    return invoke_structured(
        runnable,
        [HumanMessage(content="question")],
        ExampleOutput,
        source_node="observe",
        sleeper=lambda _: None,
        **kwargs,
    )


def test_structured_output_succeeds_without_retry() -> None:
    runnable = SequenceRunnable(envelope(parsed=ExampleOutput(answer="ok")))

    result = invoke(runnable, retries=2)

    assert result.value.answer == "ok"
    assert result.retry_count == 0
    assert len(runnable.calls) == 1


@pytest.mark.parametrize(
    ("invalid", "expected_reason"),
    [
        (None, "empty_response"),
        (envelope(), "empty_response"),
        (envelope(content="ordinary prose"), "missing_tool_call"),
        (envelope(content="{}", error=ValueError("invalid")), "schema_validation_error"),
    ],
)
def test_invalid_completed_response_retries(
    invalid: Any, expected_reason: str
) -> None:
    diagnostics: list[dict[str, Any]] = []
    runnable = SequenceRunnable(invalid, envelope(parsed={"answer": "recovered"}))

    result = invoke(runnable, retries=1, on_diagnostic=diagnostics.append)

    assert result.value.answer == "recovered"
    assert result.retry_count == 1
    assert diagnostics[0]["diagnostic_code"] == "structured_output_retry"
    assert diagnostics[0]["failure_reason"] == expected_reason
    assert isinstance(runnable.calls[1][-1], HumanMessage)
    assert "ExampleOutput" in runnable.calls[1][-1].content


@pytest.mark.parametrize(
    "content",
    [
        '{"answer": "from content"}',
        '```json\n{"answer": "from content"}\n```',
    ],
)
def test_schema_valid_content_json_is_accepted(content: str) -> None:
    diagnostics: list[dict[str, Any]] = []
    runnable = SequenceRunnable(envelope(content=content))

    result = invoke(runnable, retries=2, on_diagnostic=diagnostics.append)

    assert result.value.answer == "from content"
    assert result.content_fallback_count == 1
    assert diagnostics[0]["diagnostic_code"] == "content_json_fallback"
    assert "raw" not in diagnostics[0]


def test_mixed_prose_and_json_is_not_accepted() -> None:
    runnable = SequenceRunnable(envelope(content='result: {"answer": "no"}'))

    with pytest.raises(StructuredOutputError) as raised:
        invoke(runnable, retries=0)

    assert raised.value.failure_reason == "missing_tool_call"
    assert raised.value.attempts == 1


def test_content_json_does_not_override_an_invalid_tool_call() -> None:
    response = envelope(content='{"answer": "must not be accepted"}')
    response["raw"] = AIMessage(
        content='{"answer": "must not be accepted"}',
        tool_calls=[{"name": "ExampleOutput", "args": {}, "id": "call-1"}],
    )
    response["parsing_error"] = ValueError("invalid tool arguments")
    runnable = SequenceRunnable(response)

    with pytest.raises(StructuredOutputError) as raised:
        invoke(runnable, retries=0)

    assert raised.value.failure_reason == "schema_validation_error"


def test_retry_exhaustion_is_safe_and_bounded() -> None:
    diagnostics: list[dict[str, Any]] = []
    runnable = SequenceRunnable(None, None, None)

    with pytest.raises(StructuredOutputError) as raised:
        invoke(runnable, retries=2, on_diagnostic=diagnostics.append)

    assert len(runnable.calls) == 3
    assert raised.value.schema_name == "ExampleOutput"
    assert [item["diagnostic_code"] for item in diagnostics] == [
        "structured_output_retry",
        "structured_output_retry",
        "structured_output_failed",
    ]
    assert all("content" not in item and "raw" not in item for item in diagnostics)


def test_provider_exception_is_not_retried() -> None:
    runnable = SequenceRunnable(RuntimeError("Insufficient Balance"))

    with pytest.raises(RuntimeError, match="Insufficient Balance"):
        invoke(runnable, retries=5)

    assert len(runnable.calls) == 1
