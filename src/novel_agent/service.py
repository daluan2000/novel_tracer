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
from novel_agent.tools import build_tools
from novel_agent.tracing import TraceWriter


@dataclass(frozen=True)
class LoadedCorpus:
    corpus: NovelCorpus
    elapsed_seconds: float


@dataclass(frozen=True)
class AgentExecutionResult:
    state: dict[str, Any]
    cancelled: bool = False


AgentUpdateCallback = Callable[[str, dict[str, Any], dict[str, Any]], None]
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
    should_cancel: CancelCheck | None = None,
) -> AgentExecutionResult:
    """Run the graph once and expose node-boundary updates to any presentation layer."""

    active_model = model or ModelConfig.from_env().create_model()
    graph = build_agent_graph(
        model=active_model,
        tools=build_tools(corpus),
        corpus=corpus,
    )
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": max_steps * 5 + 20,
    }
    trace = TraceWriter(trace_path) if trace_path is not None else None
    current_state: dict[str, Any] = dict(initial_state(question, max_steps))

    if should_cancel and should_cancel():
        return AgentExecutionResult(state=current_state, cancelled=True)

    for event in graph.stream(
        initial_state(question, max_steps),
        config=config,
        stream_mode="updates",
    ):
        for node, update in event.items():
            if trace is not None:
                trace.append(node, update)
            current_state = dict(graph.get_state(config).values)
            if on_update is not None:
                on_update(node, update, current_state)
            if should_cancel and should_cancel():
                return AgentExecutionResult(state=current_state, cancelled=True)

    return AgentExecutionResult(state=dict(graph.get_state(config).values))
