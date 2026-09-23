from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from novel_agent.models import (
    FinalAnswer,
    ObservationOutput,
    PlanOutput,
    ReplanOutput,
    ReviewResult,
)
from novel_agent.prompts import (
    CHECKER_PROMPT,
    OBSERVER_PROMPT,
    PLANNER_PROMPT,
    REPLANNER_PROMPT,
    RESEARCHER_PROMPT,
    WRITER_PROMPT,
)
from novel_agent.repository import NovelCorpus
from novel_agent.state import AgentState


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _as_model(value: Any, model_type: type[Any]) -> Any:
    if isinstance(value, model_type):
        return value
    return model_type.model_validate(value)


def _current_task(state: AgentState) -> dict[str, Any] | None:
    task_id = state.get("current_task_id")
    for task in state.get("plan", []):
        if task.get("task_id") == task_id:
            return task
    return None


def _first_pending_task_id(plan: list[dict[str, Any]]) -> str | None:
    for task in plan:
        if task.get("status") in {"pending", "in_progress"}:
            return str(task.get("task_id"))
    return None


def _tool_call_fingerprints(message: AIMessage) -> list[str]:
    return [
        f"{call.get('name')}:{json.dumps(call.get('args', {}), ensure_ascii=False, sort_keys=True)}"
        for call in message.tool_calls
    ]


def _normalized_quote(value: str) -> str:
    return re.sub(r"\s+", "", value).strip("，。；：、,.!！?？\"'“”‘’")


def _research_history(messages: list[Any]) -> list[Any]:
    """Keep the user question and the latest complete AI/tool exchange.

    Cutting a message list by count can leave a ToolMessage without its preceding
    AIMessage(tool_calls=...), which OpenAI-compatible APIs reject.
    """
    first_human = next((message for message in messages if isinstance(message, HumanMessage)), None)
    last_call_index = -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            last_call_index = index
            break
    if last_call_index < 0:
        return [first_human] if first_human is not None else []
    recent = list(messages[last_call_index:])
    if first_human is not None and first_human not in recent:
        return [first_human, *recent]
    return recent


