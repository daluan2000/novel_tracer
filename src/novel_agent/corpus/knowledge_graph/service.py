from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
import threading
import time
import uuid
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from langchain_core.messages import HumanMessage, SystemMessage

from novel_agent.runtime.config import (ModelConfig, structured_output_retries, graph_request_timeout_seconds,
    graph_request_retries, graph_max_output_tokens, graph_extraction_concurrency,
    graph_resolution_concurrency, graph_rate_limit_retries, graph_min_entity_mentions)
from novel_agent.runtime.structured_output import invoke_structured
from .extraction import (EXTRACTION_PROMPT, COMPACT_EXTRACTION_PROMPT, validate_batch,
    validate_compact_batch, core_candidate_counts, compact_window, windows, split_window)
from .concurrency import BuildStopped, parallel_map
from .models import (BatchResolutionDecision, ExtractionBatch, CompactExtractionBatch,
                     GraphError, GraphOutputLimitError, ResolutionDecision)
from .ontology import PARSER_VERSION, RULE_VERSION, digest, ontology_payload
from .output_contracts import output_contract
from .query import GraphReadSession
from .resolution import (BATCH_RESOLUTION_PROMPT, IDENTITY_PROMPT, RESOLUTION_PROMPT,
                         RESOLUTION_BATCH_SIZE, resolve)
from .selection import select_candidates, select_resolved
from .storage import GraphStorage, encode, now

LOGGER = logging.getLogger(__name__)
ACTIVE = {"queued", "building", "stopping"}


