from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from langchain_core.messages import HumanMessage, SystemMessage

from novel_agent.runtime.config import ModelConfig, structured_output_retries, graph_request_timeout_seconds, graph_request_retries, graph_max_output_tokens
from novel_agent.runtime.structured_output import invoke_structured
from .extraction import EXTRACTION_PROMPT, validate_batch, windows, split_window
from .models import ExtractionBatch, GraphError, GraphOutputLimitError, ResolutionDecision
from .ontology import PARSER_VERSION, RULE_VERSION, digest, ontology_payload
from .query import GraphReadSession
from .resolution import RESOLUTION_PROMPT, resolve
from .storage import GraphStorage, encode, now

LOGGER = logging.getLogger(__name__)
ACTIVE = {"queued", "building", "stopping"}


class BuildStopped(Exception):
    pass


def is_model_timeout(error: BaseException) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower():
            return True
        error = error.__cause__ or error.__context__
    return False


def is_output_limit(error: BaseException) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, GraphOutputLimitError) or "lengthfinishreason" in type(error).__name__.lower():
            return True
        error = error.__cause__ or error.__context__
    return False


def response_output_limited(response) -> bool:
    raw = response.get("raw") if isinstance(response, dict) else response
    metadata = getattr(raw, "response_metadata", None) or {}
    return any(metadata.get(key) in {"length", "max_tokens", "max_output_tokens"}
               for key in ("finish_reason", "stop_reason"))


def source_identity(item):
    return {"source_hash": item.source_hash, "parser": PARSER_VERSION,
            "mapping": digest([(c.chunk_id, c.section_id, c.start_char, c.end_char) for c in item.corpus.document.chunks])}


def model_identity(config):
    parsed = urlsplit(config.base_url or "https://api.openai.com/v1")
    host = parsed.netloc.rsplit("@", 1)[-1].lower()
    safe_url = urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/"), "", ""))
    return {"model_name": config.model_name, "base_url": safe_url,
            "temperature": config.temperature, "thinking_mode": config.thinking_mode}


