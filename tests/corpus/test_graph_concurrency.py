from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from novel_agent.corpus.knowledge_graph.concurrency import BuildStopped, parallel_map
from novel_agent.corpus.knowledge_graph.models import ResolutionDecision
from novel_agent.corpus.knowledge_graph.resolution import resolve
from novel_agent.corpus.knowledge_graph.service import rate_limit_delay
from novel_agent.corpus.knowledge_graph.storage import GraphStorage, SCHEMA, PHASE_SCHEMA, REVISION_SCHEMA, now
from novel_agent.runtime.config import (graph_extraction_concurrency, graph_resolution_concurrency,
                                       graph_rate_limit_retries)
from novel_agent.web.app import create_app
from test_knowledge_graph import upload, wait_job


@pytest.mark.parametrize("function,key,default,low,high", [
    (graph_extraction_concurrency, "NOVEL_AGENT_GRAPH_EXTRACTION_CONCURRENCY", 4, 1, 16),
    (graph_resolution_concurrency, "NOVEL_AGENT_GRAPH_RESOLUTION_CONCURRENCY", 4, 1, 16),
    (graph_rate_limit_retries, "NOVEL_AGENT_GRAPH_RATE_LIMIT_RETRIES", 3, 0, 5),
])
def test_concurrency_config(monkeypatch, function, key, default, low, high):
    monkeypatch.delenv(key, raising=False)
    assert function() == default
    for value in (low, high):
        monkeypatch.setenv(key, str(value))
        assert function() == value
    for value in (str(low - 1), str(high + 1), "many", "1.5", ""):
        monkeypatch.setenv(key, value)
        with pytest.raises(RuntimeError):
            function()


def test_parallel_map_is_bounded_and_preserves_order():
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    active, peak = 0, 0

    def task(value):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=3)
        time.sleep((4 - value % 4) * 0.005)
        with lock:
            active -= 1
        return value

    assert parallel_map(task, range(8), 4) == list(range(8))
    assert peak == 4


def test_parallel_map_drains_successes_and_keeps_fatal_error():
    entered, abort, release, saved = (threading.Event() for _ in range(4))
    calls = []

    def task(value):
        calls.append(value)
        if value == 0:
            assert entered.wait(3)
            raise ValueError("fatal")
        entered.set()
        assert release.wait(3)
        saved.set()
        raise BuildStopped()

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(parallel_map, task, range(10), 2, on_error=abort.set)
        assert abort.wait(3)
        assert not future.done()
        release.set()
        with pytest.raises(ValueError, match="fatal"):
            future.result(timeout=3)
    assert saved.is_set() and sorted(calls) == [0, 1]


def entity(name, position, *, kind="person", description=""):
    mention = {"start_char": position, "end_char": position + len(name), "quote": name,
               "section_id": "s", "section_order": 0}
    return {"local_id": f"{name}:{position}", "name": name, "type": kind,
            "description": description, "mentions": [mention], "contexts": [mention]}


def resolution_fixture():
    values = [entity("张三", 0), entity("李四", 10), entity("Ａ", 20), entity("a", 30),
              entity("师父", 40), entity("师父", 50), entity("师父", 40),
              entity("张三", 100, description="同名异人"), entity("张三", 200),
              entity("李四", 110), entity("乙", 120), entity("张三", 130, kind="place")]
    batches = [{"unit_id": f"u{i:03d}", "entities": [value], "relations": [], "identity_links": []}
               for i, value in enumerate(values)]
    left, right = copy.deepcopy(values[9]), copy.deepcopy(values[10])
    batches.append({"unit_id": "z", "entities": [left, right], "relations": [
        {"source": left["local_id"], "target": right["local_id"], "type": "knows",
         "polarity": "affirmed", "statement_mode": "narration", "evidence": left["mentions"]}],
        "identity_links": [{"source": left["local_id"], "target": right["local_id"],
                            "evidence": [{**left["mentions"][0], "quote": "李四又名乙。"}]}]})
    return batches


