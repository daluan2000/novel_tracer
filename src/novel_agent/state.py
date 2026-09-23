from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    question: str
    plan: list[dict[str, Any]]
    current_task_id: str | None
    task_attempts: dict[str, int]
    max_task_attempts: int
    evidence: list[dict[str, Any]]
    max_evidence_per_task: int
    hypotheses: list[dict[str, Any]]
    unresolved_questions: list[str]
    suggested_queries: list[str]
    step_count: int
    max_steps: int
    replan_count: int
    max_replans: int
    tool_call_history: list[dict[str, Any]]
    review: dict[str, Any] | None
    termination_reason: str | None
    final_answer: str | None
    limitations: list[str]
    structured_retry_count: int
    content_fallback_count: int


def initial_state(question: str, max_steps: int) -> AgentState:
    return {
        "messages": [HumanMessage(content=question)],
        "question": question,
        "plan": [],
        "current_task_id": None,
        "task_attempts": {},
        "max_task_attempts": 2,
        "evidence": [],
        "max_evidence_per_task": 6,
        "hypotheses": [],
        "unresolved_questions": [],
        "suggested_queries": [],
        "step_count": 0,
        "max_steps": max_steps,
        "replan_count": 0,
        "max_replans": 2,
        "tool_call_history": [],
        "review": None,
        "termination_reason": None,
        "final_answer": None,
        "limitations": [],
        "structured_retry_count": 0,
        "content_fallback_count": 0,
    }
