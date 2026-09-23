from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage


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
    }
