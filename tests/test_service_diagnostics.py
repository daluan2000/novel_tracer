from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel

from novel_agent.repository import NovelCorpus
from novel_agent.service import execute_agent


class DiagnosticGraph:
    def __init__(self, callback):
        self.callback = callback
        self.values: dict[str, Any] = {}

    def stream(self, state: dict[str, Any], **_kwargs: Any):
        self.values = dict(state)
        self.callback(
            {
                "source_node": "planner",
                "schema": "PlanOutput",
                "diagnostic_code": "structured_output_retry",
                "attempt": 1,
                "max_attempts": 3,
                "retry_number": 1,
                "max_retries": 2,
                "failure_reason": "missing_tool_call",
            }
        )
        self.values["plan"] = [
            {"task_id": "T1", "description": "测试", "status": "in_progress"}
        ]
        yield {"planner": {"plan": self.values["plan"]}}

    def get_state(self, _config: dict[str, Any]):
        return SimpleNamespace(values=self.values)


def test_service_records_safe_model_diagnostics(monkeypatch, tmp_path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("第一章\n\n正文" * 50, encoding="utf-8")
    corpus = NovelCorpus.from_path(path)
    diagnostics: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def build_graph(**kwargs: Any):
        return DiagnosticGraph(kwargs["on_diagnostic"])

    monkeypatch.setattr("novel_agent.service.build_agent_graph", build_graph)
    trace_path = tmp_path / "trace.jsonl"
    result = execute_agent(
        corpus,
        "问题",
        max_steps=5,
        thread_id="diagnostic-test",
        trace_path=trace_path,
        model=cast(BaseChatModel, object()),
        on_diagnostic=lambda diagnostic, state: diagnostics.append((diagnostic, state)),
    )

    trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    diagnostic_event = next(item for item in trace if item["node"] == "model_diagnostic")
    assert diagnostic_event["update"]["failure_reason"] == "missing_tool_call"
    assert "raw" not in diagnostic_event["update"]
    assert "content" not in diagnostic_event["update"]
    assert diagnostics[0][1]["structured_retry_count"] == 1
    assert result.state["structured_retry_count"] == 1
    assert result.state["step_count"] == 0
