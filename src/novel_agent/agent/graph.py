"""Build the LangGraph workflow for evidence-based novel research."""

from __future__ import annotations

import hashlib

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from novel_agent.agent.nodes import AgentNodes
from novel_agent.agent.routing import route_after_assessor, route_after_researcher
from novel_agent.agent.state import AgentState
from novel_agent.corpus.repository import NovelCorpus
from novel_agent.runtime.structured_output import DiagnosticCallback
from novel_agent.runtime.tracing import ModelUsageCallback


def build_agent_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    corpus: NovelCorpus,
    *,
    checkpointer: InMemorySaver | None = None,
    structured_retries: int | None = None,
    on_diagnostic: DiagnosticCallback | None = None,
    on_model_usage: ModelUsageCallback | None = None,
):
    """组装并编译 LangGraph；checkpointer 让同一 thread_id 可读取最新状态。"""

    nodes = AgentNodes(
        model=model,
        tools=tools,
        corpus=corpus,
        structured_retries=structured_retries,
        on_diagnostic=on_diagnostic,
        on_model_usage=on_model_usage,
    )
    builder = StateGraph(AgentState)
    builder.add_node("planner", nodes.planner)
    builder.add_node("researcher", nodes.researcher)
    builder.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    builder.add_node("assessor", nodes.assessor)
    builder.add_node("replanner", nodes.replanner)
    builder.add_node("writer", nodes.writer)

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "researcher")
    builder.add_conditional_edges("researcher", route_after_researcher)
    builder.add_edge("tools", "assessor")
    builder.add_conditional_edges("assessor", route_after_assessor)
    builder.add_edge("replanner", "researcher")
    builder.add_edge("writer", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())


def stable_thread_id(novel_path: str, question: str) -> str:
    """为相同小说和问题生成可复现的短线程 ID。"""

    digest = hashlib.sha256(f"{novel_path}\n{question}".encode("utf-8")).hexdigest()
    return digest[:16]
