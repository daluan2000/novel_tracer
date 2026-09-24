"""Agent 的核心状态图。

主流程如下：

    START -> planner -> researcher -> tools -> assessor
                           ^                    |
                           |                    +-> writer -> END
                           +---- replanner <----+

Researcher 也可以不调用工具而直接进入 Assessor。Assessor 是循环的决策中心：
它验证证据、更新任务状态，然后决定继续调查、重新规划或开始写最终答案。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from novel_agent.config import structured_output_retries as configured_structured_retries
from novel_agent.models import AssessmentOutput, FinalAnswer, PlanOutput, ReplanOutput
from novel_agent.prompts import (
    ASSESSOR_PROMPT,
    PLANNER_PROMPT,
    REPLANNER_PROMPT,
    RESEARCHER_PROMPT,
    WRITER_PROMPT,
)
from novel_agent.repository import NovelCorpus
from novel_agent.state import AgentState
from novel_agent.structured_output import DiagnosticCallback, invoke_structured
from novel_agent.tracing import ModelUsageCallback, model_usage_event

MAX_UNRESOLVED_QUESTIONS = 8
MAX_SUGGESTED_QUERIES = 5
MAX_HYPOTHESES = 8
MAX_RESEARCH_EVIDENCE = 6


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _current_task(state: AgentState) -> dict[str, Any] | None:
    task_id = state.get("current_task_id")
    return next((task for task in state.get("plan", []) if task.get("task_id") == task_id), None)


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


def _bounded_unique(values: list[str], limit: int) -> list[str]:
    unique = list(dict.fromkeys(value.strip() for value in values if value and value.strip()))
    return unique[-limit:]


def _research_history(messages: list[Any]) -> list[Any]:
    """只保留原问题，避免把已经处理过的大段工具结果反复发给模型。

    完整工具轨迹仍保存在 AgentState 中；Researcher 下一轮需要的信息会由
    Assessor 压缩进 evidence、unresolved_questions 等结构化字段。
    """

    first_human = next((message for message in messages if isinstance(message, HumanMessage)), None)
    return [first_human] if first_human is not None else []


def _evidence_summary(
    evidence: list[dict[str, Any]],
    *,
    limit: int | None = None,
    include_quote: bool = False,
) -> list[dict[str, Any]]:
    selected = evidence[-limit:] if limit is not None else evidence
    keys = ["evidence_id", "task_id", "claim", "chunk_id", "supports"]
    if include_quote:
        keys.extend(["quote", "section_title", "start_line", "end_line", "interpretation"])
    return [{key: item.get(key) for key in keys} for item in selected]


def _hypothesis_summary(hypotheses: list[dict[str, Any]], limit: int = MAX_HYPOTHESES) -> list[dict[str, Any]]:
    keys = ["hypothesis_id", "statement", "confidence", "status"]
    return [{key: item.get(key) for key in keys} for item in hypotheses[-limit:]]


def _latest_tool_exchange(messages: list[Any]) -> tuple[int, list[ToolMessage]]:
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


def _tool_result(content: Any) -> Any:
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


class AgentNodes:
    """状态图中各节点的实现。

    节点接收完整 AgentState，但只返回本轮发生变化的字段。Planner、Assessor、
    Replanner 和 Writer 要求模型返回 Pydantic 结构；Researcher 则允许模型通过
    tool_calls 自主选择检索工具。
    """

    def __init__(
        self,
        model: BaseChatModel,
        tools: list[BaseTool],
        corpus: NovelCorpus,
        *,
        structured_retries: int | None = None,
        on_diagnostic: DiagnosticCallback | None = None,
        on_model_usage: ModelUsageCallback | None = None,
    ):
        self.model = model
        self.corpus = corpus
        self.structured_retries = (
            configured_structured_retries()
            if structured_retries is None
            else structured_retries
        )
        self.on_diagnostic = on_diagnostic
        self.on_model_usage = on_model_usage

        # 同一个基础模型绑定成五种角色。Researcher 绑定真实工具；其余角色绑定
        # 输出 schema，使后续代码能按字段处理结果，而不必解析自然语言。
        self.research_model = model.bind_tools(tools)
        structured_options = {"method": "function_calling", "include_raw": True}
        self.plan_model = model.with_structured_output(PlanOutput, **structured_options)
        self.assess_model = model.with_structured_output(AssessmentOutput, **structured_options)
        self.replan_model = model.with_structured_output(ReplanOutput, **structured_options)
        self.writer_model = model.with_structured_output(FinalAnswer, **structured_options)

    def _invoke_structured(
        self, runnable: Any, messages: list[Any], schema: type[Any], source_node: str
    ) -> Any:
        return invoke_structured(
            runnable,
            messages,
            schema,
            source_node=source_node,
            retries=self.structured_retries,
            on_diagnostic=self.on_diagnostic,
            on_model_usage=self.on_model_usage,
        ).value

    def planner(self, state: AgentState) -> dict[str, Any]:
        """把用户问题拆成最多三个可调查任务，并激活第一个任务。

        简单事实题不值得多调用一次模型，因此直接走本地生成的单任务计划。
        """

        if is_simple_question(state["question"]):
            return {
                "question_mode": "simple",
                "plan": [{
                    "task_id": "T1",
                    "description": "查找能够直接回答问题的原文",
                    "status": "in_progress",
                }],
                "current_task_id": "T1",
            }

        output = self._invoke_structured(
            self.plan_model,
            [SystemMessage(content=PLANNER_PROMPT), HumanMessage(content=state["question"])],
            PlanOutput,
            "planner",
        )
        tasks = [task.model_dump() for task in output.tasks[:3]]
        if not tasks:
            tasks = [{
                "task_id": "T1",
                "description": "查找与问题直接相关的原文",
                "status": "pending",
            }]
        tasks[0]["status"] = "in_progress"
        return {"question_mode": "complex", "plan": tasks, "current_task_id": tasks[0]["task_id"]}

    def researcher(self, state: AgentState) -> dict[str, Any]:
        """根据当前任务和证据缺口，决定下一次要调用哪些小说检索工具。"""

        # 在调用模型前先执行硬预算检查，保证调查循环一定能够结束。
        if state["step_count"] >= state["max_steps"]:
            return {
                "messages": [AIMessage(content="已达到最大调查步数，进入证据审查。")],
                "termination_reason": "max_steps_reached",
            }

        # 只给模型最近且必要的摘要，控制上下文长度。原始工具结果已经由
        # Assessor 消化，不会在下一轮 Researcher 中整段重放。
        context = {
            "current_task": _current_task(state),
            "plan_status": [
                {"task_id": task.get("task_id"), "status": task.get("status")}
                for task in state["plan"]
            ],
            "verified_evidence": _evidence_summary(state["evidence"], limit=MAX_RESEARCH_EVIDENCE),
            "unresolved_questions": state["unresolved_questions"][-MAX_UNRESOLVED_QUESTIONS:],
            "suggested_queries": state["suggested_queries"][-MAX_SUGGESTED_QUERIES:],
            "remaining_steps": state["max_steps"] - state["step_count"],
            "recent_tool_calls": [
                {
                    "tool": item.get("tool"),
                    "args": item.get("args", {}),
                    "fingerprint": item.get("fingerprint"),
                    "ok": item.get("ok"),
                }
                for item in state["tool_call_history"][-3:]
            ],
        }
        messages = [
            SystemMessage(content=RESEARCHER_PROMPT),
            *_research_history(state["messages"]),
            HumanMessage(content="当前状态：\n" + _json(context)),
        ]
        started = time.perf_counter()
        response = self.research_model.invoke(messages)
        if self.on_model_usage is not None:
            self.on_model_usage(model_usage_event(response, "researcher", time.perf_counter() - started))
        if not isinstance(response, AIMessage):
            response = AIMessage(content=str(response))
        if len(response.tool_calls) > 2:
            response = response.model_copy(update={"tool_calls": response.tool_calls[:2]})

        # 同一调用连续出现通常表示模型陷入循环。这里主动熔断，让 Assessor
        # 使用已有证据收尾，而不是继续浪费步数。
        fingerprints = _tool_call_fingerprints(response)
        recent_fingerprints = [item.get("fingerprint") for item in state["tool_call_history"][-2:]]
        if fingerprints and all(recent_fingerprints.count(item) >= 2 for item in fingerprints):
            return {
                "messages": [AIMessage(content="检测到连续重复工具调用，进入证据审查。")],
                "step_count": state["step_count"] + 1,
                "termination_reason": "repeated_tool_call",
            }
        return {"messages": [response], "step_count": state["step_count"] + 1}

    def assessor(self, state: AgentState) -> dict[str, Any]:
        """审查本轮工具结果，并充当整个调查循环的决策中心。

        该节点会：提取并校验证据、维护假设和任务状态、记录工具调用，最后
        生成 review。路由函数根据 review 决定继续 Researcher、进入 Replanner，
        或交给 Writer。
        """

        # 找到最近一次 AI tool_calls 及其对应的 ToolMessage。ToolNode 只负责
        # 执行工具，工具结果的业务含义统一在这里解释。
        last_call_index, tool_messages = _latest_tool_exchange(state["messages"])
        prompt_data = {
            "current_task": _current_task(state),
            "plan": state["plan"],
            "existing_evidence": _evidence_summary(state["evidence"]),
            "existing_hypotheses": _hypothesis_summary(state["hypotheses"]),
            "unresolved_questions": state["unresolved_questions"][-MAX_UNRESOLVED_QUESTIONS:],
            "tool_results": [
                {"tool_name": message.name, "tool_result": _tool_result(message.content)}
                for message in tool_messages
            ],
            "steps": {"used": state["step_count"], "maximum": state["max_steps"]},
        }
        output = self._invoke_structured(
            self.assess_model,
            [
                SystemMessage(content=ASSESSOR_PROMPT),
                HumanMessage(content=state["question"]),
                HumanMessage(content="当前状态与当轮工具结果：\n" + _json(prompt_data)),
            ],
            AssessmentOutput,
            "assessor",
        )

        # 模型提出的“证据”不能直接相信：先去重，再确认 quote 的确逐字存在于
        # 指定 chunk。只有通过校验的内容才能进入最终写作材料。
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
        for item in output.evidence[:3]:
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

        # 假设允许随着新证据被 revised/rejected；相同 ID 会覆盖旧版本。
        hypothesis_by_id = {item["hypothesis_id"]: item for item in state["hypotheses"]}
        for hypothesis in output.hypotheses:
            hypothesis_by_id[hypothesis.hypothesis_id] = hypothesis.model_dump()
        hypotheses = list(hypothesis_by_id.values())
        hypotheses.sort(key=lambda item: item.get("status") not in {"active", "revised"})
        hypotheses = hypotheses[:MAX_HYPOTHESES]

        # 将 ToolMessage 与发起它的 tool_call 重新配对，供重复调用检测和 UI 展示。
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
            content = str(tool_message.content)
            parsed_result = _tool_result(tool_message.content)
            history.append({
                "tool": tool_message.name,
                "args": matching_call.get("args", {}),
                "fingerprint": fingerprint,
                "ok": not (isinstance(parsed_result, dict) and "error" in parsed_result),
                "result_size_chars": len(content),
                "rejected_evidence_ids": rejected_quotes,
            })

        unresolved = _bounded_unique(
            [*state["unresolved_questions"], *output.unresolved_questions],
            MAX_UNRESOLVED_QUESTIONS,
        )
        suggested_queries = _bounded_unique(output.suggested_queries, MAX_SUGGESTED_QUERIES)
        task_attempts = dict(state["task_attempts"])
        if tool_messages:
            task_attempts[current_task_id] = task_attempts.get(current_task_id, 0) + 1

        # 任务只有在 Assessor 判定完成且至少拥有一条有效证据时才能 completed；
        # 达到单任务尝试上限仍无证据时标为 blocked，避免卡死在一个任务上。
        plan = [dict(task) for task in state["plan"]]
        completed = set(output.completed_task_ids)
        evidence_task_ids = {item.get("task_id") for item in evidence}
        for task in plan:
            if task["task_id"] in completed and task["task_id"] in evidence_task_ids:
                task["status"] = "completed"

        current_task = next((task for task in plan if task.get("task_id") == current_task_id), None)
        if (
            current_task is not None
            and current_task.get("status") == "in_progress"
            and task_attempts.get(str(current_task_id), 0) >= state["max_task_attempts"]
        ):
            current_task["status"] = "completed" if current_task_id in evidence_task_ids else "blocked"

        next_task_id = output.next_task_id
        available_ids = {
            task["task_id"] for task in plan if task.get("status") in {"pending", "in_progress"}
        }
        if next_task_id not in available_ids:
            next_task_id = _first_pending_task_id(plan)
        for task in plan:
            if task["task_id"] == next_task_id and task["status"] == "pending":
                task["status"] = "in_progress"

        # “模型认为证据充分”还不够：计划中的每项任务也必须已经得到处理。
        # 这是代码层的约束，用来防止模型过早结束调查。
        all_tasks_resolved = bool(plan) and all(
            task.get("status") in {"completed", "blocked"} for task in plan
        )
        effective_sufficient = output.sufficient and all_tasks_resolved
        review = {
            "sufficient": effective_sufficient,
            "missing_information": output.missing_information[:MAX_UNRESOLVED_QUESTIONS],
            "contradictions": output.contradictions[:MAX_UNRESOLVED_QUESTIONS],
            "suggested_queries": suggested_queries,
            "should_replan": output.should_replan,
            "completed_task_ids": output.completed_task_ids,
            "next_task_id": next_task_id,
            "rationale": output.rationale,
            "decision_summary": output.decision_summary,
            "all_tasks_resolved": all_tasks_resolved,
        }
        if output.sufficient and not all_tasks_resolved:
            review["rationale"] = (
                output.rationale + "；仍有未完成计划任务，因此继续调查。"
            ).strip("；")

        termination = state.get("termination_reason")
        if effective_sufficient:
            termination = "evidence_sufficient"
        elif state["step_count"] >= state["max_steps"]:
            termination = "max_steps_reached"
        elif all_tasks_resolved and (
            state["question_mode"] == "simple"
            or not output.should_replan
            or state["replan_count"] >= state["max_replans"]
        ):
            termination = "tasks_resolved"

        return {
            "plan": plan,
            "current_task_id": next_task_id,
            "task_attempts": task_attempts,
            "evidence": evidence,
            "hypotheses": hypotheses,
            "unresolved_questions": unresolved,
            "suggested_queries": suggested_queries,
            "tool_call_history": history,
            "review": review,
            "termination_reason": termination,
        }

    def replanner(self, state: AgentState) -> dict[str, Any]:
        """根据证据缺口重写后续计划，同时保留已完成/阻塞任务的状态。"""

        prompt_data = {
            "plan": state["plan"],
            "evidence_summary": _evidence_summary(state["evidence"]),
            "review": {
                key: (state["review"] or {}).get(key)
                for key in ("missing_information", "contradictions", "suggested_queries", "rationale")
            },
        }
        output = self._invoke_structured(
            self.replan_model,
            [
                SystemMessage(content=REPLANNER_PROMPT),
                HumanMessage(content=state["question"]),
                HumanMessage(content="当前调查摘要：\n" + _json(prompt_data)),
            ],
            ReplanOutput,
            "replanner",
        )
        tasks = [task.model_dump() for task in output.tasks[:3]]
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
        """只使用已经过校验的证据生成最终答案，并显式报告材料限制。"""

        review = state["review"] or {}
        prompt_data = {
            "evidence": _evidence_summary(state["evidence"], include_quote=True),
            "relevant_hypotheses": _hypothesis_summary(state["hypotheses"], limit=5),
            "missing_information": review.get("missing_information", []),
            "contradictions": review.get("contradictions", []),
            "termination_reason": state["termination_reason"],
        }
        output = self._invoke_structured(
            self.writer_model,
            [
                SystemMessage(content=WRITER_PROMPT),
                HumanMessage(content=state["question"]),
                HumanMessage(content="已验证材料：\n" + _json(prompt_data)),
            ],
            FinalAnswer,
            "writer",
        )
        return {"final_answer": output.answer, "limitations": output.limitations}


def route_after_researcher(state: AgentState) -> Literal["tools", "assessor"]:
    """Researcher 发出了 tool_calls 就执行工具，否则直接审查现有材料。"""

    if state.get("termination_reason") in {"max_steps_reached", "repeated_tool_call"}:
        return "assessor"
    last_message = state["messages"][-1]
    return "tools" if isinstance(last_message, AIMessage) and last_message.tool_calls else "assessor"


def route_after_assessor(state: AgentState) -> Literal["researcher", "replanner", "writer"]:
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

    # 固定边描述主干，条件边描述 Agent 的循环和退出条件。
    builder.add_edge(START, "planner")
    builder.add_edge("planner", "researcher")
    builder.add_conditional_edges("researcher", route_after_researcher)
    builder.add_edge("tools", "assessor")
    builder.add_conditional_edges("assessor", route_after_assessor)
    builder.add_edge("replanner", "researcher")
    builder.add_edge("writer", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())


def stable_thread_id(novel_path: str, question: str) -> str:
    """为相同小说和问题生成可复现的短线程 ID（当前 Web/CLI 可另行传入 ID）。"""

    digest = hashlib.sha256(f"{novel_path}\n{question}".encode("utf-8")).hexdigest()
    return digest[:16]
