"""Conditional routing for the agent graph."""

from typing import Literal

from langchain_core.messages import AIMessage

from novel_agent.agent.state import AgentState


def route_after_researcher(state: AgentState) -> Literal["tools", "assessor"]:
    """Researcher 发出了 tool_calls 就执行工具，否则直接审查现有材料。"""

    if state.get("termination_reason") in {"max_steps_reached", "repeated_tool_call"}:
        return "assessor"
    last_message = state["messages"][-1]
    return (
        "tools"
        if isinstance(last_message, AIMessage) and last_message.tool_calls
        else "assessor"
    )


def route_after_assessor(
    state: AgentState,
) -> Literal["researcher", "replanner", "writer"]:
    """按“可重规划 -> 可结束 -> 继续检索”的优先级选择下一节点。"""

    review = state.get("review") or {}
    can_replan = (
        review.get("should_replan")
        and state.get("question_mode") == "complex"
        and state["replan_count"] < state["max_replans"]
        and state["step_count"] < state["max_steps"]
    )
    if can_replan:
        return "replanner"
    if (
        review.get("sufficient")
        or review.get("all_tasks_resolved")
        or state["step_count"] >= state["max_steps"]
        or state.get("termination_reason")
        in {"max_steps_reached", "repeated_tool_call", "tasks_resolved"}
    ):
        return "writer"
    return "researcher"
