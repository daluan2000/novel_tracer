"""Pure helpers for compacting and interpreting agent context."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novel_agent.agent.state import AgentState

MAX_HYPOTHESES = 8


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def current_task(state: AgentState) -> dict[str, Any] | None:
    task_id = state.get("current_task_id")
    return next(
        (task for task in state.get("plan", []) if task.get("task_id") == task_id),
        None,
    )


def first_pending_task_id(plan: list[dict[str, Any]]) -> str | None:
    for task in plan:
        if task.get("status") in {"pending", "in_progress"}:
            return str(task.get("task_id"))
    return None


def tool_call_fingerprints(message: AIMessage) -> list[str]:
    return [
        f"{call.get('name')}:{json.dumps(call.get('args', {}), ensure_ascii=False, sort_keys=True)}"
        for call in message.tool_calls
    ]


def normalized_quote(value: str) -> str:
    return re.sub(r"\s+", "", value).strip("，。；：、,.!！?？\"'“”‘’")


def bounded_unique(values: list[str], limit: int) -> list[str]:
    unique = list(
        dict.fromkeys(value.strip() for value in values if value and value.strip())
    )
    return unique[-limit:]


def research_history(messages: list[Any]) -> list[Any]:
    """只保留原问题，避免把已经处理的大段工具结果反复发给模型。"""

    first_human = next(
        (message for message in messages if isinstance(message, HumanMessage)),
        None,
    )
    return [first_human] if first_human is not None else []


def evidence_summary(
    evidence: list[dict[str, Any]],
    *,
    limit: int | None = None,
    include_quote: bool = False,
) -> list[dict[str, Any]]:
    selected = evidence[-limit:] if limit is not None else evidence
    keys = ["evidence_id", "task_id", "claim", "chunk_id", "supports"]
    if include_quote:
        keys.extend(
            ["quote", "section_title", "start_line", "end_line", "interpretation"]
        )
    return [{key: item.get(key) for key in keys} for item in selected]


def hypothesis_summary(
    hypotheses: list[dict[str, Any]], limit: int = MAX_HYPOTHESES
) -> list[dict[str, Any]]:
    keys = ["hypothesis_id", "statement", "confidence", "status"]
    return [{key: item.get(key) for key in keys} for item in hypotheses[-limit:]]


def latest_tool_exchange(messages: list[Any]) -> tuple[int, list[ToolMessage]]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage):
            if not message.tool_calls:
                return -1, []
            return index, [
                candidate
                for candidate in messages[index + 1 :]
                if isinstance(candidate, ToolMessage)
            ]
    return -1, []


def decode_tool_result(content: Any) -> Any:
    if not isinstance(content, str):
        return content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return content


_COMPLEX_QUESTION_MARKERS = (
    "分析", "为什么", "为何", "如何体现", "变化", "成长", "关系", "动机", "原因",
    "意义", "象征", "主题", "对比", "评价", "解读", "发展", "矛盾", "影响", "阶段",
)


def is_simple_question(question: str) -> bool:
    normalized = re.sub(r"\s+", "", question)
    return len(normalized) <= 60 and not any(
        marker in normalized for marker in _COMPLEX_QUESTION_MARKERS
    )