class AgentNodes:
    def __init__(self, model: BaseChatModel, tools: list[BaseTool], corpus: NovelCorpus):
        self.model = model
        self.corpus = corpus
        self.research_model = model.bind_tools(tools)
        # Function-calling mode works with more OpenAI-compatible providers than
        # provider-native JSON schema mode while still giving Pydantic validation.
        self.plan_model = model.with_structured_output(PlanOutput, method="function_calling")
        self.observe_model = model.with_structured_output(ObservationOutput, method="function_calling")
        self.review_model = model.with_structured_output(ReviewResult, method="function_calling")
        self.replan_model = model.with_structured_output(ReplanOutput, method="function_calling")
        self.writer_model = model.with_structured_output(FinalAnswer, method="function_calling")

    def planner(self, state: AgentState) -> dict[str, Any]:
        output = _as_model(
            self.plan_model.invoke(
                [
                    SystemMessage(content=PLANNER_PROMPT),
                    ("human", state["question"]),
                ]
            ),
            PlanOutput,
        )
        tasks = [task.model_dump() for task in output.tasks[:5]]
        if not tasks:
            tasks = [
                {
                    "task_id": "T1",
                    "description": "查找与问题直接相关的原文",
                    "status": "pending",
                }
            ]
        tasks[0]["status"] = "in_progress"
        return {"plan": tasks, "current_task_id": tasks[0]["task_id"]}

    def researcher(self, state: AgentState) -> dict[str, Any]:
        if state["step_count"] >= state["max_steps"]:
            return {
                "messages": [AIMessage(content="已达到最大调查步数，进入证据审查。")],
                "termination_reason": "max_steps_reached",
            }

        context = {
            "question": state["question"],
            "current_task": _current_task(state),
            "plan": state["plan"],
            "verified_evidence": [
                {
                    "evidence_id": item.get("evidence_id"),
                    "task_id": item.get("task_id"),
                    "claim": item.get("claim"),
                    "chunk_id": item.get("chunk_id"),
                    "supports": item.get("supports"),
                }
                for item in state["evidence"][-20:]
            ],
            "unresolved_questions": state["unresolved_questions"],
            "suggested_queries": state["suggested_queries"],
            "remaining_steps": state["max_steps"] - state["step_count"],
            "recent_tool_calls": state["tool_call_history"][-5:],
        }
        recent_messages = _research_history(state["messages"])
        response = self.research_model.invoke(
            [SystemMessage(content=RESEARCHER_PROMPT + "\n\n当前状态：\n" + _json(context)), *recent_messages]
        )
        if not isinstance(response, AIMessage):
            response = AIMessage(content=str(response))
        if len(response.tool_calls) > 2:
            response = response.model_copy(update={"tool_calls": response.tool_calls[:2]})

        fingerprints = _tool_call_fingerprints(response)
        recent_fingerprints = [item.get("fingerprint") for item in state["tool_call_history"][-2:]]
        if fingerprints and all(recent_fingerprints.count(item) >= 2 for item in fingerprints):
            return {
                "messages": [AIMessage(content="检测到连续重复工具调用，进入证据审查。")],
                "step_count": state["step_count"] + 1,
                "termination_reason": "repeated_tool_call",
            }
        return {"messages": [response], "step_count": state["step_count"] + 1}

    def observe(self, state: AgentState) -> dict[str, Any]:
        last_call_index = -1
        for index in range(len(state["messages"]) - 1, -1, -1):
            message = state["messages"][index]
            if isinstance(message, AIMessage) and message.tool_calls:
                last_call_index = index
                break
        tool_messages = [
            message
            for message in state["messages"][last_call_index + 1 :]
            if isinstance(message, ToolMessage)
        ]
        if not tool_messages:
            return {}
        current_task = _current_task(state)
        prompt_data = {
            "question": state["question"],
            "current_task": current_task,
            "existing_evidence": state["evidence"][-20:],
            "existing_hypotheses": state["hypotheses"],
            "tool_results": [
                {"tool_name": message.name, "tool_result": message.content}
                for message in tool_messages
            ],
        }
        output = _as_model(
            self.observe_model.invoke(
                [SystemMessage(content=OBSERVER_PROMPT), ("human", _json(prompt_data))]
            ),
            ObservationOutput,
        )

        existing_ids = {item["evidence_id"] for item in state["evidence"]}
        existing_quotes = {
            _normalized_quote(str(item.get("quote", "")))
            for item in state["evidence"]
            if _normalized_quote(str(item.get("quote", "")))
        }
        evidence = list(state["evidence"])
        rejected_quotes: list[str] = []
        current_task_id = state.get("current_task_id") or "unassigned"
        current_count = sum(item.get("task_id") == current_task_id for item in evidence)
        remaining_capacity = max(0, state["max_evidence_per_task"] - current_count)
        for item in output.evidence:
            if remaining_capacity <= 0:
                break
            if item.evidence_id in existing_ids:
                continue
            normalized_quote = _normalized_quote(item.quote)
            if not normalized_quote or normalized_quote in existing_quotes:
                continue
            try:
                valid = self.corpus.validate_quote(item.chunk_id, item.quote)
            except KeyError:
                valid = False
            if not valid:
                rejected_quotes.append(item.evidence_id)
                continue
            evidence_item = item.model_dump()
            evidence_item["task_id"] = current_task_id
            evidence.append(evidence_item)
            existing_ids.add(item.evidence_id)
            existing_quotes.add(normalized_quote)
            remaining_capacity -= 1

        hypothesis_by_id = {item["hypothesis_id"]: item for item in state["hypotheses"]}
        for hypothesis in output.hypotheses:
            hypothesis_by_id[hypothesis.hypothesis_id] = hypothesis.model_dump()

        history = list(state["tool_call_history"])
        calls_by_id: dict[str, dict[str, Any]] = {}
        if last_call_index >= 0:
            call_message = state["messages"][last_call_index]
            if isinstance(call_message, AIMessage):
                calls_by_id = {str(call.get("id")): call for call in call_message.tool_calls}
        for tool_message in tool_messages:
            matching_call = calls_by_id.get(str(tool_message.tool_call_id), {})
            fingerprint = (
                f"{matching_call.get('name')}:{json.dumps(matching_call.get('args', {}), ensure_ascii=False, sort_keys=True)}"
                if matching_call
                else f"{tool_message.name}:unknown"
            )
            history.append(
                {
                    "tool": tool_message.name,
                    "args": matching_call.get("args", {}),
                    "fingerprint": fingerprint,
                    "result_summary": str(tool_message.content)[:400],
                    "rejected_evidence_ids": rejected_quotes,
                }
            )
        unresolved = list(dict.fromkeys([*state["unresolved_questions"], *output.unresolved_questions]))
        task_attempts = dict(state["task_attempts"])
        task_attempts[current_task_id] = task_attempts.get(current_task_id, 0) + 1
        return {
            "evidence": evidence,
            "hypotheses": list(hypothesis_by_id.values()),
            "unresolved_questions": unresolved,
            "tool_call_history": history,
            "task_attempts": task_attempts,
        }

    def checker(self, state: AgentState) -> dict[str, Any]:
        prompt_data = {
            "question": state["question"],
            "plan": state["plan"],
            "current_task_id": state["current_task_id"],
            "evidence": state["evidence"],
            "hypotheses": state["hypotheses"],
            "unresolved_questions": state["unresolved_questions"],
            "steps": {"used": state["step_count"], "maximum": state["max_steps"]},
        }
        output = _as_model(
            self.review_model.invoke(
                [SystemMessage(content=CHECKER_PROMPT), ("human", _json(prompt_data))]
            ),
            ReviewResult,
        )
        plan = [dict(task) for task in state["plan"]]
        completed = set(output.completed_task_ids)
        for task in plan:
            if task["task_id"] in completed:
                task["status"] = "completed"

        current_task_id = state.get("current_task_id")
        current_task = next(
            (task for task in plan if task.get("task_id") == current_task_id), None
        )
        if (
            current_task is not None
            and current_task.get("status") == "in_progress"
            and state["task_attempts"].get(str(current_task_id), 0) >= state["max_task_attempts"]
        ):
            has_evidence = any(
                item.get("task_id") == current_task_id for item in state["evidence"]
            )
            current_task["status"] = "completed" if has_evidence else "blocked"

        next_task_id = output.next_task_id
        available_ids = {
            task["task_id"]
            for task in plan
            if task.get("status") in {"pending", "in_progress"}
        }
        if next_task_id not in available_ids:
            next_task_id = _first_pending_task_id(plan)
        for task in plan:
            if task["task_id"] == next_task_id and task["status"] == "pending":
                task["status"] = "in_progress"

        all_tasks_resolved = all(
            task.get("status") in {"completed", "blocked"} for task in plan
        )
        effective_sufficient = output.sufficient and all_tasks_resolved
        review = output.model_dump()
        review["sufficient"] = effective_sufficient
        if output.sufficient and not all_tasks_resolved:
            review["rationale"] = (
                output.rationale + "；仍有未完成计划任务，因此继续调查。"
            ).strip("；")

        termination = state.get("termination_reason")
        if effective_sufficient:
            termination = "evidence_sufficient"
        elif state["step_count"] >= state["max_steps"]:
            termination = "max_steps_reached"
        return {
            "plan": plan,
            "current_task_id": next_task_id,
            "suggested_queries": output.suggested_queries,
            "review": review,
            "termination_reason": termination,
        }

    def replanner(self, state: AgentState) -> dict[str, Any]:
        prompt_data = {
            "question": state["question"],
            "plan": state["plan"],
            "evidence": state["evidence"],
            "hypotheses": state["hypotheses"],
            "review": state["review"],
        }
        output = _as_model(
            self.replan_model.invoke(
                [SystemMessage(content=REPLANNER_PROMPT), ("human", _json(prompt_data))]
            ),
            ReplanOutput,
        )
        tasks = [task.model_dump() for task in output.tasks[:6]]
        previous_status = {task["task_id"]: task["status"] for task in state["plan"]}
        for task in tasks:
            if previous_status.get(task["task_id"]) in {"completed", "blocked"}:
                task["status"] = previous_status[task["task_id"]]
        next_task_id = _first_pending_task_id(tasks)
        if next_task_id:
            for task in tasks:
                if task["task_id"] == next_task_id:
                    task["status"] = "in_progress"
                    break
        return {
            "plan": tasks,
            "current_task_id": next_task_id,
            "replan_count": state["replan_count"] + 1,
        }

    def writer(self, state: AgentState) -> dict[str, Any]:
        prompt_data = {
            "question": state["question"],
            "plan": state["plan"],
            "evidence": state["evidence"],
            "hypotheses": state["hypotheses"],
            "review": state["review"],
            "termination_reason": state["termination_reason"],
        }
        output = _as_model(
            self.writer_model.invoke(
                [SystemMessage(content=WRITER_PROMPT), ("human", _json(prompt_data))]
            ),
            FinalAnswer,
        )
        return {"final_answer": output.answer, "limitations": output.limitations}


