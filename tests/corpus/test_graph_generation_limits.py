from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from novel_agent.corpus.knowledge_graph.extraction import validate_batch, windows, compact_window, validate_compact_batch, core_candidate_counts
from novel_agent.corpus.knowledge_graph.models import ExtractionBatch, CompactExtractionBatch
from novel_agent.corpus.knowledge_graph.service import KnowledgeGraphService
from novel_agent.corpus.knowledge_graph.storage import encode
from novel_agent.corpus.repository import NovelCorpus
from novel_agent.runtime.config import ModelConfig
from novel_agent.web.app import create_app

from test_knowledge_graph import ScriptModel, upload, wait_job


@pytest.mark.parametrize("compact", [False, True])
def test_graph_requests_use_matching_format_contract_and_actionable_retry(tmp_path, monkeypatch, caplog, compact):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_STRUCTURED_RETRIES", "1")
    calls = []
    class FormatModel:
        def with_structured_output(self, schema, **kwargs):
            def invoke(messages):
                calls.append((schema, messages))
                if compact and schema is ExtractionBatch:
                    return {"raw": SimpleNamespace(response_metadata={"finish_reason": "length"}),
                            "parsed": None, "parsing_error": None}
                if len([s for s, _ in calls if s is schema]) == 1:
                    return {"entities": [{"local_id": "e1", "name": "private-source-text", "type": "bad-value"}]}
                return schema()
            return SimpleNamespace(invoke=invoke)
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=FormatModel)) as client:
        novel = upload(client, "张三拜李四为师。")
        client.post(f"/api/novels/{novel}/graph/builds", json={"mode": "start"})
        status = wait_job(client, novel)
        schema = CompactExtractionBatch if compact else ExtractionBatch
        relevant = [messages for s, messages in calls if s is schema]
        assert len(relevant) == 2
        prompt = relevant[0][0].content
        assert f"调用 {schema.__name__}" in prompt
        example = json.loads(next(line for line in prompt.splitlines() if line.startswith('{"entities":')))
        schema.model_validate(example)
        assert "entities.0.type" in relevant[1][-1].content
        assert any(e["code"] == "structured_output_retry" for e in status["job"]["events"])
        assert "literal_error" in caplog.text and "entities.0.type" in caplog.text
        assert "private-source-text" not in caplog.text and "bad-value" not in caplog.text


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
                payload = json.loads(messages[1].content)
                core = "".join(s["text"] for s in payload["segments"] if s["region"] == "core") if "segments" in payload else payload["spans"][0]["text"]
                calls.append(core)
                if len(core) > 1000:
                    if failure == "timeout":
                        raise RuntimeError("safe wrapper") from TimeoutError("provider secret")
                    if failure == "sdk_length":
                        raise LengthFinishReasonError("provider secret")
                    raw = SimpleNamespace(response_metadata={"finish_reason": "length"},
                        usage_metadata={"input_tokens": 100, "output_tokens": 6144, "total_tokens": 6244})
                    return {"raw": raw, "parsed": None, "parsing_error": ValueError("partial JSON")}
                return {}
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


def test_compact_evidence_ids_are_mapped_to_verified_source(corpus):
    window, payload = compact_window(corpus, windows(corpus)[0])
    segment = next(s for s in payload["segments"] if s["region"] == "core" and "吕树" in s["text"] and "吕小鱼" in s["text"])
    batch = CompactExtractionBatch.model_validate({
        "entities": [
            {"local_id": "a", "name": "吕树", "type": "person", "span_id": segment["id"]},
            {"local_id": "b", "name": "吕小鱼", "type": "person", "span_id": segment["id"]}],
        "relations": [
            {"source": "a", "target": "b", "type": "knows", "evidence_ids": [segment["id"]]},
            {"source": "a", "target": "b", "type": "friend_of", "evidence_ids": ["fabricated-id"]}]})
    result = validate_compact_batch(corpus, window, batch)
    assert len(result["entities"]) == 2 and len(result["relations"]) == 1
    assert result["rejected"]["relations"] == 1
    span = result["relations"][0]["evidence"][0]
    assert span["quote"] == segment["text"]
    assert corpus.document.source.text[span["start_char"]:span["end_char"]] == span["quote"]
    assert corpus.validate_quote(span["chunk_id"], span["quote"])
    assert result["extraction_format"] == "compact-v1"
    assert all(len(s["text"]) <= 180 for s in payload["segments"])