class KnowledgeGraphService:
    def __init__(self, root: Path, novels, model_factory: Callable | None = None):
        self.storage = GraphStorage(root)
        self.novels, self.model_factory = novels, model_factory
        self.error_code = None
        self.lock = threading.RLock()
        self.wake, self.shutdown = threading.Event(), threading.Event()
        self.worker = None
        self.running_since: tuple[str, float, float] | None = None

    def open(self):
        try:
            self.storage.open()
            with self.storage.connect() as db:
                for item in self.novels.list():
                    for row in db.execute("SELECT version,source_identity FROM graph_versions WHERE novel_id=? AND state IN ('published','superseded')", (item.novel_id,)).fetchall():
                        if json.loads(row["source_identity"]) != source_identity(item):
                            db.execute("UPDATE graph_versions SET state='invalid' WHERE version=?", (row["version"],))
                            db.execute("UPDATE graph_novels SET active_version=NULL WHERE active_version=?", (row["version"],))
            self.worker = threading.Thread(target=self._worker, name="novel-graph-builder", daemon=True)
            self.worker.start()
        except (OSError, sqlite3.Error, GraphError) as exc:
            self.error_code = exc.code if isinstance(exc, GraphError) else "graph_storage_unavailable"
            self.storage.close()
            LOGGER.warning("Knowledge graph unavailable: %s", self.error_code)

    def close(self):
        self.shutdown.set()
        self.wake.set()
        # The configured request timeout bounds the wait; never release the process lock
        # while a synchronous model request can still write a checkpoint.
        if self.worker:
            self.worker.join()
        self.storage.close()

    def _available(self):
        if self.error_code:
            raise GraphError(self.error_code, "图谱子系统不可用，原文检索仍可使用。", 503)

    def _config(self):
        try:
            config = ModelConfig.from_env()
            graph_request_timeout_seconds(config.request_timeout_seconds)
            graph_request_retries()
            graph_max_output_tokens()
            return config
        except (ValueError, RuntimeError) as exc:
            raise GraphError("graph_model_not_configured", "请先配置有效的聊天模型。") from exc

    def _fingerprint(self, item, config):
        return digest({"source": source_identity(item), "model": model_identity(config),
                       "rules": RULE_VERSION, "core_chars": 2500, "context_chars": 300,
                       "prompts": digest([EXTRACTION_PROMPT, RESOLUTION_PROMPT]), "ontology": ontology_payload(),
                       "schemas": digest([ExtractionBatch.model_json_schema(), ResolutionDecision.model_json_schema()])})

    def status(self, novel_id):
        item = self.novels.get(novel_id)
        try:
            config = self._config()
        except GraphError:
            config = None
        result = {"available": False, "active_version": None, "error_code": self.error_code,
                  "configured": config is not None, "model_name": config.model_name if config else None,
                  "source_characters": len(item.corpus.document.source.text),
                  "estimated_extraction_units": len(windows(item.corpus)), "job": None,
                  "request_timeout_seconds": graph_request_timeout_seconds(config.request_timeout_seconds) if config else None,
                  "timeout_retries": graph_request_retries() if config else None,
                  "max_output_tokens": graph_max_output_tokens() if config else None,
                  "ontology": ontology_payload()}
        if self.error_code:
            return result
        try:
            # Publication changes active_version and job status together. Hold the
            # publication lock across this composite status read as well.
            with self.lock:
                version = self.storage.active(novel_id)
                result.update(available=version is not None, active_version=version)
                job = self.storage.job(novel_id)
                if job:
                    result["job"] = self.job_payload(job, item, config)
                if version:
                    with self.storage.connect() as db:
                        row = db.execute("SELECT counts_json FROM graph_versions WHERE version=?", (version,)).fetchone()
                        result["counts"] = json.loads(row[0])
        except sqlite3.Error:
            self.error_code = "graph_storage_unavailable"
            result.update(available=False, error_code=self.error_code)
        return result

    def job_payload(self, job, item=None, config=None):
        version = job["version"]
        with self.storage.connect() as db:
            units = db.execute("SELECT kind,status,COUNT(*) n FROM graph_units WHERE version=? GROUP BY kind,status", (version,)).fetchall()
            counts = {(u["kind"], u["status"]): u["n"] for u in units}
            attempts = db.execute("SELECT status,usage_json FROM graph_attempts WHERE version=?", (version,)).fetchall()
            usage = {"request_count": len(attempts), "reported_input_tokens": 0, "reported_output_tokens": 0,
                     "unknown_request_count": 0, "failed_request_count": 0}
            for a in attempts:
                u = json.loads(a["usage_json"]) if a["usage_json"] else {}
                usage["reported_input_tokens"] += u.get("input_tokens") or 0
                usage["reported_output_tokens"] += u.get("output_tokens") or 0
                usage["unknown_request_count"] += int(u.get("input_tokens") is None or u.get("output_tokens") is None)
                usage["failed_request_count"] += int(a["status"] in {"failed", "interrupted"})
            version_row = db.execute("SELECT fingerprint,counts_json FROM graph_versions WHERE version=?", (version,)).fetchone()
            totals = json.loads(version_row["counts_json"])
            events = [dict(e) for e in db.execute("SELECT code,level,created_at FROM graph_events WHERE job_id=? ORDER BY event_id DESC LIMIT 50", (job["job_id"],))]
        def progress(kind):
            return {"completed": counts.get((kind, "completed"), 0),
                    "total": sum(n for (k, state), n in counts.items() if k == kind and state != "split")}
        elapsed = job["elapsed_seconds"]
        clock = self.running_since
        if clock and clock[0] == job["job_id"]:
            elapsed = clock[2] + time.monotonic() - clock[1]
        return {"job_id": job["job_id"], "graph_version": version, "status": job["status"], "phase": job["phase"],
                "stop_requested": bool(job["stop_requested"]), "extraction": progress("extract"),
                "resolution": progress("resolve"), "entity_count": totals.get("entity_count", 0),
                "relation_count": totals.get("relation_count", 0), "usage": usage,
                "elapsed_seconds": elapsed, "error_code": job["error_code"], "events": events,
                "can_resume": bool(job["status"] in {"failed", "stopped"} and config and item
                                   and version_row["fingerprint"] == self._fingerprint(item, config))}

    def build(self, novel_id, mode="start", job_id=None):
        self._available()
        item = self.novels.get(novel_id)
        config = self._config()
        fingerprint = self._fingerprint(item, config)
        with self.lock:
            current = self.storage.job(novel_id)
            if current and current["status"] in ACTIVE:
                if mode == "resume" and job_id != current["job_id"]:
                    raise GraphError("graph_job_conflict", "已有其他构建任务。")
                return self.job_payload(current, item, config), 202
            with self.storage.connect() as db:
                if mode == "resume":
                    current = self.storage.job(novel_id, job_id)
                    if not current:
                        raise GraphError("graph_object_not_found", "构建任务不存在。", 404)
                    row = db.execute("SELECT fingerprint FROM graph_versions WHERE version=?", (current["version"],)).fetchone()
                    if current["status"] not in {"failed", "stopped"}:
                        raise GraphError("graph_job_conflict", "该任务无法继续。")
                    if row[0] != fingerprint:
                        raise GraphError("graph_fingerprint_changed", "正文或模型配置已变化，请重建。")
                    if current["error_code"] in {"graph_model_timeout", "graph_model_output_limit"}:
                        # Legacy attempts used a generic request-failed code. Preserve
                        # the known timeout on the pending unit before clearing job.error_code.
                        failed = db.execute("SELECT unit_id FROM graph_attempts WHERE version=? AND status='failed' ORDER BY rowid DESC LIMIT 1", (current["version"],)).fetchone()
                        if failed:
                            db.execute("UPDATE graph_units SET result_json=? WHERE version=? AND unit_id=? AND kind='extract' AND status='pending'",
                                (encode({"last_error_code": current["error_code"]}), current["version"], failed[0]))
                    db.execute("UPDATE graph_jobs SET status='queued',stop_requested=0,error_code=NULL,updated_at=? WHERE job_id=?", (now(), current["job_id"]))
                else:
                    active = self.storage.active(novel_id)
                    if mode == "start" and active:
                        row = db.execute("SELECT fingerprint FROM graph_versions WHERE version=?", (active,)).fetchone()
                        if row[0] == fingerprint:
                            return {"cache_hit": True, "graph_version": active, "status": "completed"}, 200
                    if mode == "start" and current and current["status"] in {"failed", "stopped"}:
                        raise GraphError("graph_resume_required", "存在未完成任务，请选择继续或重建。")
                    version, job_id = uuid.uuid4().hex, uuid.uuid4().hex
                    db.execute("INSERT INTO graph_versions(version,novel_id,state,fingerprint,source_identity,model_identity,created_at) VALUES(?,?,?,?,?,?,?)", (version, novel_id, "staging", fingerprint, encode(source_identity(item)), encode(model_identity(config)), now()))
                    db.execute("INSERT INTO graph_jobs(job_id,novel_id,version,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (job_id, novel_id, version, "queued", now(), now()))
                    for i, w in enumerate(windows(item.corpus)):
                        db.execute("INSERT INTO graph_units VALUES(?,?,?,?,?,?,?)", (version, w.unit_id, "extract", i, digest(w.payload(item.corpus)), "pending", "{}"))
            job = self.storage.job(novel_id, job_id if mode != "resume" else current["job_id"])
            response = self.job_payload(job, item, config)
            self.wake.set()
            return response, 202

    def stop(self, novel_id, job_id):
        self._available()
        self.novels.get(novel_id)
        with self.lock:
            job = self.storage.job(novel_id, job_id)
            if not job:
                raise GraphError("graph_object_not_found", "构建任务不存在。", 404)
            if job["status"] in ACTIVE:
                self.storage.update_job(job_id, stop_requested=1, status="stopped" if job["status"] == "queued" else "stopping")
            return self.job_payload(self.storage.job(novel_id, job_id))

    def reader(self, novel_id, version=None, current_only=True):
        self._available()
        item = self.novels.get(novel_id)
        active = self.storage.active(novel_id)
        if not active:
            raise GraphError("graph_not_ready", "请先构建知识图谱。")
        if version is not None and current_only and version != active:
            raise GraphError("graph_version_changed", "图谱已更新，请刷新。")
        return GraphReadSession(self.storage, item.corpus, novel_id, version or active)

    def capture(self, novel_id):
        try:
            return self.reader(novel_id)
        except (GraphError, sqlite3.Error):
            return None

    def _check_stop(self, job):
        latest = self.storage.job(job["novel_id"], job["job_id"])
        if self.shutdown.is_set() or latest["stop_requested"]:
            raise BuildStopped()

    def _wait_for_retry(self, job, seconds):
        if self.shutdown.wait(seconds):
            raise BuildStopped()
        self._check_stop(job)

    def _invoke(self, job, model, unit_id, schema, prompt, payload, *, timeout_retries=None):
        self._check_stop(job)
        service = self
        retries = graph_request_retries() if timeout_retries is None else timeout_retries
        class TrackedRunnable:
            def invoke(self, messages):
                for retry in range(retries + 1):
                    service._check_stop(job)
                    attempt = uuid.uuid4().hex
                    service.storage.start_attempt(attempt, job["version"], unit_id)
                    started = time.monotonic()
                    try:
                        response = runnable.invoke(messages)
                    except Exception as exc:
                        timed_out = is_model_timeout(exc)
                        service.storage.finish_attempt(attempt, (time.monotonic() - started) * 1000,
                            error_code="graph_model_timeout" if timed_out else "graph_model_output_limit" if is_output_limit(exc) else "model_request_failed")
                        service._check_stop(job)
                        if is_output_limit(exc):
                            raise GraphOutputLimitError() from exc
                        if not timed_out or retry == retries:
                            raise
                        service.storage.event(job["job_id"], "graph_model_timeout_retry", "warning")
                        service._wait_for_retry(job, min(2 ** retry, 4))
                        continue
                    from novel_agent.runtime.tracing import model_usage_event
                    usage = model_usage_event(response, "graph_" + job["phase"], time.monotonic() - started)
                    limited = response_output_limited(response)
                    service.storage.finish_attempt(attempt, (time.monotonic() - started) * 1000, usage=usage,
                        error_code="graph_model_output_limit" if limited else None)
                    if limited:
                        raise GraphOutputLimitError()
                    return response
        runnable = model.with_structured_output(schema, method="function_calling", include_raw=True)
        return invoke_structured(TrackedRunnable(), [SystemMessage(content=prompt), HumanMessage(content=encode(payload))], schema,
            source_node="graph_" + job["phase"], retries=structured_output_retries(),
            on_diagnostic=lambda d: self.storage.event(job["job_id"], d["diagnostic_code"], "warning")).value

    def _run(self, job):
        started = time.monotonic()
        item = self.novels.get(job["novel_id"])
        corpus = item.corpus
        config = self._config()
        with self.storage.connect() as db:
            expected = db.execute("SELECT fingerprint FROM graph_versions WHERE version=?", (job["version"],)).fetchone()[0]
        if expected != self._fingerprint(item, config):
            raise GraphError("graph_fingerprint_changed", "配置已变化，请重建。")
        graph_config = replace(config, request_timeout_seconds=graph_request_timeout_seconds(config.request_timeout_seconds))
        model = self.model_factory() if self.model_factory else graph_config.create_model(max_retries=0, max_tokens=graph_max_output_tokens())
        batches = []
        def split_extract(w, sequence, reason):
            children = split_window(corpus, w)
            self.storage.split_unit(job["version"], w.unit_id,
                [(child.unit_id, digest(child.payload(corpus))) for child in children], sequence)
            self.storage.event(job["job_id"], reason, "warning")
            for child in children:
                extract(child, sequence)

        def extract(w, sequence):
            self._check_stop(job)
            saved = self.storage.unit(job["version"], w.unit_id)
            if saved and saved["status"] == "completed":
                batches.append(saved["result"])
                return
            if saved and saved["status"] == "split":
                for child in split_window(corpus, w):
                    extract(child, sequence)
                return
            if not saved:
                self.storage.save_unit(job["version"], w.unit_id, "extract", sequence, digest(w.payload(corpus)), {}, "pending")
            can_split = w.end - w.start >= 1000
            previous_error = saved["result"].get("last_error_code") if saved else None
            if can_split and previous_error in {"graph_model_timeout", "graph_model_output_limit"}:
                split_extract(w, sequence, "graph_extraction_output_split" if previous_error == "graph_model_output_limit" else "graph_extraction_timeout_split")
                return
            try:
                batch = self._invoke(job, model, w.unit_id, ExtractionBatch, EXTRACTION_PROMPT, w.payload(corpus),
                    timeout_retries=0 if can_split else None)
            except Exception as exc:
                if not is_model_timeout(exc) and not is_output_limit(exc):
                    raise
                self._check_stop(job)
                if not can_split:
                    raise
                split_extract(w, sequence, "graph_extraction_output_split" if is_output_limit(exc) else "graph_extraction_timeout_split")
                return
            if len(batch.entities) > 80 or len(batch.relations) > 120 or len(batch.identity_links) > 40:
                split_extract(w, sequence, "graph_extraction_capacity_split")
                return
            result = validate_batch(corpus, w, batch)
            for entity in result["entities"]:
                entity["contexts"] = []
                for mention in entity["mentions"][:2]:
                    chunk = corpus.get_chunk(mention["chunk_id"])
                    a, b = max(chunk.start_char, mention["start_char"] - 100), min(chunk.end_char, mention["end_char"] + 150)
                    entity["contexts"].append({**mention, "start_char": a, "end_char": b, "quote": corpus.document.source.text[a:b]})
            self.storage.save_unit(job["version"], w.unit_id, "extract", sequence, digest(w.payload(corpus)), result)
            for kind, count in result["rejected"].items():
                if count:
                    self.storage.event(job["job_id"], f"rejected_{kind}:{count}", "warning")
            batches.append(result)
            self.storage.update_job(job["job_id"], elapsed_seconds=job["elapsed_seconds"] + time.monotonic() - started)
        self.storage.update_job(job["job_id"], phase="extracting")
        job["phase"] = "extracting"
        for sequence, window in enumerate(windows(corpus)):
            extract(window, sequence)
        self._check_stop(job)
        job["phase"] = "resolving"
        self.storage.update_job(job["job_id"], phase="resolving")
        total = sum(len(b["entities"]) for b in batches)
        for sequence in range(total):
            uid = f"resolve:{sequence}"
            if not self.storage.unit(job["version"], uid):
                self.storage.save_unit(job["version"], uid, "resolve", sequence, "", {}, "pending")
        def checkpoint(unit_id, input_hash, result=None, sequence=0):
            if result is not None:
                self.storage.save_unit(job["version"], unit_id, "resolve", sequence, input_hash, result)
                return
            self._check_stop(job)
            saved = self.storage.unit(job["version"], unit_id)
            if saved and saved["status"] == "completed":
                if saved["input_hash"] != input_hash:
                    raise ValueError("resolution_checkpoint_changed")
                return saved["result"]
        entities, relations = resolve(batches, job["version"],
            lambda uid, schema, prompt, payload: self._invoke(job, model, uid, schema, prompt, payload),
            checkpoint, lambda code: self.storage.event(job["job_id"], code, "warning"))
        self._check_stop(job)
        self.storage.update_job(job["job_id"], phase="materializing")
        self.storage.materialize(job["version"], entities, relations)
        with self.lock:
            self._check_stop(job)
            if source_identity(item) != json.loads(self._version_source(job["version"])):
                raise ValueError("graph_source_changed")
            self.storage.update_job(job["job_id"], phase="publishing", elapsed_seconds=job["elapsed_seconds"] + time.monotonic() - started)
            if not self.storage.publish(job["novel_id"], job["version"], job["job_id"]):
                raise BuildStopped()

    def _version_source(self, version):
        with self.storage.connect() as db:
            return db.execute("SELECT source_identity FROM graph_versions WHERE version=?", (version,)).fetchone()[0]

    def _worker(self):
        while not self.shutdown.is_set():
            self.wake.wait(0.5)
            self.wake.clear()
            while not self.shutdown.is_set():
                try:
                    with self.lock, self.storage.connect() as db:
                        row = db.execute("SELECT * FROM graph_jobs WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
                        if row is None:
                            break
                        job = dict(row)
                        db.execute("UPDATE graph_jobs SET status='building',updated_at=? WHERE job_id=?", (now(), job["job_id"]))
                    try:
                        self.running_since = (job["job_id"], time.monotonic(), job["elapsed_seconds"])
                        self._run(job)
                    except BuildStopped:
                        self.storage.update_job(job["job_id"], status="stopped", error_code="service_interrupted" if self.shutdown.is_set() else None)
                    except Exception as exc:
                        error_type = type(exc).__name__.lower()
                        code = exc.code if isinstance(exc, GraphError) else (
                            "graph_model_timeout" if is_model_timeout(exc) else
                            "graph_model_authentication_failed" if "authentication" in error_type else
                            "graph_model_rate_limited" if "ratelimit" in error_type else
                            "graph_model_connection_failed" if "connection" in error_type else
                            "graph_structured_output_failed" if "structuredoutput" in error_type else "graph_build_failed")
                        self.storage.update_job(job["job_id"], status="failed", error_code=code)
                        self.storage.event(job["job_id"], code, "error")
                        LOGGER.warning("Graph job %s failed (%s)", job["job_id"], type(exc).__name__)
                    finally:
                        clock = self.running_since
                        if clock:
                            self.storage.update_job(job["job_id"], elapsed_seconds=clock[2] + time.monotonic() - clock[1])
                        self.running_since = None
                except sqlite3.Error:
                    self.error_code = "graph_storage_unavailable"
                    return
