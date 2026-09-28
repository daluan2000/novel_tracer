from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import BaseMessage

ModelUsageCallback = Callable[[dict[str, Any]], None]


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def model_usage_event(response: Any, source_node: str, elapsed_seconds: float) -> dict[str, Any]:
    """Normalize LangChain and OpenAI-compatible usage metadata without response content."""

    raw = response.get("raw") if isinstance(response, dict) and "raw" in response else response
    usage = getattr(raw, "usage_metadata", None) or {}
    response_metadata = getattr(raw, "response_metadata", None) or {}
    provider_usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}

    input_details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}
    prompt_details = provider_usage.get("prompt_tokens_details") or {}
    completion_details = provider_usage.get("completion_tokens_details") or {}

    input_tokens = _integer(usage.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _integer(provider_usage.get("prompt_tokens"))
    output_tokens = _integer(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _integer(provider_usage.get("completion_tokens"))
    total_tokens = _integer(usage.get("total_tokens"))
    if total_tokens is None:
        total_tokens = _integer(provider_usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    reasoning_tokens = _integer(output_details.get("reasoning"))
    if reasoning_tokens is None:
        reasoning_tokens = _integer(completion_details.get("reasoning_tokens"))
    cached_tokens = _integer(input_details.get("cache_read"))
    if cached_tokens is None:
        cached_tokens = _integer(prompt_details.get("cached_tokens"))

    return {
        "source_node": source_node,
        "elapsed_seconds": round(elapsed_seconds, 6),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
        "usage_reported": any(
            value is not None
            for value in (input_tokens, output_tokens, total_tokens, reasoning_tokens, cached_tokens)
        ),
    }


def empty_token_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "elapsed_seconds": 0.0,
        "reported_call_count": 0,
        "unknown_call_count": 0,
        "by_node": {},
    }


def add_model_usage(summary: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    updated = {
        **empty_token_usage(),
        **summary,
        "by_node": {key: dict(value) for key, value in summary.get("by_node", {}).items()},
    }
    node = str(event.get("source_node") or "unknown")
    node_summary = {
        **empty_token_usage(),
        **updated["by_node"].get(node, {}),
    }
    node_summary.pop("by_node", None)

    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_tokens",
    ):
        value = event.get(key)
        if isinstance(value, int):
            updated[key] += value
            node_summary[key] += value
    elapsed = event.get("elapsed_seconds")
    if isinstance(elapsed, (int, float)):
        updated["elapsed_seconds"] = round(updated["elapsed_seconds"] + elapsed, 6)
        node_summary["elapsed_seconds"] = round(node_summary["elapsed_seconds"] + elapsed, 6)
    count_key = "reported_call_count" if event.get("usage_reported") else "unknown_call_count"
    updated[count_key] += 1
    node_summary[count_key] += 1
    updated["by_node"][node] = node_summary
    return updated


def _serialize(value: Any) -> Any:
    if isinstance(value, BaseMessage):
        return {
            "type": value.type,
            "content": value.content,
            "name": getattr(value, "name", None),
            "tool_calls": getattr(value, "tool_calls", None),
            "tool_call_id": getattr(value, "tool_call_id", None),
        }
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if hasattr(value, "model_dump"):
        return _serialize(value.model_dump())
    return value


class TraceWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, node: str, update: dict[str, Any]) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "node": node,
            "update": _serialize(update),
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def run_metrics(state: dict[str, Any]) -> dict[str, Any]:
    history = state.get("tool_call_history", [])
    fingerprints = [item.get("fingerprint") for item in history]
    unique_ratio = len(set(fingerprints)) / len(fingerprints) if fingerprints else 0.0
    evidence = state.get("evidence", [])
    plan = state.get("plan", [])
    covered_tasks = {item.get("task_id") for item in evidence}
    return {
        "final_answer_completed": bool(state.get("final_answer")),
        "tool_call_count": len(history),
        "unique_tool_call_ratio": round(unique_ratio, 3),
        "evidence_count": len(evidence),
        "counter_evidence_found": any(not item.get("supports", True) for item in evidence),
        "evidence_coverage": round(len(covered_tasks) / len(plan), 3) if plan else 0.0,
        "replan_count": state.get("replan_count", 0),
        "resolved_task_count": sum(
            task.get("status") in {"completed", "blocked"} for task in plan
        ),
        "step_count": state.get("step_count", 0),
        "termination_reason": state.get("termination_reason"),
        "structured_retry_count": state.get("structured_retry_count", 0),
        "content_fallback_count": state.get("content_fallback_count", 0),
        "model_call_count": state.get("model_call_count", 0),
        "token_usage": state.get("token_usage") or empty_token_usage(),
    }