def test_parallel_resolution_matches_serial_payloads_checkpoints_and_replay():
    def run(workers, prior=None):
        saved, payloads, lock = copy.deepcopy(prior or {}), {}, threading.Lock()

        def invoke(uid, schema, prompt, payload):
            assert prior is None, "checkpoint replay must not call a model"
            with lock:
                payloads[uid] = (prompt, copy.deepcopy(payload))
            refs = payload["identity_excerpts"] or payload["entity"]["excerpts"]
            if payload["entity"].get("description") == "同名异人":
                return ResolutionDecision(decision="new", supporting_mentions=[refs[0]["mention_id"]])
            if len(payload["candidates"]) > 1:
                return ResolutionDecision(decision="ambiguous")
            return ResolutionDecision(decision="merge", candidate_id=payload["candidates"][0]["candidate_id"],
                                      supporting_mentions=[refs[0]["mention_id"], payload["candidates"][0]["excerpts"][0]["mention_id"]])

        def checkpoint(uid, fingerprint, result=None, sequence=0):
            with lock:
                if result is not None:
                    saved[uid] = (fingerprint, result, sequence)
                elif uid in saved:
                    assert saved[uid][0] == fingerprint
                    return saved[uid][1]

        result = resolve(resolution_fixture(), "v", invoke, checkpoint, lambda *_: None, concurrency=workers)
        return result, saved, payloads

    serial, saved, payloads = run(1)
    parallel, parallel_saved, parallel_payloads = run(4)
    assert parallel == serial
    assert parallel_saved == saved and parallel_payloads == payloads
    assert run(4, saved)[0] == serial
    assert run(1, parallel_saved)[0] == serial
    people = [e for e in serial[0] if e["name"] == "张三" and e["type"] == "person"]
    assert len(people) == 3  # Explicit homonym plus an unresolved multi-candidate occurrence.
    assert len([e for e in serial[0] if e["name"] == "师父"]) == 2
    assert not serial[1]  # The late alias merge removes the former self edge.


class FunctionModel:
    def __init__(self, function):
        self.function, self.owner = function, threading.get_ident()

    def with_structured_output(self, schema, **kwargs):
        assert threading.get_ident() == self.owner

        def invoke(messages):
            assert threading.get_ident() == self.owner
            return self.function(schema, json.loads(messages[1].content))

        return SimpleNamespace(invoke=invoke)


