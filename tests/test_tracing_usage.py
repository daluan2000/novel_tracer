from __future__ import annotations

from langchain_core.messages import AIMessage

from novel_agent.tracing import add_model_usage, empty_token_usage, model_usage_event


def test_model_usage_is_normalized_and_aggregated_by_node() -> None:
    response = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 30},
            "output_token_details": {"reasoning": 5},
        },
    )

    event = model_usage_event(response, "researcher", 0.25)
    summary = add_model_usage(empty_token_usage(), event)

    assert event["usage_reported"] is True
    assert summary["total_tokens"] == 120
    assert summary["cached_tokens"] == 30
    assert summary["reasoning_tokens"] == 5
    assert summary["reported_call_count"] == 1
    assert summary["by_node"]["researcher"]["input_tokens"] == 100


def test_missing_usage_is_counted_as_unknown_not_zero_usage() -> None:
    event = model_usage_event(AIMessage(content="ok"), "writer", 0.1)
    summary = add_model_usage(empty_token_usage(), event)

    assert event["usage_reported"] is False
    assert summary["unknown_call_count"] == 1
    assert summary["reported_call_count"] == 0
