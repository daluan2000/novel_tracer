from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from novel_agent.agent.state import initial_state
from novel_agent.application.service import AgentExecutionResult
from novel_agent.web.app import create_app
from novel_agent.web.conversations import ConversationStore


NOVEL_TEXT = ("第一章 开始\n\n人物在这里出现。\n" * 30).encode("utf-8")


def _upload(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/novels", files={"file": ("sample.txt", NOVEL_TEXT, "text/plain")}
    )
    assert response.status_code == 201
    return response.json()


def _events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_conversation_api_persists_turns_and_limits_context(tmp_path: Path) -> None:
    seen_history: list[list[dict[str, str]]] = []

    def executor(_corpus, question: str, **kwargs: Any) -> AgentExecutionResult:
        history = kwargs["conversation_history"]
        seen_history.append(history)
        state = dict(initial_state(question, kwargs["max_steps"], history))
        state["final_answer"] = f"回答：{question}"
        state["termination_reason"] = "evidence_sufficient"
        kwargs["on_update"]("writer", {"final_answer": state["final_answer"]}, state)
        return AgentExecutionResult(state=state)

    data_root = tmp_path / "data"
    with TestClient(create_app(executor=executor, data_root=data_root)) as client:
        novel = _upload(client)
        created = client.post(
            "/api/conversations", json={"novel_id": novel["novel_id"]}
        )
        assert created.status_code == 201
        conversation_id = created.json()["conversation_id"]

        for index in range(12):
            question = f"第 {index + 1} 个问题" + ("很长" * 20 if index == 0 else "")
            started = client.post(
                f"/api/conversations/{conversation_id}/runs",
                json={"question": question, "max_steps": 5},
            )
            assert started.status_code == 202
            streamed = client.get(
                f"/api/runs/{started.json()['run_id']}/events"
            )
            assert _events(streamed.text)[-1]["type"] == "complete"

        detail = client.get(f"/api/conversations/{conversation_id}").json()
        listed = client.get(
            f"/api/novels/{novel['novel_id']}/conversations"
        ).json()
        after_first = client.get(
            f"/api/runs/{detail['turns'][-1]['run_id']}/events", params={"after": 1}
        )

    assert len(detail["turns"]) == 12
    assert detail["title"].endswith("…")
    assert len(detail["title"].removesuffix("…")) <= 32
    assert listed["items"][0]["turn_count"] == 12
    assert len(seen_history[-1]) == 10
    assert seen_history[-1][0]["question"].startswith("第 2 个问题")
    assert seen_history[-1][-1]["answer"].startswith("回答：第 11 个问题")
    assert all(event["sequence"] > 1 for event in _events(after_first.text))

    with TestClient(create_app(executor=executor, data_root=data_root)) as client:
        restored = client.get(f"/api/conversations/{conversation_id}")
        assert restored.status_code == 200
        assert len(restored.json()["turns"]) == 12
        deleted = client.delete(f"/api/conversations/{conversation_id}")
        assert deleted.json()["deleted"] is True


def test_failed_and_cancelled_turns_are_excluded_from_history(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    store.open()
    item = store.create("novel-1")
    conversation_id = item["conversation_id"]

    for run_id, status, answer in (
        ("completed-run", "completed", "可信答案"),
        ("failed-run", "failed", "失败时的草稿"),
        ("cancelled-run", "cancelled", "取消时的草稿"),
    ):
        store.start_turn(
            conversation_id, run_id=run_id, question=run_id, max_steps=5
        )
        store.record_event(
            conversation_id,
            run_id,
            {
                "sequence": 1,
                "timestamp": "2026-10-08T00:00:00+00:00",
                "type": "complete" if status == "completed" else status,
                "status": status,
                "node": "writer",
                "detail": {},
                "snapshot": {"final_answer": answer},
                "error": None,
            },
        )

    assert store.history(conversation_id) == [
        {"question": "completed-run", "answer": "可信答案"}
    ]


def test_active_turn_is_interrupted_after_restart_and_cannot_be_deleted(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path)
    store.open()
    item = store.create("novel-1")
    conversation_id = item["conversation_id"]
    store.start_turn(
        conversation_id, run_id="active-run", question="问题", max_steps=5
    )

    try:
        store.delete(conversation_id)
    except RuntimeError as exc:
        assert "运行中" in str(exc)
    else:
        raise AssertionError("active conversation deletion should fail")

    restored = ConversationStore(tmp_path)
    restored.open()
    turn = restored.get(conversation_id)["turns"][0]
    assert turn["status"] == "failed"
    assert turn["events"][-1]["detail"]["code"] == "run_interrupted"
    assert turn["resumable"] is False


def test_running_conversation_delete_returns_conflict(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_executor(_corpus, question: str, **kwargs: Any) -> AgentExecutionResult:
        started.set()
        release.wait(timeout=2)
        return AgentExecutionResult(
            state=dict(initial_state(question, kwargs["max_steps"]))
        )

    with TestClient(create_app(executor=slow_executor, data_root=tmp_path / "data")) as client:
        novel = _upload(client)
        conversation = client.post(
            "/api/conversations", json={"novel_id": novel["novel_id"]}
        ).json()
        run = client.post(
            f"/api/conversations/{conversation['conversation_id']}/runs",
            json={"question": "运行中的问题", "max_steps": 5},
        )
        assert run.status_code == 202
        assert started.wait(timeout=1)
        deleted = client.delete(
            f"/api/conversations/{conversation['conversation_id']}"
        )
        release.set()

    assert deleted.status_code == 409


def test_invalid_conversation_file_does_not_block_store_startup(tmp_path: Path) -> None:
    root = tmp_path / "conversations"
    root.mkdir(parents=True)
    (root / "broken.json").write_text("{broken", encoding="utf-8")

    store = ConversationStore(tmp_path)
    store.open()

    assert store.list_for_novel("novel-1") == []
