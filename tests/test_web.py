from __future__ import annotations

import json
import time
from typing import Any

from fastapi.testclient import TestClient

from novel_agent.service import AgentExecutionResult
from novel_agent.state import initial_state
from novel_agent.structured_output import StructuredOutputError
from novel_agent.web import create_app


SAMPLE_TEXT = ("第一章 庙会\n\n吕树和吕小鱼在庙会上讨论晚饭。\n\n" * 30).encode("utf-8")


def _upload(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/novels",
        files={"file": ("sample.txt", SAMPLE_TEXT, "text/plain")},
    )
    assert response.status_code == 201
    return response.json()


def _success_executor(_corpus, question: str, **kwargs: Any) -> AgentExecutionResult:
    state = dict(initial_state(question, kwargs["max_steps"]))
    state["plan"] = [{"task_id": "T1", "description": "查找人物", "status": "in_progress"}]
    state["current_task_id"] = "T1"
    kwargs["on_update"]("planner", {"plan": state["plan"]}, state)
    state["plan"][0]["status"] = "completed"
    state["final_answer"] = "基于原文完成。"
    state["termination_reason"] = "evidence_sufficient"
    kwargs["on_update"]("writer", {"final_answer": state["final_answer"]}, state)
    return AgentExecutionResult(state=state)


def _slow_executor(_corpus, question: str, **kwargs: Any) -> AgentExecutionResult:
    state = dict(initial_state(question, kwargs["max_steps"]))
    state["plan"] = [{"task_id": "T1", "description": "等待停止", "status": "in_progress"}]
    state["current_task_id"] = "T1"
    kwargs["on_update"]("planner", {"plan": state["plan"]}, state)
    deadline = time.time() + 2
    while not kwargs["should_cancel"]() and time.time() < deadline:
        time.sleep(0.01)
    return AgentExecutionResult(state=state, cancelled=kwargs["should_cancel"]())


def _structured_failure_executor(_corpus, question: str, **kwargs: Any) -> AgentExecutionResult:
    state = dict(initial_state(question, kwargs["max_steps"]))
    state["plan"] = [{"task_id": "T1", "description": "已有计划", "status": "in_progress"}]
    state["current_task_id"] = "T1"
    kwargs["on_update"]("planner", {"plan": state["plan"]}, state)
    diagnostic = {
        "source_node": "observe",
        "schema": "ObservationOutput",
        "diagnostic_code": "structured_output_retry",
        "attempt": 1,
        "max_attempts": 3,
        "retry_number": 1,
        "max_retries": 2,
        "failure_reason": "missing_tool_call",
    }
    state["structured_retry_count"] = 1
    kwargs["on_diagnostic"](diagnostic, state)
    error = StructuredOutputError(
        source_node="observe",
        schema_name="ObservationOutput",
        attempts=3,
        failure_reason="missing_tool_call",
    )
    error.state = state
    raise error


def _sse_events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_config_status_does_not_expose_api_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-key")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    with TestClient(create_app(executor=_success_executor)) as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["model_name"] == "test-model"
    assert response.json()["structured_output_retries"] == 2
    assert "super-secret-key" not in response.text


def test_config_status_rejects_invalid_structured_retry_setting(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_AGENT_STRUCTURED_RETRIES", "6")

    with TestClient(create_app()) as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert response.json()["structured_output_retries"] is None
    assert "0 到 5" in response.json()["error"]


def test_upload_structure_search_and_context() -> None:
    with TestClient(create_app(executor=_success_executor)) as client:
        novel = _upload(client)
        novel_id = novel["novel_id"]

        sections = client.get(f"/api/novels/{novel_id}/sections", params={"limit": 1})
        search = client.get(
            f"/api/novels/{novel_id}/search",
            params={"q": "吕树 吕小鱼", "top_k": 3},
        )
        chunk_id = search.json()[0]["chunk_id"]
        context = client.get(f"/api/novels/{novel_id}/chunks/{chunk_id}/context")

    assert novel["filename"] == "sample.txt"
    assert novel["section_count"] >= 1
    assert sections.status_code == 200
    assert sections.json()["items"]
    assert search.status_code == 200
    assert {"吕树", "吕小鱼"}.issubset(search.json()[0]["matched_terms"])
    assert context.status_code == 200
    assert any("吕小鱼" in chunk["text"] for chunk in context.json())


def test_upload_validation_and_missing_novel() -> None:
    with TestClient(create_app(executor=_success_executor, upload_limit=4)) as client:
        wrong_type = client.post(
            "/api/novels", files={"file": ("sample.md", b"text", "text/markdown")}
        )
        empty = client.post(
            "/api/novels", files={"file": ("sample.txt", b"", "text/plain")}
        )
        too_large = client.post(
            "/api/novels", files={"file": ("sample.txt", b"12345", "text/plain")}
        )
        missing = client.get("/api/novels/missing/sections")

    assert wrong_type.status_code == 400
    assert empty.status_code == 400
    assert too_large.status_code == 400
    assert missing.status_code == 404


def test_run_stream_exposes_normalized_events_only() -> None:
    with TestClient(create_app(executor=_success_executor)) as client:
        novel = _upload(client)
        started = client.post(
            "/api/runs",
            json={"novel_id": novel["novel_id"], "question": "人物在哪里？", "max_steps": 5},
        )
        stream = client.get(f"/api/runs/{started.json()['run_id']}/events")

    assert started.status_code == 202
    events = _sse_events(stream.text)
    assert [event["type"] for event in events][-1] == "complete"
    assert any(event["node"] == "planner" for event in events)
    assert events[-1]["snapshot"]["final_answer"] == "基于原文完成。"
    assert "messages" not in events[-1]["snapshot"]
    assert "tool_call_history" not in events[-1]["snapshot"]


def test_concurrent_run_is_rejected_and_cancel_is_terminal() -> None:
    with TestClient(create_app(executor=_slow_executor)) as client:
        novel = _upload(client)
        payload = {"novel_id": novel["novel_id"], "question": "停止测试", "max_steps": 5}
        first = client.post("/api/runs", json=payload)
        second = client.post("/api/runs", json=payload)
        cancelled = client.post(f"/api/runs/{first.json()['run_id']}/cancel")
        stream = client.get(f"/api/runs/{first.json()['run_id']}/events")

    assert first.status_code == 202
    assert second.status_code == 409
    assert cancelled.status_code == 202
    assert _sse_events(stream.text)[-1]["type"] == "cancelled"


def test_structured_failure_keeps_partial_state_and_safe_diagnostics() -> None:
    with TestClient(create_app(executor=_structured_failure_executor)) as client:
        novel = _upload(client)
        started = client.post(
            "/api/runs",
            json={"novel_id": novel["novel_id"], "question": "结构化失败测试", "max_steps": 5},
        )
        stream = client.get(f"/api/runs/{started.json()['run_id']}/events")

    events = _sse_events(stream.text)
    retry = next(
        event for event in events
        if event["detail"].get("diagnostic_code") == "structured_output_retry"
    )
    failure = events[-1]
    assert retry["node"] == "observe"
    assert retry["detail"]["retry_number"] == 1
    assert failure["type"] == "error"
    assert failure["node"] == "observe"
    assert failure["detail"]["code"] == "structured_output_failed"
    assert failure["detail"]["retryable"] is True
    assert failure["snapshot"]["plan"][0]["description"] == "已有计划"
    assert failure["snapshot"]["metrics"]["structured_retry_count"] == 1
    assert "raw" not in stream.text
    assert "结构化失败测试" not in stream.text
