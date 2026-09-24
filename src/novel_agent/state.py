from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """LangGraph 中所有节点共享的状态（可以把它理解成 Agent 的工作白板）。

    每个节点只返回自己要修改的字段，LangGraph 会把返回值合并回这份状态。
    唯一特殊的是 ``messages``：它使用 ``add_messages`` reducer，因此新消息会
    追加到历史记录，而不是覆盖整个列表。
    """

    # 对话输入与模型/工具消息轨迹。
    messages: Annotated[list[AnyMessage], add_messages]
    question: str

    # Planner 生成的任务清单，以及 Researcher 当前正在处理的任务。
    question_mode: str
    plan: list[dict[str, Any]]
    current_task_id: str | None
    task_attempts: dict[str, int]
    max_task_attempts: int

    # Assessor 从工具结果中提取、并经过原文校验的调查材料。
    evidence: list[dict[str, Any]]
    max_evidence_per_task: int
    hypotheses: list[dict[str, Any]]
    unresolved_questions: list[str]
    suggested_queries: list[str]

    # 循环预算：step_count 统计 Researcher 回合，而不是图中所有节点的数量。
    step_count: int
    max_steps: int
    replan_count: int
    max_replans: int

    # 决策与停止信息。review 决定下一跳是继续调查、重规划还是写答案。
    tool_call_history: list[dict[str, Any]]
    review: dict[str, Any] | None
    termination_reason: str | None
    final_answer: str | None
    limitations: list[str]

    # 运行诊断指标，由 service.py 的回调汇总，不参与 Agent 的业务推理。
    structured_retry_count: int
    content_fallback_count: int
    model_call_count: int
    token_usage: dict[str, Any]


def initial_state(question: str, max_steps: int) -> AgentState:
    """为一次全新的调查创建状态；旧问题的证据不会带入新问题。"""

    return {
        "messages": [HumanMessage(content=question)],
        "question": question,
        "question_mode": "unknown",
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
        "model_call_count": 0,
        "token_usage": {},
    }