def test_concurrent_stop_drains_inflight_and_resumes_at_new_concurrency(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_EXTRACTION_CONCURRENCY", "4")
    barrier, release = threading.Barrier(5), threading.Event()
    calls, lock = [], threading.Lock()

    def blocked(schema, payload):
        with lock:
            calls.append(payload)
        barrier.wait(5)
        assert release.wait(5)
        return {}

    root = tmp_path / "data"
    with TestClient(create_app(data_root=root, graph_model_factory=lambda: FunctionModel(blocked))) as client:
        novel = upload(client, "".join(f"{i:05d}张三走过山路。\n" for i in range(2500)))
        base = f"/api/novels/{novel}/graph"
        job = client.post(base + "/builds", json={"mode": "start"}).json()
        barrier.wait(5)
        assert client.post(base + f"/builds/{job['job_id']}/stop").json()["status"] == "stopping"
        release.set()
        stopped = wait_job(client, novel, "stopped")
        assert stopped["job"]["extraction"]["completed"] == 4
        assert len(calls) == 4 and not stopped["available"]
        prior = stopped["job"]["phase_elapsed_seconds"]["extracting"]

    monkeypatch.setenv("NOVEL_AGENT_GRAPH_EXTRACTION_CONCURRENCY", "2")
    resumed_calls = []

    def empty(schema, payload):
        resumed_calls.append(payload)
        return {}

    with TestClient(create_app(data_root=root, graph_model_factory=lambda: FunctionModel(empty))) as client:
        client.post(base + "/builds", json={"mode": "resume", "job_id": job["job_id"]})
        completed = wait_job(client, novel)
        assert all(payload not in calls for payload in resumed_calls)
        assert completed["job"]["phase_elapsed_seconds"]["extracting"] >= prior
        assert completed["job"]["usage"]["request_count"] == len(calls) + len(resumed_calls)
        assert completed["extraction_concurrency"] == 2


def test_fatal_failure_waits_for_successful_sibling_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_EXTRACTION_CONCURRENCY", "2")
    entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
    calls = []

    class AuthenticationError(Exception):
        pass

    def request(schema, payload):
        with lock:
            calls.append(payload)
            index = len(calls)
        if index == 1:
            assert entered.wait(3)
            raise AuthenticationError()
        entered.set()
        assert release.wait(5)
        return {}

    app = create_app(data_root=tmp_path, graph_model_factory=lambda: FunctionModel(request))
    with TestClient(app) as client:
        novel = upload(client, "张三走过山路。" * 3000)
        base = f"/api/novels/{novel}/graph"
        client.post(base + "/builds", json={"mode": "start"})
        assert app.state.knowledge_graph.abort.wait(3)
        assert client.get(base + "/status").json()["job"]["status"] == "building"
        release.set()
        failed = wait_job(client, novel, "failed")
        assert failed["job"]["error_code"] == "graph_model_authentication_failed"
        assert failed["job"]["extraction"]["completed"] == 1
        assert len(calls) == 2


def test_close_waits_for_inflight_response_and_releases_process_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    entered, release = threading.Event(), threading.Event()

    def request(schema, payload):
        entered.set()
        assert release.wait(5)
        return {}

    app = create_app(data_root=tmp_path, graph_model_factory=lambda: FunctionModel(request))
    client = TestClient(app)
    client.__enter__()
    try:
        novel = upload(client, "张三走过山路。")
        client.post(f"/api/novels/{novel}/graph/builds", json={"mode": "start"})
        assert entered.wait(3)
        service = app.state.knowledge_graph
        with ThreadPoolExecutor(1) as pool:
            closing = pool.submit(client.__exit__, None, None, None)
            assert service.shutdown.wait(3)
            assert not closing.done() and service.storage.lock_file is not None
            release.set()
            closing.result(timeout=5)
        assert not service.worker.is_alive()
        assert service.storage.job(novel)["status"] == "stopped"
        with service.storage.connect() as db:
            assert db.execute("SELECT count(*) FROM graph_units WHERE status='completed'").fetchone()[0] == 1
        assert service.storage.lock_file is None
    finally:
        release.set()
        if client.exit_stack is not None:
            client.__exit__(None, None, None)


def test_out_of_order_extraction_is_collected_in_source_order(tmp_path, monkeypatch):
    import novel_agent.corpus.knowledge_graph.service as module
    from novel_agent.corpus.knowledge_graph.extraction import windows
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_EXTRACTION_CONCURRENCY", "4")
    barrier, lock = threading.Barrier(4), threading.Lock()
    seen, captured, completed = [], [], []
    original = module.build_catalog

    def capture(batches, *args, **kwargs):
        captured.extend(batch["unit_id"] for batch in batches)
        return original(batches, *args, **kwargs)

    monkeypatch.setattr(module, "build_catalog", capture)

    def request(schema, payload):
        core = payload["spans"][0]["text"]
        with lock:
            seen.append(core)
            index = len(seen)
        if index <= 4:
            barrier.wait(3)
            time.sleep(0.06 if core == first_core[0] else 0.001)
        with lock:
            completed.append(core)
        return {}

    first_core = []
    app = create_app(data_root=tmp_path, graph_model_factory=lambda: FunctionModel(request))
    with TestClient(app) as client:
        novel = upload(client, "".join(f"{i:05d}张三走过山路。\n" for i in range(2500)))
        corpus = app.state.knowledge_graph.novels.get(novel).corpus
        expected = windows(corpus)
        first_core.append(expected[0].payload(corpus)["spans"][0]["text"])
        client.post(f"/api/novels/{novel}/graph/builds", json={"mode": "start"})
        wait_job(client, novel)
        assert captured == [window.unit_id for window in expected]
        assert completed[0] != first_core[0]


class RateLimitError(Exception):
    status_code = 429

    def __init__(self, retry_after="0"):
        self.response = SimpleNamespace(headers={"retry-after": retry_after}, status_code=429)


def test_retry_after_and_wrapped_rate_limits():
    assert rate_limit_delay(RateLimitError("2.5"), 0) == 2.5
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
    assert 28 <= rate_limit_delay(RateLimitError(date), 0) <= 30
    assert 4 <= rate_limit_delay(RateLimitError("bad"), 2) <= 5
    assert rate_limit_delay(ValueError(), 0) is None
    try:
        raise RuntimeError() from RateLimitError("0")
    except RuntimeError as error:
        assert rate_limit_delay(error, 0) == 0


@pytest.mark.parametrize("exhaust", [False, True])
def test_rate_limit_retries_count_each_request(tmp_path, monkeypatch, exhaust):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_RATE_LIMIT_RETRIES", "2")
    calls = []

    def request(schema, payload):
        calls.append(payload)
        if exhaust or len(calls) < 3:
            raise RateLimitError()
        return {}

    with TestClient(create_app(data_root=tmp_path, graph_model_factory=lambda: FunctionModel(request))) as client:
        novel = upload(client, "张三走过山路。")
        client.post(f"/api/novels/{novel}/graph/builds", json={"mode": "start"})
        status = wait_job(client, novel, "failed" if exhaust else "completed")
        assert len(calls) == status["job"]["usage"]["request_count"] == 3
        assert status["job"]["usage"]["failed_request_count"] == (3 if exhaust else 2)
        assert status["job"]["error_code"] == ("graph_model_rate_limited" if exhaust else None)
        assert sum(e["code"] == "graph_model_rate_limit_retry" for e in status["job"]["events"]) == 2


def test_shared_cooldown_is_interruptible(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_EXTRACTION_CONCURRENCY", "1")
    entered = threading.Event()

    def request(schema, payload):
        entered.set()
        raise RateLimitError("30")

    app = create_app(data_root=tmp_path, graph_model_factory=lambda: FunctionModel(request))
    with TestClient(app) as client:
        novel = upload(client, "张三走过山路。")
        base = f"/api/novels/{novel}/graph"
        job = client.post(base + "/builds", json={"mode": "start"}).json()
        assert entered.wait(3)
        # Wait until the common deadline has been installed, not just the request entered.
        deadline = time.monotonic() + 3
        while app.state.knowledge_graph.cooldown_until <= time.monotonic():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(app.state.knowledge_graph._wait_for_cooldown,
                                  app.state.knowledge_graph.storage.job(novel))
            assert not waiting.done()
            client.post(base + f"/builds/{job['job_id']}/stop")
            with pytest.raises(BuildStopped):
                waiting.result(timeout=2)
        status = wait_job(client, novel, "stopped")
        assert status["job"]["usage"]["request_count"] == 1


def test_timeout_and_rate_limit_retry_budgets_do_not_multiply(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_RATE_LIMIT_RETRIES", "2")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_REQUEST_RETRIES", "1")
    responses = iter([RateLimitError(), TimeoutError(), RateLimitError(), {}])
    calls = []

    def request(schema, payload):
        calls.append(payload)
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    app = create_app(data_root=tmp_path, graph_model_factory=lambda: FunctionModel(request))
    monkeypatch.setattr(app.state.knowledge_graph, "_wait_for_retry", lambda *_: None)
    with TestClient(app) as client:
        novel = upload(client, "张三走过山路。")
        client.post(f"/api/novels/{novel}/graph/builds", json={"mode": "start"})
        status = wait_job(client, novel)
        assert len(calls) == 4
        assert status["job"]["usage"]["failed_request_count"] == 3


def test_schema_one_migrates_preserving_published_version_and_missing_times(tmp_path):
    folder = tmp_path / "knowledge_graph"
    folder.mkdir()
    with sqlite3.connect(folder / "graph.sqlite3") as db:
        db.executescript(SCHEMA.replace(PHASE_SCHEMA + ";\n", "").replace(REVISION_SCHEMA, "").replace("user_version=4", "user_version=1"))
        db.execute("INSERT INTO graph_versions(version,novel_id,state,fingerprint,source_identity,model_identity,created_at) VALUES('v','n','published','f','{}','{}',?)", (now(),))
        db.execute("INSERT INTO graph_novels VALUES('n','v')")
        db.execute("INSERT INTO graph_jobs(job_id,novel_id,version,status,created_at,updated_at) VALUES('j','n','v','completed',?,?)", (now(), now()))
    storage = GraphStorage(tmp_path)
    storage.open()
    try:
        assert storage.active("n") == "v"
        assert storage.phase_times("j") == {}
        storage.add_phase_time("j", "extracting", 2)
        storage.add_phase_time("j", "extracting", 3)
        assert storage.phase_times("j") == {"extracting": 5}
        with storage.connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        storage.close()