def route_after_researcher(state: AgentState) -> Literal["tools", "checker"]:
    if state.get("termination_reason") in {"max_steps_reached", "repeated_tool_call"}:
        return "checker"
    last_message = state["messages"][-1]
    return "tools" if isinstance(last_message, AIMessage) and last_message.tool_calls else "checker"


def route_after_checker(state: AgentState) -> Literal["researcher", "replanner", "writer"]:
    review = state.get("review") or {}
    if (
        review.get("sufficient")
        or state["step_count"] >= state["max_steps"]
        or state.get("termination_reason") in {"max_steps_reached", "repeated_tool_call"}
    ):
        return "writer"
    if review.get("should_replan") and state["replan_count"] < state["max_replans"]:
        return "replanner"
    return "researcher"


def build_agent_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    corpus: NovelCorpus,
    *,
    checkpointer: InMemorySaver | None = None,
):
    nodes = AgentNodes(model=model, tools=tools, corpus=corpus)
    builder = StateGraph(AgentState)
    builder.add_node("planner", nodes.planner)
    builder.add_node("researcher", nodes.researcher)
    builder.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    builder.add_node("observe", nodes.observe)
    builder.add_node("checker", nodes.checker)
    builder.add_node("replanner", nodes.replanner)
    builder.add_node("writer", nodes.writer)

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "researcher")
    builder.add_conditional_edges("researcher", route_after_researcher)
    builder.add_edge("tools", "observe")
    builder.add_edge("observe", "checker")
    builder.add_conditional_edges("checker", route_after_checker)
    builder.add_edge("replanner", "researcher")
    builder.add_edge("writer", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())


def stable_thread_id(novel_path: str, question: str) -> str:
    digest = hashlib.sha256(f"{novel_path}\n{question}".encode("utf-8")).hexdigest()
    return digest[:16]
