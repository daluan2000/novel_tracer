from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novel_agent.web.app import create_app


NOVEL_TEXT = "第一章 归来\n\n石猴回到花果山，与混世魔王交战。\n\n" * 30
NOVEL_BYTES = NOVEL_TEXT.encode("utf-8")


def _upload(client: TestClient, name: str, content: bytes = NOVEL_BYTES) -> dict:
    response = client.post(
        "/api/novels",
        files={"file": (name, content, "text/plain")},
    )
    assert response.status_code == 201
    return response.json()


def _wait_for_status(client: TestClient, novel_id: str, expected: str) -> dict:
    deadline = time.time() + 3
    status = client.get(f"/api/novels/{novel_id}/retrieval-status").json()
    while status["status"] != expected and time.time() < deadline:
        time.sleep(0.01)
        status = client.get(f"/api/novels/{novel_id}/retrieval-status").json()
    assert status["status"] == expected
    return status


def test_novel_is_persisted_deduplicated_and_restored(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    first_app = create_app(data_root=data_root)
    with TestClient(first_app) as client:
        first = _upload(client, "first.txt")
        duplicate = _upload(client, "renamed.txt", NOVEL_TEXT.encode("utf-16"))
        changed = _upload(client, "changed.txt", NOVEL_BYTES + "尾声".encode("utf-8"))
        listed = client.get("/api/novels").json()

        assert duplicate["novel_id"] == first["novel_id"]
        assert duplicate["filename"] == "first.txt"
        assert changed["novel_id"] != first["novel_id"]
        assert listed["total"] == 2
        assert listed["items"][0]["novel_id"] == changed["novel_id"]

    novel_directory = data_root / "novels" / first["novel_id"]
    manifest = json.loads((novel_directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["filename"] == "first.txt"
    assert len(manifest["source_hash"]) == 64
    assert (novel_directory / "source.txt").read_bytes() == NOVEL_BYTES

    restored_app = create_app(data_root=data_root)
    with TestClient(restored_app) as client:
        listed = client.get("/api/novels").json()
        search = client.get(
            f"/api/novels/{first['novel_id']}/search",
            params={"q": "混世魔王"},
        )

        assert listed["total"] == 2
        assert {item["novel_id"] for item in listed["items"]} == {
            first["novel_id"],
            changed["novel_id"],
        }
        assert search.status_code == 200
        assert search.json()


def test_dense_embedding_cache_is_reused_after_service_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class CountingEmbedding:
        def __init__(self) -> None:
            self.document_calls = 0

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.document_calls += 1
            return [[1.0, 0.0] for _text in texts]

        def embed_query(self, _text: str) -> list[float]:
            return [1.0, 0.0]

    provider = CountingEmbedding()
    monkeypatch.setenv("EMBEDDING_MODEL", "persistent-test-embedding")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(
        "novel_agent.corpus.repository.create_embedding_provider",
        lambda _config: provider,
    )
    data_root = tmp_path / "data"

    with TestClient(create_app(data_root=data_root)) as client:
        novel = _upload(client, "cached.txt")
        initial = client.get(f"/api/novels/{novel['novel_id']}/retrieval-status").json()
        assert initial["status"] == "lexical_ready"
        assert initial["embedding_enabled"] is False
        client.put(
            f"/api/novels/{novel['novel_id']}/embedding",
            json={"enabled": True},
        )
        first_status = _wait_for_status(client, novel["novel_id"], "hybrid_ready")
        assert first_status["metrics"]["index_cache_hit"] is False
        first_call_count = provider.document_calls
        assert first_call_count > 0

    with TestClient(create_app(data_root=data_root)) as client:
        listed = client.get("/api/novels").json()
        second_status = _wait_for_status(client, novel["novel_id"], "hybrid_ready")

        assert listed["items"][0]["novel_id"] == novel["novel_id"]
        assert second_status["embedding_enabled"] is True
        assert second_status["embedding_progress"]["percentage"] == 100
        assert second_status["metrics"]["index_cache_hit"] is True
        assert second_status["metrics"]["document_request_count"] == 0
        assert any(
            event["code"] == "embedding_cache_loaded"
            for event in second_status["events"]
        )
        assert provider.document_calls == first_call_count


@pytest.mark.parametrize("artifact", ["manifest", "source"])
def test_invalid_persisted_novel_is_skipped_without_blocking_startup(
    tmp_path: Path,
    artifact: str,
) -> None:
    data_root = tmp_path / "data"
    with TestClient(create_app(data_root=data_root)) as client:
        novel = _upload(client, "broken.txt")

    directory = data_root / "novels" / novel["novel_id"]
    if artifact == "manifest":
        (directory / "manifest.json").write_text("{broken", encoding="utf-8")
    else:
        (directory / "source.txt").write_text("内容已被修改", encoding="utf-8")

    with TestClient(create_app(data_root=data_root)) as client:
        assert client.get("/api/novels").json() == {"items": [], "total": 0}
        assert client.get("/api/config").status_code == 200
