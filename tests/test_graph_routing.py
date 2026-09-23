from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from novel_agent.graph import (
    AgentNodes,
    _research_history,
    build_agent_graph,
    route_after_checker,
    route_after_researcher,
)
from novel_agent.models import FinalAnswer, ObservationOutput, PlanOutput, ReplanOutput, ReviewResult
from novel_agent.repository import NovelCorpus
from novel_agent.service import execute_agent
from novel_agent.state import initial_state
from novel_agent.tools import build_tools


class ScriptedChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "scripted-test-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "search_novel",
                                "args": {"keyword": "人物", "top_k": 2},
                                "id": "scripted-call",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> RunnableLambda:
        def produce(_: Any) -> Any:
            if schema is PlanOutput:
                return PlanOutput(
                    tasks=[{"task_id": "T1", "description": "查找人物", "status": "pending"}]
                )
            if schema is ObservationOutput:
                return ObservationOutput(decision_summary="完成一次搜索")
            if schema is ReviewResult:
                return ReviewResult(sufficient=True, completed_task_ids=["T1"])
            if schema is ReplanOutput:
                return ReplanOutput(tasks=[], rationale="无需重规划")
            if schema is FinalAnswer:
                return FinalAnswer(answer="基于现有证据完成。")
            raise AssertionError(f"unexpected schema: {schema}")

        return RunnableLambda(produce)


def test_researcher_routes_to_tools_for_tool_call() -> None:
    state = initial_state("问题", 10)
    state["messages"] = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_novel",
                    "args": {"keyword": "人物"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        )
    ]

    assert route_after_researcher(state) == "tools"


def test_researcher_routes_to_checker_without_tool_call() -> None:
    state = initial_state("问题", 10)
    state["messages"] = [AIMessage(content="信息可能已经足够。")]

    assert route_after_researcher(state) == "checker"


def test_checker_routes_by_review_and_budget() -> None:
    state = initial_state("问题", 10)
    state["review"] = {"sufficient": True, "should_replan": False}
    assert route_after_checker(state) == "writer"

    state["review"] = {"sufficient": False, "should_replan": True}
    assert route_after_checker(state) == "replanner"

    state["review"] = {"sufficient": False, "should_replan": False}
    assert route_after_checker(state) == "researcher"

    state["step_count"] = 10
    assert route_after_checker(state) == "writer"

    state["step_count"] = 1
    state["termination_reason"] = "repeated_tool_call"
    assert route_after_checker(state) == "writer"


def test_full_graph_executes_tool_loop_with_scripted_model(tmp_path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("第一章 开始\n\n人物在这里出现。" * 100, encoding="utf-8")
    corpus = NovelCorpus.from_path(path)
    tools = build_tools(corpus)
    graph = build_agent_graph(ScriptedChatModel(), tools, corpus)
    config = {"configurable": {"thread_id": "test-thread"}}

    result = graph.invoke(initial_state("人物在哪里出现？", 5), config=config)

    assert result["final_answer"] == "基于现有证据完成。"
    assert len(result["tool_call_history"]) == 1
    assert result["tool_call_history"][0]["tool"] == "search_novel"
    assert result["termination_reason"] == "evidence_sufficient"


def test_shared_execution_service_streams_updates(tmp_path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("第一章 开始\n\n人物在这里出现。" * 100, encoding="utf-8")
    corpus = NovelCorpus.from_path(path)
    seen_nodes: list[str] = []

    result = execute_agent(
        corpus,
        "人物在哪里出现？",
        max_steps=5,
        thread_id="service-test",
        model=ScriptedChatModel(),
        on_update=lambda node, _update, _state: seen_nodes.append(node),
    )

    assert result.cancelled is False
    assert result.state["final_answer"] == "基于现有证据完成。"
    assert seen_nodes == ["planner", "researcher", "tools", "observe", "checker", "writer"]


def test_research_history_preserves_latest_tool_pair() -> None:
    messages: list[BaseMessage] = [HumanMessage(content="问题")]
    for index in range(8):
        call_id = f"call-{index}"
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "search_novel",
                            "args": {"keyword": str(index)},
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="结果", tool_call_id=call_id, name="search_novel"),
            ]
        )

    recent = _research_history(messages)

    assert isinstance(recent[0], HumanMessage)
    assert isinstance(recent[1], AIMessage)
    assert recent[1].tool_calls[0]["id"] == "call-7"
    assert isinstance(recent[2], ToolMessage)
    assert recent[2].tool_call_id == "call-7"


def test_checker_advances_task_after_attempt_budget(tmp_path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("第一章 开始\n\n人物在这里出现。" * 100, encoding="utf-8")
    corpus = NovelCorpus.from_path(path)
    nodes = AgentNodes(ScriptedChatModel(), build_tools(corpus), corpus)
    nodes.review_model = RunnableLambda(
        lambda _: ReviewResult(sufficient=False, should_replan=False)
    )
    state = initial_state("分析人物变化", 8)
    state["plan"] = [
        {"task_id": "T1", "description": "早期", "status": "in_progress"},
        {"task_id": "T2", "description": "后期", "status": "pending"},
    ]
    state["current_task_id"] = "T1"
    state["task_attempts"] = {"T1": 2}
    state["evidence"] = [
        {
            "evidence_id": "E1",
            "task_id": "T1",
            "claim": "人物出现",
            "quote": "人物在这里出现",
            "section_title": "第一章 开始",
            "start_line": 1,
            "end_line": 2,
            "chunk_id": corpus.document.chunks[0].chunk_id,
            "supports": True,
            "interpretation": "早期证据",
        }
    ]

    update = nodes.checker(state)

    assert update["plan"][0]["status"] == "completed"
    assert update["plan"][1]["status"] == "in_progress"
    assert update["current_task_id"] == "T2"
