from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel

from novel_agent.config import ModelConfig
from novel_agent.graph import build_agent_graph
from novel_agent.repository import NovelCorpus
from novel_agent.state import initial_state
from novel_agent.structured_output import StructuredOutputError
from novel_agent.tools import build_tools
from novel_agent.tracing import TraceWriter, add_model_usage, empty_token_usage


@dataclass(frozen=True)
class LoadedCorpus:
    corpus: NovelCorpus
    elapsed_seconds: float


@dataclass(frozen=True)
class AgentExecutionResult:
    state: dict[str, Any]
    cancelled: bool = False


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
) -> AgentExecutionResult:
    """Run the graph once and expose node-boundary updates to any presentation layer."""

    active_model = model or ModelConfig.from_env().create_model()
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": max_steps * 5 + 20,
    }
    trace = TraceWriter(trace_path) if trace_path is not None else None
    current_state: dict[str, Any] = dict(initial_state(question, max_steps))
    retry_count = 0
    fallback_count = 0
    model_call_count = 0
    token_usage = empty_token_usage()

    def state_with_diagnostics(state: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(state)
        enriched["structured_retry_count"] = retry_count
        enriched["content_fallback_count"] = fallback_count
        enriched["model_call_count"] = model_call_count
        enriched["token_usage"] = token_usage
        return enriched

    def handle_diagnostic(diagnostic: dict[str, Any]) -> None:
        nonlocal retry_count, fallback_count, current_state
        if diagnostic.get("diagnostic_code") == "structured_output_retry":
            retry_count += 1
        elif diagnostic.get("diagnostic_code") == "content_json_fallback":
            fallback_count += 1
        current_state = state_with_diagnostics(current_state)
        if trace is not None:
            trace.append("model_diagnostic", diagnostic)
        if on_diagnostic is not None:
            on_diagnostic(dict(diagnostic), dict(current_state))

    def handle_model_usage(usage: dict[str, Any]) -> None:
        nonlocal model_call_count, token_usage, current_state
        model_call_count += 1
        token_usage = add_model_usage(token_usage, usage)
        current_state = state_with_diagnostics(current_state)
        if trace is not None:
            trace.append("model_usage", usage)

    graph = build_agent_graph(
        model=active_model,
        tools=build_tools(corpus),
        corpus=corpus,
        structured_retries=structured_retries,
        on_diagnostic=handle_diagnostic,
        on_model_usage=handle_model_usage,
    )

    if should_cancel and should_cancel():
        return AgentExecutionResult(state=current_state, cancelled=True)

    try:
        for event in graph.stream(
            initial_state(question, max_steps),
            config=config,
            stream_mode="updates",
        ):
            for node, update in event.items():
                if trace is not None:
                    trace.append(node, update)
                current_state = state_with_diagnostics(dict(graph.get_state(config).values))
                if on_update is not None:
                    on_update(node, update, current_state)
                if should_cancel and should_cancel():
                    return AgentExecutionResult(state=current_state, cancelled=True)
    except StructuredOutputError as exc:
        checkpoint = dict(graph.get_state(config).values)
        current_state = state_with_diagnostics(checkpoint or current_state)
        exc.state = current_state
        raise

    return AgentExecutionResult(
        state=state_with_diagnostics(dict(graph.get_state(config).values))
    )
