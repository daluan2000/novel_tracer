from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.memory import InMemorySaver

from novel_agent.agent.graph import build_agent_graph
from novel_agent.agent.state import initial_state
from novel_agent.agent.tools import build_tools
from novel_agent.corpus.repository import NovelCorpus
from novel_agent.runtime.config import ModelConfig
from novel_agent.runtime.tracing import TraceWriter, add_model_usage, empty_token_usage


@dataclass(frozen=True)
class LoadedCorpus:
    corpus: NovelCorpus
    elapsed_seconds: float


@dataclass(frozen=True)
class AgentExecutionResult:
    state: dict[str, Any]
    cancelled: bool = False


@dataclass
class ExecutionTelemetry:
    """Metrics that must survive manual resumptions of the same run."""

    structured_retry_count: int = 0
    content_fallback_count: int = 0
    model_call_count: int = 0
    token_usage: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.token_usage is None:
            self.token_usage = empty_token_usage()


AgentUpdateCallback = Callable[[str, dict[str, Any], dict[str, Any]], None]
AgentDiagnosticCallback = Callable[[dict[str, Any], dict[str, Any]], None]
CancelCheck = Callable[[], bool]


def load_corpus(path: str | Path) -> LoadedCorpus:
    started = time.perf_counter()
    corpus = NovelCorpus.from_path(path)
    return LoadedCorpus(corpus=corpus, elapsed_seconds=time.perf_counter() - started)


def execute_agent(
    corpus: NovelCorpus,
    question: str,
    *,
    max_steps: int,
    thread_id: str,
    trace_path: str | Path | None = None,
    model: BaseChatModel | None = None,
    on_update: AgentUpdateCallback | None = None,
    on_diagnostic: AgentDiagnosticCallback | None = None,
    should_cancel: CancelCheck | None = None,
    structured_retries: int | None = None,
    checkpointer: InMemorySaver | None = None,
    resume: bool = False,
    telemetry: ExecutionTelemetry | None = None,
) -> AgentExecutionResult:
    """执行一次 Agent 图，并把节点级事件暴露给 Web 层。

    这里是应用层入口：它创建模型与工具、初始化状态、消费 LangGraph 的流式
    更新，并汇总诊断指标。具体的调查决策全部留在 agent 包中。
    """

    active_model = model or ModelConfig.from_env().create_model()
    # thread_id 是 LangGraph checkpoint 的会话键；recursion_limit 是框架级保险，
    # 真正的业务步数限制仍由 AgentState.max_steps 控制。
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": max_steps * 5 + 20,
    }
    trace = TraceWriter(trace_path) if trace_path is not None else None
    current_state: dict[str, Any] = dict(initial_state(question, max_steps))
    execution_telemetry = telemetry or ExecutionTelemetry()

    def state_with_diagnostics(state: dict[str, Any]) -> dict[str, Any]:
        """把回调在图外统计的指标投影到对外可见状态。"""

        enriched = dict(state)
        enriched["structured_retry_count"] = execution_telemetry.structured_retry_count
        enriched["content_fallback_count"] = execution_telemetry.content_fallback_count
        enriched["model_call_count"] = execution_telemetry.model_call_count
        enriched["token_usage"] = execution_telemetry.token_usage or empty_token_usage()
        return enriched

    def handle_diagnostic(diagnostic: dict[str, Any]) -> None:
        nonlocal current_state
        if diagnostic.get("diagnostic_code") == "structured_output_retry":
            execution_telemetry.structured_retry_count += 1
        elif diagnostic.get("diagnostic_code") == "content_json_fallback":
            execution_telemetry.content_fallback_count += 1
        current_state = state_with_diagnostics(current_state)
        if trace is not None:
            trace.append("model_diagnostic", diagnostic)
        if on_diagnostic is not None:
            on_diagnostic(dict(diagnostic), dict(current_state))

    def handle_model_usage(usage: dict[str, Any]) -> None:
        nonlocal current_state
        execution_telemetry.model_call_count += 1
        execution_telemetry.token_usage = add_model_usage(
            execution_telemetry.token_usage or empty_token_usage(), usage
        )
        current_state = state_with_diagnostics(current_state)
        if trace is not None:
            trace.append("model_usage", usage)

    # 每本小说都生成一组只访问该 corpus 的本地工具，模型不能越过这些工具
    # 直接读取文件系统。
    graph = build_agent_graph(
        model=active_model,
        tools=build_tools(corpus),
        corpus=corpus,
        structured_retries=structured_retries,
        on_diagnostic=handle_diagnostic,
        on_model_usage=handle_model_usage,
        checkpointer=checkpointer,
    )

    if resume:
        resumed = graph.get_state(config)
        if not resumed.values or not resumed.next:
            raise RuntimeError("任务没有可恢复的失败节点。")
        current_state = state_with_diagnostics(dict(resumed.values))

    if should_cancel and should_cancel():
        return AgentExecutionResult(state=current_state, cancelled=True)

    try:
        # updates 模式每经过一个节点就产生一次 {node_name: changed_fields}。
        # get_state 再从 checkpoint 取得合并后的完整状态，方便 UI 画时间线。
        for event in graph.stream(
            None if resume else initial_state(question, max_steps),
            config=config,
            stream_mode="updates",
        ):
            for node, update in event.items():
                if trace is not None:
                    trace.append(node, update)
                current_state = state_with_diagnostics(dict(graph.get_state(config).values))
                if on_update is not None:
                    on_update(node, update, current_state)
                # 取消发生在节点边界；不会在一次正在进行的模型请求中强行中断。
                if should_cancel and should_cancel():
                    return AgentExecutionResult(state=current_state, cancelled=True)
    except Exception as exc:
        # LangGraph 会在节点开始前保存 checkpoint。把失败节点和当时的状态附到
        # 原异常，Web 层既能展示现场，也能在用户确认后从该节点继续。
        try:
            snapshot = graph.get_state(config)
            checkpoint_state = dict(snapshot.values)
            failed_node = str(snapshot.next[0]) if snapshot.next else None
        except Exception:
            checkpoint_state = {}
            failed_node = None
        current_state = state_with_diagnostics(checkpoint_state or current_state)
        setattr(exc, "state", current_state)
        setattr(exc, "failed_node", failed_node)
        setattr(exc, "checkpoint_available", bool(failed_node and checkpoint_state))
        raise

    return AgentExecutionResult(
        state=state_with_diagnostics(dict(graph.get_state(config).values))
    )