def test_976_character_window_recovers_and_replays_smaller_split_after_restart(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls, entered, release = [], threading.Event(), threading.Event()
    class DenseModel:
        def with_structured_output(self, schema, **kwargs):
            def invoke(messages):
                payload = json.loads(messages[1].content)
                calls.append(schema is CompactExtractionBatch)
                if len(calls) <= 2:
                    raw = SimpleNamespace(response_metadata={"finish_reason": "length"}, usage_metadata={"input_tokens": 100, "output_tokens": 6144})
                    return {"raw": raw, "parsed": None, "parsing_error": None}
                if len(calls) == 4:
                    entered.set()
                    assert release.wait(3)
                return {}
            return SimpleNamespace(invoke=invoke)
    root = tmp_path / "data"
    with TestClient(create_app(data_root=root, graph_model_factory=DenseModel)) as client:
        novel = upload(client, "张三认识李四。" + "风" * (976 - len("张三认识李四。")))
        base = f"/api/novels/{novel}/graph"
        job = client.post(base + "/builds", json={"mode": "start"}).json()
        assert entered.wait(5)
        client.post(base + f"/builds/{job['job_id']}/stop")
        release.set()
        stopped = wait_job(client, novel, "stopped")
        assert stopped["job"]["extraction"] == {"completed": 2, "total": 2}
        with client.app.state.knowledge_graph.storage.connect() as db:
            parent = db.execute("SELECT result_json FROM graph_units WHERE version=? AND unit_id='extract:0:0'", (job["graph_version"],)).fetchone()
            assert json.loads(parent[0])["split_min_chars"] == 200
        assert calls == [False, True, True, True]
    with TestClient(create_app(data_root=root, graph_model_factory=DenseModel)) as client:
        client.post(base + "/builds", json={"mode": "resume", "job_id": job["job_id"]})
        wait_job(client, novel)
        assert len(calls) == 4


@pytest.mark.parametrize("compact,count", [(False, 81), (True, 32)])
def test_minimum_dense_window_keeps_all_valid_candidates(tmp_path, monkeypatch, compact, count):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    names = ["神" + chr(0x4E00 + i) for i in range(count)]
    body = "、".join(names) + "。"
    body += "风" * (258 - len(body))
    calls = []
    class DenseModel:
        def with_structured_output(self, schema, **kwargs):
            def invoke(messages):
                payload = json.loads(messages[1].content)
                calls.append(schema.__name__)
                if compact and schema is ExtractionBatch:
                    raw = SimpleNamespace(response_metadata={"finish_reason": "length"}, usage_metadata={"input_tokens": 100, "output_tokens": 6144})
                    return {"raw": raw, "parsed": None, "parsing_error": None}
                if schema is CompactExtractionBatch:
                    return {"entities": [{"local_id": f"e{i}", "name": name, "type": "person",
                        "span_id": next(s["id"] for s in payload["segments"] if s["region"] == "core" and name in s["text"])}
                        for i, name in enumerate(names)]}
                return {"entities": [{"local_id": f"e{i}", "name": name, "type": "person",
                    "mentions": [{"span_id": "core", "quote": name}]} for i, name in enumerate(names)]}
            return SimpleNamespace(invoke=invoke)
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=DenseModel)) as client:
        novel = upload(client, body)
        client.post(f"/api/novels/{novel}/graph/builds", json={"mode": "start"})
        status = wait_job(client, novel)
        assert status["counts"]["entity_count"] == count
        assert status["job"]["extraction"] == {"completed": 1, "total": 1}
        assert "graph_extraction_capacity_saved" in {e["code"] for e in status["job"]["events"]}
        result = client.app.state.knowledge_graph.storage.unit(status["active_version"], "extract:0:0")["result"]
        assert len(result["entities"]) == count
        assert result["diagnostics"]["capacity_saturated"] is True
        assert result["diagnostics"]["core_chars"] == 258
        assert len(calls) == (2 if compact else 1)


def test_successful_model_result_is_reused_after_processing_failure(tmp_path, monkeypatch):
    import novel_agent.corpus.knowledge_graph.service as graph_service
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    original = graph_service.validate_batch
    def processing_error(*args, **kwargs):
        raise ValueError("local processing failed")
    monkeypatch.setattr(graph_service, "validate_batch", processing_error)
    model = ScriptModel()
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=lambda: model)) as client:
        novel = upload(client, "张三拜李四为师。")
        base = f"/api/novels/{novel}/graph"
        client.post(base + "/builds", json={"mode": "start"})
        failed = wait_job(client, novel, "failed")
        assert failed["job"]["error_code"] == "graph_extraction_processing_failed"
        assert failed["job"]["extraction"] == {"completed": 0, "total": 1, "cached": 1}
        cached = client.app.state.knowledge_graph.storage.unit(failed["job"]["graph_version"], "extract:0:0")
        assert cached["status"] == "extracted" and cached["result"]["batch"]["entities"]
        assert len(model.calls) == 1
        monkeypatch.setattr(graph_service, "validate_batch", original)
        client.post(base + "/builds", json={"mode": "resume", "job_id": failed["job"]["job_id"]})
        completed = wait_job(client, novel)
        assert len(model.calls) == 1
        assert completed["counts"] == {"entity_count": 2, "relation_count": 1}
        assert completed["job"]["usage"]["request_count"] == 1


def test_density_ignores_repeated_facts_and_context_only_entities():
    window = SimpleNamespace(start=100, end=200)
    def entity(local_id, name, position):
        return {"local_id": local_id, "name": name, "type": "person", "mentions": [{"start_char": position, "end_char": position + 2}]}
    relation = {"source": "a", "target": "b", "type": "knows", "polarity": "affirmed", "statement_mode": "narration"}
    result = {"entities": [entity("a", "张三", 110), entity("same-a", "张三", 110), entity("b", "李四", 50)],
        "relations": [relation, {**relation, "source": "same-a"}], "identity_links": []}
    assert core_candidate_counts(result, window) == {"entities": 1, "relations": 1, "identity_links": 0}


def test_invalid_cached_candidates_do_not_trigger_another_model_request(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    source = tmp_path / "novel.txt"
    source.write_text("张三拜李四为师。", encoding="utf-8")
    corpus = NovelCorpus.from_path(source)
    item = SimpleNamespace(novel_id="book", corpus=corpus, source_hash=hashlib.sha256(corpus.document.source.text.encode()).hexdigest())
    service = KnowledgeGraphService(tmp_path / "data", SimpleNamespace(get=lambda key: item), ScriptModel)
    service.storage.open()
    try:
        job, _ = service.build("book")
        version = job["graph_version"]
        window = windows(corpus)[0]
        saved = service.storage.unit(version, window.unit_id)
        service.storage.save_unit(version, window.unit_id, "extract", 0, saved["input_hash"], {}, "extracted")
        with pytest.raises(ValueError, match="已缓存片段"):
            service._run(service.storage.job("book"))
        with service.storage.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM graph_attempts").fetchone()[0] == 0
    finally:
        service.close()
