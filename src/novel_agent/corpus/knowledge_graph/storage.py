from __future__ import annotations

import json
import sqlite3
import time
from functools import wraps
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import GraphError
from .ontology import digest, normalize


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def retry_busy(operation):
    """Retry rolled-back storage operations without re-invoking an extraction model."""
    @wraps(operation)
    def wrapped(*args, **kwargs):
        for attempt in range(3):
            try:
                return operation(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if attempt == 2 or not any(word in str(exc).lower() for word in ("locked", "busy")):
                    raise
                time.sleep(0.1 * (2 ** attempt))
    return wrapped


SCHEMA = """
CREATE TABLE graph_versions (
 version TEXT PRIMARY KEY, novel_id TEXT NOT NULL, state TEXT NOT NULL,
 fingerprint TEXT NOT NULL, source_identity TEXT NOT NULL, model_identity TEXT NOT NULL,
 created_at TEXT NOT NULL, published_at TEXT, counts_json TEXT NOT NULL DEFAULT '{}');
CREATE UNIQUE INDEX graph_version_scope ON graph_versions(novel_id,version);
CREATE TABLE graph_novels (
 novel_id TEXT PRIMARY KEY, active_version TEXT REFERENCES graph_versions(version),
 FOREIGN KEY(novel_id,active_version) REFERENCES graph_versions(novel_id,version));
CREATE TABLE graph_jobs (
 job_id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, version TEXT NOT NULL REFERENCES graph_versions(version),
 status TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'extracting', stop_requested INTEGER NOT NULL DEFAULT 0,
 error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, elapsed_seconds REAL NOT NULL DEFAULT 0,
 FOREIGN KEY(novel_id,version) REFERENCES graph_versions(novel_id,version));
CREATE UNIQUE INDEX graph_one_job_per_novel ON graph_jobs(novel_id)
 WHERE status IN ('queued','building','stopping');
CREATE UNIQUE INDEX graph_one_worker ON graph_jobs((1)) WHERE status IN ('building','stopping');
CREATE INDEX graph_job_queue ON graph_jobs(status, created_at);
CREATE TABLE graph_units (
 version TEXT NOT NULL REFERENCES graph_versions(version), unit_id TEXT NOT NULL, kind TEXT NOT NULL,
 sequence INTEGER NOT NULL, input_hash TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(version, unit_id));
CREATE TABLE graph_attempts (
 attempt_id TEXT PRIMARY KEY, version TEXT NOT NULL REFERENCES graph_versions(version), unit_id TEXT NOT NULL,
 status TEXT NOT NULL, usage_json TEXT, elapsed_ms REAL, error_code TEXT);
CREATE TABLE graph_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES graph_jobs(job_id),
 code TEXT NOT NULL, level TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE graph_entities (
 version TEXT NOT NULL REFERENCES graph_versions(version), entity_id TEXT NOT NULL,
 type TEXT NOT NULL, name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 first_section_order INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(version,entity_id));
CREATE INDEX graph_names ON graph_entities(version,normalized_name,type);
CREATE TABLE graph_aliases (
 version TEXT NOT NULL, entity_id TEXT NOT NULL, alias TEXT NOT NULL, normalized_alias TEXT NOT NULL,
 PRIMARY KEY(version,entity_id,normalized_alias),
 FOREIGN KEY(version,entity_id) REFERENCES graph_entities(version,entity_id) ON DELETE CASCADE);
CREATE INDEX graph_alias_names ON graph_aliases(version,normalized_alias);
CREATE TABLE graph_mentions (
 version TEXT NOT NULL, mention_id TEXT NOT NULL, entity_id TEXT NOT NULL,
 section_order INTEGER NOT NULL, start_char INTEGER NOT NULL, end_char INTEGER NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(version,mention_id),
 FOREIGN KEY(version,entity_id) REFERENCES graph_entities(version,entity_id) ON DELETE CASCADE);
CREATE INDEX graph_mention_range ON graph_mentions(version,entity_id,section_order);
CREATE TABLE graph_relations (
 version TEXT NOT NULL, relation_id TEXT NOT NULL, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
 relation_type TEXT NOT NULL, polarity TEXT NOT NULL, statement_mode TEXT NOT NULL,
 PRIMARY KEY(version,relation_id), CHECK(source_id != target_id),
 UNIQUE(version,source_id,relation_type,target_id,polarity,statement_mode),
 FOREIGN KEY(version,source_id) REFERENCES graph_entities(version,entity_id),
 FOREIGN KEY(version,target_id) REFERENCES graph_entities(version,entity_id));
CREATE INDEX graph_sources ON graph_relations(version,source_id);
CREATE INDEX graph_targets ON graph_relations(version,target_id);
CREATE TABLE graph_evidence_groups (
 version TEXT NOT NULL, group_id TEXT NOT NULL, relation_id TEXT NOT NULL,
 first_section INTEGER NOT NULL, last_section INTEGER NOT NULL, evidence_hash TEXT NOT NULL,
 PRIMARY KEY(version,group_id), UNIQUE(version,relation_id,evidence_hash),
 FOREIGN KEY(version,relation_id) REFERENCES graph_relations(version,relation_id) ON DELETE CASCADE);
CREATE INDEX graph_group_relation ON graph_evidence_groups(version,relation_id,first_section,last_section);
CREATE TABLE graph_evidence_spans (
 version TEXT NOT NULL, group_id TEXT NOT NULL, span_index INTEGER NOT NULL,
 section_order INTEGER NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(version,group_id,span_index),
 FOREIGN KEY(version,group_id) REFERENCES graph_evidence_groups(version,group_id) ON DELETE CASCADE);
PRAGMA user_version=1;
"""


class GraphStorage:
    def __init__(self, root: Path):
        self.root = root / "knowledge_graph"
        self.path = self.root / "graph.sqlite3"
        self.lock_file = None

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def open(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_file = (self.root / "process.lock").open("a+b")
        self.lock_file.seek(0)
        self.lock_file.write(b"0")
        self.lock_file.flush()
        self.lock_file.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock_file.close()
            self.lock_file = None
            raise GraphError("graph_process_busy", "另一服务进程正在使用图谱。", 503) from exc
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.executescript(SCHEMA)
            elif version != 1:
                raise GraphError("graph_storage_unavailable", "图谱数据库版本不兼容。", 503)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                raise GraphError("graph_storage_unavailable", "图谱数据库校验失败。", 503)
            jobs = db.execute("SELECT job_id FROM graph_jobs WHERE status IN ('queued','building','stopping')").fetchall()
            db.execute("UPDATE graph_jobs SET status='stopped',error_code='service_interrupted',updated_at=? WHERE status IN ('queued','building','stopping')", (now(),))
            db.execute("UPDATE graph_attempts SET status='interrupted',error_code='service_interrupted' WHERE status='started'")
            for job in jobs:
                db.execute("INSERT INTO graph_events(job_id,code,level,created_at) VALUES(?,?,?,?)", (job[0], "service_interrupted", "warning", now()))

    def close(self):
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def active(self, novel_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT active_version FROM graph_novels WHERE novel_id=?", (novel_id,)).fetchone()
            return row[0] if row else None

    def job(self, novel_id: str, job_id: str | None = None) -> dict | None:
        with self.connect() as db:
            sql = "SELECT * FROM graph_jobs WHERE novel_id=?"
            params = [novel_id]
            if job_id:
                sql += " AND job_id=?"
                params.append(job_id)
            row = db.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT 1", params).fetchone()
            return dict(row) if row else None

    @retry_busy
    def update_job(self, job_id: str, **fields):
        allowed = {"status", "phase", "error_code", "stop_requested", "elapsed_seconds"}
        if not fields.keys() <= allowed:
            raise ValueError("invalid_job_update")
        fields["updated_at"] = now()
        with self.connect() as db:
            db.execute("UPDATE graph_jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE job_id=?", [*fields.values(), job_id])

    @retry_busy
    def event(self, job_id: str, code: str, level: str = "info"):
        with self.connect() as db:
            db.execute("INSERT INTO graph_events(job_id,code,level,created_at) VALUES(?,?,?,?)", (job_id, code, level, now()))

    def unit(self, version: str, unit_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM graph_units WHERE version=? AND unit_id=?", (version, unit_id)).fetchone()
            return {**dict(row), "result": json.loads(row["result_json"])} if row else None

    @retry_busy
    def save_unit(self, version: str, unit_id: str, kind: str, sequence: int, input_hash: str, result: dict, status="completed"):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO graph_units VALUES(?,?,?,?,?,?,?)", (version, unit_id, kind, sequence, input_hash, status, encode(result)))

    @retry_busy
    def split_unit(self, version: str, unit_id: str, children: list[tuple[str, str]], sequence: int, *, min_chars: int = 500):
        """Persist both halves before requesting either, including across restarts."""
        with self.connect() as db:
            db.execute("UPDATE graph_units SET status='split',result_json=? WHERE version=? AND unit_id=?", (encode({"split_min_chars": min_chars}), version, unit_id))
            for child_id, input_hash in children:
                db.execute("INSERT OR IGNORE INTO graph_units VALUES(?,?,?,?,?,'pending','{}')", (version, child_id, "extract", sequence, input_hash))

    @retry_busy
    def start_attempt(self, attempt_id: str, version: str, unit_id: str):
        with self.connect() as db:
            db.execute("INSERT INTO graph_attempts(attempt_id,version,unit_id,status) VALUES(?,?,?,'started')", (attempt_id, version, unit_id))

    @retry_busy
    def finish_attempt(self, attempt_id: str, elapsed_ms: float, usage: dict | None = None, error_code: str | None = None):
        with self.connect() as db:
            db.execute("UPDATE graph_attempts SET status=?,usage_json=?,elapsed_ms=?,error_code=? WHERE attempt_id=?",
                ("failed" if error_code else "completed", encode(usage) if usage is not None else None, elapsed_ms, error_code, attempt_id))

    @retry_busy
    def materialize(self, version: str, entities: list[dict], relations: list[dict]):
        with self.connect() as db:
            for table in ("graph_evidence_spans", "graph_evidence_groups", "graph_relations", "graph_mentions", "graph_aliases", "graph_entities"):
                db.execute(f"DELETE FROM {table} WHERE version=?", (version,))
            for e in entities:
                db.execute("INSERT INTO graph_entities VALUES(?,?,?,?,?,?,?)", (version, e["id"], e["type"], e["name"], normalize(e["name"]), min(m["section_order"] for m in e["mentions"]), encode({"aliases": e["aliases"]})))
                for alias in dict.fromkeys([e["name"], *e["aliases"]]):
                    db.execute("INSERT OR IGNORE INTO graph_aliases VALUES(?,?,?,?)", (version, e["id"], alias, normalize(alias)))
                for mention in e["mentions"]:
                    mid = digest([mention["start_char"], mention["end_char"], e["type"]])
                    db.execute("INSERT OR IGNORE INTO graph_mentions VALUES(?,?,?,?,?,?,?)", (version, mid, e["id"], mention["section_order"], mention["start_char"], mention["end_char"], encode(mention)))
            for r in relations:
                db.execute("INSERT INTO graph_relations VALUES(?,?,?,?,?,?,?)", (version, r["id"], r["source"], r["target"], r["type"], r["polarity"], r["statement_mode"]))
                for group in r["groups"]:
                    evidence_hash = digest(sorted((s["start_char"], s["end_char"]) for s in group))
                    gid = digest([r["id"], evidence_hash])
                    db.execute("INSERT OR IGNORE INTO graph_evidence_groups VALUES(?,?,?,?,?,?)", (version, gid, r["id"], min(s["section_order"] for s in group), max(s["section_order"] for s in group), evidence_hash))
                    for i, span in enumerate(group):
                        db.execute("INSERT OR IGNORE INTO graph_evidence_spans VALUES(?,?,?,?,?)", (version, gid, i, span["section_order"], encode(span)))
            db.execute("UPDATE graph_versions SET counts_json=? WHERE version=?", (encode({"entity_count": len(entities), "relation_count": len(relations)}), version))

    @retry_busy
    def publish(self, novel_id: str, version: str, job_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT stop_requested FROM graph_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row[0]:
                return False
            if not db.execute("SELECT 1 FROM graph_versions WHERE novel_id=? AND version=? AND state='staging'", (novel_id, version)).fetchone():
                raise ValueError("graph_version_not_staging")
            if db.execute("SELECT 1 FROM graph_units WHERE version=? AND status NOT IN ('completed','split') LIMIT 1", (version,)).fetchone():
                raise ValueError("graph_units_incomplete")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("graph_foreign_key_failed")
            missing = db.execute("SELECT 1 FROM graph_relations r WHERE version=? AND NOT EXISTS (SELECT 1 FROM graph_evidence_groups g WHERE g.version=r.version AND g.relation_id=r.relation_id) LIMIT 1", (version,)).fetchone()
            if missing:
                raise ValueError("graph_evidence_missing")
            db.execute("UPDATE graph_versions SET state='superseded' WHERE novel_id=? AND state='published'", (novel_id,))
            db.execute("UPDATE graph_versions SET state='published',published_at=? WHERE novel_id=? AND version=? AND state='staging'", (now(), novel_id, version))
            db.execute("INSERT INTO graph_novels VALUES(?,?) ON CONFLICT(novel_id) DO UPDATE SET active_version=excluded.active_version", (novel_id, version))
            db.execute("UPDATE graph_jobs SET status='completed',updated_at=? WHERE job_id=?", (now(), job_id))
        return True