def rate_limit_delay(error: BaseException, retry: int) -> float | None:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        response = getattr(error, "response", None)
        if (getattr(error, "status_code", None) == 429
                or getattr(response, "status_code", None) == 429
                or "ratelimit" in type(error).__name__.lower()):
            headers = getattr(response, "headers", None) or getattr(error, "headers", None) or {}
            raw = headers.get("retry-after") or headers.get("Retry-After")
            if raw is not None:
                try:
                    seconds = float(raw)
                except (ValueError, TypeError):
                    try:
                        date = parsedate_to_datetime(raw)
                        if date.tzinfo is None:
                            date = date.replace(tzinfo=timezone.utc)
                        seconds = (date - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        seconds = -1
                if math.isfinite(seconds) and seconds >= 0:
                    return seconds
            return min(2 ** retry, 30) + random.uniform(0, 1)
        error = error.__cause__ or error.__context__
    return None


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
        self.phase_since: tuple[str, str, float] | None = None
        self.abort = threading.Event()
        self.cooldown_lock = threading.Lock()
        self.cooldown_until = 0.0

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
            graph_extraction_concurrency()
            graph_resolution_concurrency()
            graph_rate_limit_retries()
            graph_min_entity_mentions()
            return config
        except (ValueError, RuntimeError) as exc:
            raise GraphError("graph_model_not_configured", "请先配置有效的聊天模型。") from exc

    def _fingerprint(self, item, config):
        return digest({"source": source_identity(item), "model": model_identity(config),
                       "rules": RULE_VERSION, "core_chars": 2500, "context_chars": 300,
                       "minimum_mentions": graph_min_entity_mentions(), "resolution_batch_size": RESOLUTION_BATCH_SIZE,
                       "prompts": digest([EXTRACTION_PROMPT, COMPACT_EXTRACTION_PROMPT, RESOLUTION_PROMPT, IDENTITY_PROMPT, BATCH_RESOLUTION_PROMPT]), "ontology": ontology_payload(),
                       "schemas": digest([ExtractionBatch.model_json_schema(), CompactExtractionBatch.model_json_schema(),
                                          ResolutionDecision.model_json_schema(), BatchResolutionDecision.model_json_schema()])})

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
                  "extraction_concurrency": graph_extraction_concurrency() if config else None,
                  "resolution_concurrency": graph_resolution_concurrency() if config else None,
                  "rate_limit_retries": graph_rate_limit_retries() if config else None,
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
                        counts = json.loads(row[0])
                        result["counts"] = {key: counts[key] for key in ("entity_count", "relation_count")}
        except sqlite3.Error:
            self.error_code = "graph_storage_unavailable"
            result.update(available=False, error_code=self.error_code)
        return result

    def job_payload(self, job, item=None, config=None):
        version = job["version"]
        with self.storage.connect() as db:
            units = db.execute("SELECT kind,status,COUNT(*) n FROM graph_units WHERE version=? GROUP BY kind,status", (version,)).fetchall()
            counts = {(u["kind"], u["status"]): u["n"] for u in units}
            attempts = db.execute("SELECT unit_id,status,usage_json,elapsed_ms FROM graph_attempts WHERE version=?", (version,)).fetchall()
            model_seconds = {}
            usage = {"request_count": len(attempts), "reported_input_tokens": 0, "reported_output_tokens": 0,
                     "unknown_request_count": 0, "failed_request_count": 0}
            for a in attempts:
                phase = "extracting" if a["unit_id"].startswith("extract:") else "resolving"
                model_seconds[phase] = model_seconds.get(phase, 0) + (a["elapsed_ms"] or 0) / 1000
                u = json.loads(a["usage_json"]) if a["usage_json"] else {}
                usage["reported_input_tokens"] += u.get("input_tokens") or 0
                usage["reported_output_tokens"] += u.get("output_tokens") or 0
                usage["unknown_request_count"] += int(u.get("input_tokens") is None or u.get("output_tokens") is None)
                usage["failed_request_count"] += int(a["status"] in {"failed", "interrupted"})
            version_row = db.execute("SELECT fingerprint,counts_json FROM graph_versions WHERE version=?", (version,)).fetchone()
            totals = json.loads(version_row["counts_json"])
            events = [dict(e) for e in db.execute("SELECT code,level,created_at FROM graph_events WHERE job_id=? ORDER BY event_id DESC LIMIT 50", (job["job_id"],))]
            quality = totals.get("quality_stats")
            if quality is not None:
                quality["ambiguous_count"] = db.execute("SELECT COUNT(*) FROM graph_events WHERE job_id=? AND code='entity_identity_ambiguous'", (job["job_id"],)).fetchone()[0]
                quality["identity_rejected_count"] = quality["filtered_reasons"].get("identity_evidence", 0) + db.execute(
                    "SELECT COUNT(*) FROM graph_events WHERE job_id=? AND code IN ('entity_identity_evidence_rejected','entity_identity_conflict_rejected')", (job["job_id"],)).fetchone()[0]
                quality["batch_request_count"] = sum(a["unit_id"].startswith("resolve:batch:") for a in attempts)
        def progress(kind):
            result = {"completed": counts.get((kind, "completed"), 0),
                      "total": sum(n for (k, state), n in counts.items() if k == kind and state != "split")}
            if counts.get((kind, "extracted"), 0):
                result["cached"] = counts[(kind, "extracted")]
            return result
        elapsed = job["elapsed_seconds"]
        clock = self.running_since
        if clock and clock[0] == job["job_id"]:
            elapsed = clock[2] + time.monotonic() - clock[1]
        phase_seconds = self.storage.phase_times(job["job_id"])
        phase_clock = self.phase_since
        if phase_clock and phase_clock[0] == job["job_id"]:
            phase_seconds[phase_clock[1]] = phase_seconds.get(phase_clock[1], 0) + time.monotonic() - phase_clock[2]
        return {"job_id": job["job_id"], "graph_version": version, "status": job["status"], "phase": job["phase"],
                "stop_requested": bool(job["stop_requested"]), "extraction": progress("extract"),
                "resolution": progress("resolve"), "entity_count": totals.get("entity_count", 0),
                "relation_count": totals.get("relation_count", 0), "usage": usage,
                "elapsed_seconds": elapsed, "error_code": job["error_code"], "events": events,
                "phase_elapsed_seconds": phase_seconds, "phase_model_seconds": model_seconds,
                "quality_stats": quality,
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
        if self.shutdown.is_set() or self.abort.is_set() or latest["stop_requested"]:
            raise BuildStopped()

    def _wait_for_retry(self, job, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self._check_stop(job)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self.shutdown.wait(min(remaining, 0.1)):
                raise BuildStopped()

    def _wait_for_cooldown(self, job):
        while True:
            self._check_stop(job)
            with self.cooldown_lock:
                remaining = self.cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            self._wait_for_retry(job, remaining)

    def _finish_phase(self):
        with self.lock:
            clock = self.phase_since
            if clock:
                self.storage.add_phase_time(clock[0], clock[1], time.monotonic() - clock[2])
                self.phase_since = None

    def _set_phase(self, job, phase):
        with self.lock:
            self._finish_phase()
            self.storage.update_job(job["job_id"], phase=phase)
            job["phase"] = phase
            self.phase_since = (job["job_id"], phase, time.monotonic())

    def _invoke(self, job, model, unit_id, schema, prompt, payload, *, timeout_retries=None):
        self._check_stop(job)
        service = self
        retries = graph_request_retries() if timeout_retries is None else timeout_retries
        class TrackedRunnable:
            def invoke(self, messages):
                timeout_attempts, rate_attempts = 0, 0
                while True:
                    service._check_stop(job)
                    service._wait_for_cooldown(job)
                    attempt = uuid.uuid4().hex
                    service.storage.start_attempt(attempt, job["version"], unit_id)
                    started = time.monotonic()
                    try:
                        response = runnable.invoke(messages)
                    except Exception as exc:
                        timed_out = is_model_timeout(exc)
                        delay = rate_limit_delay(exc, rate_attempts)
                        service.storage.finish_attempt(attempt, (time.monotonic() - started) * 1000,
                            error_code="graph_model_rate_limited" if delay is not None else "graph_model_timeout" if timed_out else "graph_model_output_limit" if is_output_limit(exc) else "model_request_failed")
                        service._check_stop(job)
                        if delay is not None:
                            with service.cooldown_lock:
                                service.cooldown_until = max(service.cooldown_until, time.monotonic() + delay)
                            if rate_attempts >= graph_rate_limit_retries():
                                raise GraphError("graph_model_rate_limited", "模型请求持续受到限流，已保存进度。") from exc
                            rate_attempts += 1
                            service.storage.event(job["job_id"], "graph_model_rate_limit_retry", "warning")
                            continue
                        if is_output_limit(exc):
                            raise GraphOutputLimitError() from exc
                        if not timed_out or timeout_attempts >= retries:
                            raise
                        service.storage.event(job["job_id"], "graph_model_timeout_retry", "warning")
                        service._wait_for_retry(job, min(2 ** timeout_attempts, 4))
                        timeout_attempts += 1
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
        def diagnostic(data):
            self.storage.event(job["job_id"], data["diagnostic_code"], "warning")
            LOGGER.warning("Graph structured output job=%s unit=%s schema=%s attempt=%s reason=%s fields=%s",
                job["job_id"], unit_id, schema.__name__, data["attempt"], data["failure_reason"],
                data.get("validation_issues", []))
        # Format-only guidance does not invalidate completed semantic checkpoints.
        contract = output_contract(schema)
        request_prompt = prompt + "\n\n" + contract if contract else prompt
        return invoke_structured(TrackedRunnable(), [SystemMessage(content=request_prompt), HumanMessage(content=encode(payload))], schema,
            source_node="graph_" + job["phase"], retries=structured_output_retries(),
            on_diagnostic=diagnostic, sleeper=lambda seconds: self._wait_for_retry(job, seconds)).value

    def _run(self, job):
        self.abort.clear()
        with self.cooldown_lock:
            self.cooldown_until = 0.0
        try:
            self._run_job(job)
        finally:
            self._finish_phase()

    def _run_job(self, job):
        started = time.monotonic()
        item = self.novels.get(job["novel_id"])
        corpus = item.corpus
        config = self._config()
        with self.storage.connect() as db:
            expected = db.execute("SELECT fingerprint FROM graph_versions WHERE version=?", (job["version"],)).fetchone()[0]
        if expected != self._fingerprint(item, config):
            raise GraphError("graph_fingerprint_changed", "配置已变化，请重建。")
        graph_config = replace(config, request_timeout_seconds=graph_request_timeout_seconds(config.request_timeout_seconds))
        local = threading.local()

        def get_model():
            if not hasattr(local, "model"):
                local.model = self.model_factory() if self.model_factory else graph_config.create_model(
                    max_retries=0, max_tokens=graph_max_output_tokens())
            return local.model

        compact = threading.Event()
        with self.storage.connect() as db:
            had_output_limit = bool(db.execute("SELECT 1 FROM graph_attempts WHERE version=? AND error_code='graph_model_output_limit' LIMIT 1", (job["version"],)).fetchone())
            compact_event = db.execute("SELECT 1 FROM graph_events WHERE job_id=? AND code='graph_extraction_compact_fallback' LIMIT 1", (job["job_id"],)).fetchone()
        if had_output_limit:
            compact.set()
        if had_output_limit and not compact_event:
            self.storage.event(job["job_id"], "graph_extraction_compact_fallback", "warning")
        def split_extract(w, sequence, reason, min_chars=500, candidate_counts=None):
            children = split_window(corpus, w, min_chars=min_chars)
            self.storage.split_unit(job["version"], w.unit_id,
                [(child.unit_id, digest(child.payload(corpus))) for child in children], sequence,
                min_chars=min_chars, candidate_counts=candidate_counts)
            self.storage.event(job["job_id"], reason, "warning")
            for child in children:
                extract(child, sequence)

        def extract(w, sequence):
            self._check_stop(job)
            saved = self.storage.unit(job["version"], w.unit_id)
            if saved and saved["status"] == "completed":
                local.batches.append(saved["result"])
                return
            if saved and saved["status"] == "split":
                for child in split_window(corpus, w, min_chars=saved["result"].get("split_min_chars", 500)):
                    extract(child, sequence)
                return
            if not saved:
                self.storage.save_unit(job["version"], w.unit_id, "extract", sequence, digest(w.payload(corpus)), {}, "pending")
            cached = saved["result"] if saved and saved["status"] == "extracted" else None
            if cached is not None and not isinstance(cached, dict):
                raise GraphError("graph_checkpoint_invalid", "已缓存的候选结构不兼容，请检查图谱数据。")
            previous_error = saved["result"].get("last_error_code") if saved else None
            if previous_error == "graph_model_output_limit":
                compact.set()
            use_compact = cached.get("format") == "compact-v1" if cached is not None else compact.is_set()
            minimum = 200 if use_compact else 500
            can_split = w.end - w.start >= minimum * 2
            if cached is None and can_split and previous_error == "graph_model_timeout":
                split_extract(w, sequence, "graph_extraction_timeout_split", minimum)
                return
            try:
                input_window, payload = compact_window(corpus, w) if use_compact else (w, w.payload(corpus))
                schema = CompactExtractionBatch if use_compact else ExtractionBatch
                if cached is not None:
                    if saved["input_hash"] != digest(w.payload(corpus)) or cached.get("format") not in {"compact-v1", "full-v1"}:
                        raise GraphError("graph_checkpoint_invalid", "已缓存片段与当前原文或格式不匹配。")
                    try:
                        batch = schema.model_validate(cached["batch"])
                    except (KeyError, ValueError, TypeError) as exc:
                        raise GraphError("graph_checkpoint_invalid", "已缓存的候选结构不兼容，请检查图谱数据。") from exc
                else:
                    batch = self._invoke(job, get_model(), w.unit_id, schema,
                        COMPACT_EXTRACTION_PROMPT if use_compact else EXTRACTION_PROMPT, payload,
                        timeout_retries=0 if can_split else None)
                    # Keep the successfully parsed response before any local validation,
                    # density decision or materialization can fail. This is schema-checked
                    # candidate data, never a supplier's raw response or credentials.
                    self.storage.save_unit(job["version"], w.unit_id, "extract", sequence, digest(w.payload(corpus)),
                        {"format": "compact-v1" if use_compact else "full-v1", "core_chars": w.end - w.start,
                         "batch": batch.model_dump()}, "extracted")
            except Exception as exc:
                if not is_model_timeout(exc) and not is_output_limit(exc):
                    raise
                self._check_stop(job)
                if is_output_limit(exc):
                    compact.set()
                    if not use_compact:
                        self.storage.event(job["job_id"], "graph_extraction_compact_fallback", "warning")
                        if w.end - w.start >= 1000:
                            split_extract(w, sequence, "graph_extraction_output_split", 200)
                        else:
                            extract(w, sequence)
                        return
                if not can_split:
                    raise
                split_extract(w, sequence, "graph_extraction_output_split" if is_output_limit(exc) else "graph_extraction_timeout_split", minimum)
                return
            try:
                result = validate_compact_batch(corpus, input_window, batch) if use_compact else validate_batch(corpus, w, batch)
                counts = core_candidate_counts(result, w)
                limits = (32, 48, 12) if use_compact else (80, 120, 40)
                saturated = any(size >= limit if use_compact else size > limit for size, limit in zip(counts.values(), limits))
                if saturated and can_split:
                    self._check_stop(job)
                    split_extract(w, sequence, "graph_extraction_capacity_split", minimum, counts)
                    return
                result["diagnostics"] = {"candidate_counts": counts, "core_chars": w.end - w.start,
                    "capacity_saturated": saturated, "at_minimum_window": not can_split}
                if saturated:
                    # A full, valid response at the minimum window is usable. Keep all
                    # validated candidates, disclose density, and never split below policy.
                    self.storage.event(job["job_id"], "graph_extraction_capacity_saved", "warning")
            except BuildStopped:
                raise
            except GraphError:
                raise
            except Exception as exc:
                self.storage.event(job["job_id"], f"graph_processing_failure:{w.unit_id}:{type(exc).__name__}", "error")
                raise GraphError("graph_extraction_processing_failed", "抽取结果处理失败，已缓存成功返回的候选。") from exc
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
            local.batches.append(result)
        def extract_root(item):
            sequence, window = item
            local.batches = []
            extract(window, sequence)
            return local.batches

        self._set_phase(job, "extracting")
        root_results = parallel_map(extract_root, enumerate(windows(corpus)), graph_extraction_concurrency(),
                                    check=lambda: self._check_stop(job), on_error=self.abort.set)
        batches = [batch for results in root_results for batch in results]
        self._check_stop(job)
        minimum = graph_min_entity_mentions()
        batches, quality = select_candidates(batches, minimum)
        self.storage.quality_stats(job["version"], quality)
        self._set_phase(job, "resolving")
        total = sum(len(b["entities"]) for b in batches)
        resolution_units = self.storage.prepare_resolution(job["version"], total,
            sum(len(b["identity_links"]) for b in batches))
        checkpoint_lock = threading.Lock()
        def checkpoint(unit_id, input_hash, result=None, sequence=0):
            if result is not None:
                kind = "resolve_batch" if unit_id.startswith("resolve:batch:") else "resolve_prediction" if unit_id.startswith("resolve:prediction:") else "resolve"
                self.storage.save_unit(job["version"], unit_id, kind, sequence, input_hash, result)
                with checkpoint_lock:
                    resolution_units[unit_id] = {"input_hash": input_hash, "result": result}
                return
            self._check_stop(job)
            with checkpoint_lock:
                saved = resolution_units.get(unit_id)
            if saved is not None:
                if saved["input_hash"] != input_hash:
                    raise ValueError("resolution_checkpoint_changed")
                return saved["result"]
        def save_predictions(records):
            self.storage.save_predictions(job["version"], records)
            with checkpoint_lock:
                resolution_units.update({uid: {"input_hash": fingerprint, "result": result}
                                         for uid, fingerprint, result, _ in records})
        checkpoint.save_many = save_predictions
        entities, relations = resolve(batches, job["version"],
            lambda uid, schema, prompt, payload: self._invoke(job, get_model(), uid, schema, prompt, payload,
                timeout_retries=0 if schema is BatchResolutionDecision and len(payload["entities"]) > 1 else None),
            checkpoint, lambda code: self.storage.event(job["job_id"], code, "warning"),
            concurrency=graph_resolution_concurrency(), check=lambda: self._check_stop(job), on_error=self.abort.set,
            batch_size=RESOLUTION_BATCH_SIZE)
        before_final_selection = len(entities)
        entities, relations = select_resolved(entities, relations, minimum)
        quality["filtered_reasons"]["low_frequency_after_resolution"] = before_final_selection - len(entities)
        quality["entities_after"] = {kind: sum(e["type"] == kind for e in entities)
                                     for kind in ontology_payload()["entity_types"]}
        self.storage.quality_stats(job["version"], quality)
        self._check_stop(job)
        self._set_phase(job, "materializing")
        self.storage.materialize(job["version"], entities, relations)
        with self.lock:
            self._check_stop(job)
            if source_identity(item) != json.loads(self._version_source(job["version"])):
                raise ValueError("graph_source_changed")
            self._set_phase(job, "publishing")
            self.storage.update_job(job["job_id"], elapsed_seconds=job["elapsed_seconds"] + time.monotonic() - started)
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
                        latest = self.storage.job(job["novel_id"], job["job_id"])
                        if self.shutdown.is_set() or latest["stop_requested"]:
                            self.storage.update_job(job["job_id"], status="stopped",
                                error_code="service_interrupted" if self.shutdown.is_set() else None)
                        else:
                            self.storage.update_job(job["job_id"], status="failed", error_code=code)
                            self.storage.event(job["job_id"], code, "error")
                        frames, seen, failure = [], set(), exc
                        while failure is not None and id(failure) not in seen:
                            seen.add(id(failure))
                            frames.extend(traceback.extract_tb(failure.__traceback__))
                            failure = failure.__cause__ or failure.__context__
                        location = " -> ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames)
                        LOGGER.warning("Graph job %s failed (%s) at %s", job["job_id"], type(exc).__name__, location)
                    finally:
                        clock = self.running_since
                        if clock:
                            self.storage.update_job(job["job_id"], elapsed_seconds=clock[2] + time.monotonic() - clock[1])
                        self.running_since = None
                except sqlite3.Error:
                    self.error_code = "graph_storage_unavailable"
                    return
