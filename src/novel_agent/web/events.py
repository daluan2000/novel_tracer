"""Translate internal agent state into stable public web events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from novel_agent.runtime.tracing import run_metrics

TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
NODE_LABELS = {
    "planner": "制定调查计划",
    "researcher": "选择检索动作",
    "tools": "读取小说原文",
    "assessor": "整理并审查证据",
    "replanner": "调整调查计划",
    "writer": "生成最终解读",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan": state.get("plan", []),
        "current_task_id": state.get("current_task_id"),
        "evidence": state.get("evidence", []),
        "hypotheses": state.get("hypotheses", []),
        "unresolved_questions": state.get("unresolved_questions", []),
        "suggested_queries": state.get("suggested_queries", []),
        "review": state.get("review"),
        "step_count": state.get("step_count", 0),
        "max_steps": state.get("max_steps", 0),
        "replan_count": state.get("replan_count", 0),
        "termination_reason": state.get("termination_reason"),
        "final_answer": state.get("final_answer"),
        "limitations": state.get("limitations", []),
        "metrics": run_metrics(state),
    }


def event_detail(
    node: str, update: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    detail: dict[str, Any] = {"label": NODE_LABELS.get(node, node)}
    if node == "planner":
        detail["task_count"] = len(snapshot["plan"])
    elif node == "researcher":
        calls: list[dict[str, Any]] = []
        for message in update.get("messages") or []:
            for call in getattr(message, "tool_calls", None) or []:
                calls.append({"name": call.get("name"), "args": call.get("args", {})})
        detail["tool_calls"] = calls
    elif node == "tools":
        detail["tools"] = [
            getattr(message, "name", None)
            for message in update.get("messages") or []
            if getattr(message, "name", None)
        ]
    elif node == "assessor":
        detail["evidence_count"] = len(snapshot["evidence"])
        review = snapshot.get("review") or {}
        detail["sufficient"] = review.get("sufficient")
        detail["rationale"] = review.get("rationale", "")
    elif node == "replanner":
        detail["replan_count"] = snapshot["replan_count"]
    elif node == "writer":
        detail["answer_ready"] = bool(snapshot.get("final_answer"))
    return detail
