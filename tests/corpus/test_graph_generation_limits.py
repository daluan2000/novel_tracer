from __future__ import annotations

import hashlib
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from novel_agent.corpus.knowledge_graph.extraction import validate_batch, windows
from novel_agent.corpus.knowledge_graph.models import ExtractionBatch
from novel_agent.corpus.knowledge_graph.service import KnowledgeGraphService
from novel_agent.corpus.knowledge_graph.storage import encode
from novel_agent.corpus.repository import NovelCorpus
from novel_agent.runtime.config import ModelConfig
from novel_agent.web.app import create_app

from test_knowledge_graph import ScriptModel, upload, wait_job


@pytest.mark.parametrize("failure", ["timeout", "response_length", "sdk_length"])
def test_long_window_is_split_without_repeating_unbounded_request(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_STRUCTURED_RETRIES", "5")
    calls = []
    class LengthFinishReasonError(Exception):
        pass
    class DenseModel:
        def with_structured_output(self, schema, **kwargs):
            def invoke(messages):
                import json
                core = json.loads(messages[1].content)["spans"][0]["text"]
                calls.append(core)
                if len(core) > 1000:
                    if failure == "timeout":
                        raise RuntimeError("safe wrapper") from TimeoutError("provider secret")
                    if failure == "sdk_length":
                        raise LengthFinishReasonError("provider secret")
                    raw = SimpleNamespace(response_metadata={"finish_reason": "length"},
                        usage_metadata={"input_tokens": 100, "output_tokens": 6144, "total_tokens": 6244})
                    return {"raw": raw, "parsed": None, "parsing_error": ValueError("partial JSON")}
                return ExtractionBatch()
            return SimpleNamespace(invoke=invoke)
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=DenseModel)) as client:
        novel = upload(client, "张三走过山路。" * 220)
        base = f"/api/novels/{novel}/graph"
        client.post(base + "/builds", json={"mode": "start"})
        status = wait_job(client, novel)
        assert len(calls) == 3 and len(calls[0]) > 1000
        assert len(calls[1]) <= 1000 and len(calls[2]) <= 1000
        assert calls[1] + calls[2] == calls[0]
        assert status["job"]["extraction"] == {"completed": 2, "total": 2}
        codes = {e["code"] for e in status["job"]["events"]}
        assert ("graph_extraction_timeout_split" if failure == "timeout" else "graph_extraction_output_split") in codes
        assert "structured_output_retry" not in codes
        with client.app.state.knowledge_graph.storage.connect() as db:
            first = db.execute("SELECT status,error_code,elapsed_ms FROM graph_attempts ORDER BY rowid LIMIT 1").fetchone()
        assert first["status"] == "failed" and first["elapsed_ms"] is not None
        assert first["error_code"] == ("graph_model_timeout" if failure == "timeout" else "graph_model_output_limit")
        if failure == "response_length":
            assert status["job"]["usage"]["reported_output_tokens"] == 6144
            assert status["job"]["usage"]["unknown_request_count"] == 2


def test_transient_timeout_retries_small_window_and_counts_each_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = ScriptModel()
    model.fail_at = 1
    app = create_app(data_root=tmp_path, graph_model_factory=lambda: model)
    monkeypatch.setattr(app.state.knowledge_graph, "_wait_for_retry", lambda job, seconds: None)
    with TestClient(app) as client:
        novel = upload(client)
        client.post(f"/api/novels/{novel}/graph/builds", json={"mode": "start"})
        status = wait_job(client, novel)
        assert model.calls[0] == model.calls[1]
        assert status["job"]["usage"]["failed_request_count"] == 1
        assert any(e["code"] == "graph_model_timeout_retry" for e in status["job"]["events"])


def test_retries_are_bounded_and_stop_interrupts_backoff(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_REQUEST_RETRIES", "1")
    calls = []
    class OfflineModel:
        def with_structured_output(self, *args, **kwargs):
            def invoke(messages):
                calls.append(1)
                raise TimeoutError("secret")
            return SimpleNamespace(invoke=invoke)
    app = create_app(data_root=tmp_path, graph_model_factory=OfflineModel)
    service = app.state.knowledge_graph
    monkeypatch.setattr(service, "_wait_for_retry", lambda job, seconds: None)
    with TestClient(app) as client:
        novel = upload(client)
        base = f"/api/novels/{novel}/graph"
        client.post(base + "/builds", json={"mode": "start"})
        failed = wait_job(client, novel, "failed")
        assert len(calls) == 2
        assert failed["job"]["error_code"] == "graph_model_timeout"
        assert failed["job"]["usage"]["unknown_request_count"] == 2
        entered, release = threading.Event(), threading.Event()
        def waiting(job, seconds):
            entered.set()
            assert release.wait(3)
            service._check_stop(job)
        monkeypatch.setattr(service, "_wait_for_retry", waiting)
        client.post(base + "/builds", json={"mode": "resume", "job_id": failed["job"]["job_id"]})
        assert entered.wait(3)
        client.post(base + f"/builds/{failed['job']['job_id']}/stop")
        release.set()
        wait_job(client, novel, "stopped")
        assert len(calls) == 3


def test_legacy_timeout_resume_preserves_completed_unit_and_splits_failed_unit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    source = tmp_path / "novel.txt"
    source.write_text("张三走过山路。" * 600, encoding="utf-8")
    corpus = NovelCorpus.from_path(source)
    item = SimpleNamespace(novel_id="book", corpus=corpus, source_hash=hashlib.sha256(corpus.document.source.text.encode()).hexdigest())
    calls = []
    class EmptyModel:
        def with_structured_output(self, *args, **kwargs):
            def invoke(messages):
                import json
                calls.append(json.loads(messages[1].content)["spans"][0]["text"])
                return ExtractionBatch()
            return SimpleNamespace(invoke=invoke)
    service = KnowledgeGraphService(tmp_path / "data", SimpleNamespace(get=lambda key: item), EmptyModel)
    service.storage.open()
    try:
        job, _ = service.build("book")
        version = job["graph_version"]
        units = windows(corpus)
        first, failed = units[:2]
        saved = validate_batch(corpus, first, ExtractionBatch())
        existing = service.storage.unit(version, first.unit_id)
        service.storage.save_unit(version, first.unit_id, "extract", 0, existing["input_hash"], saved)
        service.storage.update_job(job["job_id"], status="failed", error_code="graph_model_timeout")
        for attempt in ("legacy1", "legacy2"):
            service.storage.start_attempt(attempt, version, failed.unit_id)
            service.storage.finish_attempt(attempt, 120000, error_code="model_request_failed")
        config = ModelConfig.from_env()
        fingerprint = service._fingerprint(item, config)
        monkeypatch.setenv("NOVEL_AGENT_GRAPH_REQUEST_TIMEOUT_SECONDS", "240")
        monkeypatch.setenv("NOVEL_AGENT_GRAPH_MAX_OUTPUT_TOKENS", "4096")
        assert service._fingerprint(item, config) == fingerprint
        resumed, _ = service.build("book", "resume", job["job_id"])
        service._run(service.storage.job("book", job["job_id"]))
        assert resumed["graph_version"] == version
        assert service.storage.job("book")["status"] == "completed"
        assert service.storage.unit(version, first.unit_id)["result"] == saved
        assert service.storage.unit(version, failed.unit_id)["status"] == "split"
        # No re-send of the known completed batch or the timed-out large window.
        assert len(calls) == len(units) and all(len(text) < failed.end - failed.start for text in calls)
        with service.storage.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM graph_attempts WHERE unit_id=?", (failed.unit_id,)).fetchone()[0] == 2
    finally:
        service.close()
