"""Immutable graph snapshots and source-grounded manual merge rules."""
from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict

from .models import GraphError
from .ontology import SYMMETRIC, digest, normalize
from .storage import encode, now, retry_busy


def mention_key(mention, kind):
    return mention["start_char"], mention["end_char"], kind


def load_snapshot(db, version):
    entities = {}
    for row in db.execute("SELECT * FROM graph_entities WHERE version=? ORDER BY entity_id", (version,)):
        entities[row["entity_id"]] = {**json.loads(row["payload"]), "id": row["entity_id"],
            "name": row["name"], "type": row["type"], "mentions": []}
    for row in db.execute("SELECT * FROM graph_mentions WHERE version=? ORDER BY start_char,mention_id", (version,)):
        entities[row["entity_id"]]["mentions"].append(json.loads(row["payload"]))
    groups = defaultdict(list)
    spans = defaultdict(list)
    for row in db.execute("SELECT * FROM graph_evidence_spans WHERE version=? ORDER BY group_id,span_index", (version,)):
        spans[row["group_id"]].append(json.loads(row["payload"]))
    for row in db.execute("SELECT * FROM graph_evidence_groups WHERE version=? ORDER BY group_id", (version,)):
        groups[row["relation_id"]].append(spans[row["group_id"]])
    relations = [{"id": r["relation_id"], "source": r["source_id"], "target": r["target_id"],
                  "type": r["relation_type"], "polarity": r["polarity"], "statement_mode": r["statement_mode"],
                  "groups": groups[r["relation_id"]]}
                 for r in db.execute("SELECT * FROM graph_relations WHERE version=? ORDER BY relation_id", (version,))]
    bindings = [json.loads(r[0]) for r in db.execute("SELECT payload FROM graph_source_mentions WHERE version=? ORDER BY mention_id", (version,))]
    facts = [json.loads(r[0]) for r in db.execute("SELECT payload FROM graph_fact_candidates WHERE version=? ORDER BY candidate_id", (version,))]
    return {"entities": entities, "relations": relations, "bindings": bindings, "facts": facts}


def merge_snapshot(snapshot, source_id, target_id, version, canonical_name=None):
    """Compatibility wrapper for two-entity callers and old recorded fixtures."""
    return merge_entities_snapshot(snapshot, [source_id, target_id], target_id, version, canonical_name)


def rename_entity(entity, canonical_name):
    aliases = {normalize(name): name for name in [*entity["aliases"], entity["name"]]
               if normalize(name) != normalize(canonical_name)}
    entity["aliases"] = list(aliases.values())
    entity["name"] = canonical_name
    if not any(f["name"] == canonical_name for f in entity.get("name_forms", [])):
        entity.setdefault("name_forms", []).append({"name": canonical_name, "kind": "proper_name"})


def merge_entities_snapshot(snapshot, entity_ids, target_id, version, canonical_name=None):
    entities = snapshot["entities"]
    selected = set(entity_ids)
    if len(entity_ids) < 2 or len(selected) != len(entity_ids) or target_id not in selected:
        raise GraphError("graph_invalid_merge", "请至少选择两个不同实体。", 422)
    if selected - entities.keys():
        raise GraphError("graph_object_not_found", "实体不存在或不属于当前小说和版本。", 404)
    participants = [entities[eid] for eid in entity_ids]
    target = entities[target_id]
    if any(e["type"] != target["type"] for e in participants):
        raise GraphError("graph_invalid_merge", "只能合并同一类型的实体。", 422)
    before = len(snapshot["relations"])
    summaries = [{"id": e["id"], "name": e["name"], "aliases": list(e["aliases"]), "mention_count": len(e["mentions"])} for e in participants]
    target_before = len(target["mentions"])
    target["aliases"] = sorted({name for e in participants for name in [e["name"], *e["aliases"]]})
    forms = {normalize(f["name"]): f for e in [*participants, target] for f in e.get("name_forms", [])}
    for summary in summaries:
        name = summary["name"]
        forms.setdefault(normalize(name), {"name": name, "kind": "proper_name"})
    target["name_forms"] = list(forms.values())
    target["identity_evidence"] = sorted({mid for e in participants for mid in e.get("identity_evidence", [])})
    target["mentions"] = sorted({mention_key(m, target["type"]): m for e in participants
                                 for m in e["mentions"]}.values(), key=lambda m: m["start_char"])
    rename_entity(target, canonical_name or target["name"])
    for eid in selected - {target_id}:
        del entities[eid]
    remap = lambda eid: target_id if eid in selected else eid
    for binding in snapshot["bindings"]:
        binding["entity_id"] = remap(binding.get("entity_id"))
        binding["candidate_ids"] = sorted({remap(eid) for eid in binding.get("candidate_ids", [])})
    for fact in snapshot["facts"]:
        for side in ("source_id", "target_id"):
            fact[side] = remap(fact.get(side))
        if fact.get("source_id") is not None and fact.get("source_id") == fact.get("target_id"):
            fact.update(status="filtered", reason="manual_merge_self_relation")
    edges, self_edges = {}, 0
    for edge in snapshot["relations"]:
        left, right = remap(edge["source"]), remap(edge["target"])
        if left == right:
            self_edges += 1
            continue
        if edge["type"] in SYMMETRIC:
            left, right = sorted((left, right))
        key = (left, edge["type"], right, edge["polarity"], edge["statement_mode"])
        merged = edges.setdefault(key, {**edge, "id": digest([version, *key]), "source": left, "target": right, "groups": []})
        for group in edge["groups"]:
            if group not in merged["groups"]:
                merged["groups"].append(group)
    snapshot["relations"] = list(edges.values())
    sources = [e for e in summaries if e["id"] != target_id]
    return {"entities": summaries, "sources": sources,
            **({"source": sources[0]} if len(sources) == 1 else {}),
            "target": {"id": target_id, "name": target["name"], "aliases": target["aliases"],
                       "mention_count": len(target["mentions"]), "mention_count_before": target_before},
            "relation_count_before": before, "relation_count_after": len(edges), "self_relations_removed": self_edges}


class GraphRevisions:
    def __init__(self, storage):
        self.storage = storage

    def _version(self, db, novel_id, version):
        row = db.execute("SELECT * FROM graph_versions WHERE novel_id=? AND version=?", (novel_id, version)).fetchone()
        if not row:
            raise GraphError("graph_object_not_found", "图谱版本不存在或不属于当前小说。", 404)
        return row

    def _revision(self, db, version):
        row = db.execute("SELECT * FROM graph_revisions WHERE version=?", (version,)).fetchone()
        return dict(row) if row else {"corrections": "[]", "origin_job_id": None, "operation": "build"}

    def _check_write(self, db, novel_id, expected, identity):
        active = db.execute("SELECT active_version FROM graph_novels WHERE novel_id=?", (novel_id,)).fetchone()
        if not active or active[0] != expected:
            raise GraphError("graph_version_changed", "图谱已更新，请刷新后重试。")
        if db.execute("SELECT 1 FROM graph_jobs WHERE novel_id=? AND status IN ('queued','building','stopping')", (novel_id,)).fetchone():
            raise GraphError("graph_job_conflict", "构建期间不能修改或恢复图谱，请先停止构建。")
        row = self._version(db, novel_id, expected)
        if row["state"] != "published" or json.loads(row["source_identity"]) != identity:
            raise GraphError("graph_source_changed", "正文或原文位置映射已变化，请重新构建。")
        return row

    def capture_build(self, db, novel_id, version):
        active = db.execute("SELECT active_version FROM graph_novels WHERE novel_id=?", (novel_id,)).fetchone()
        parent = active[0] if active else None
        rules = self._revision(db, parent)["corrections"] if parent else "[]"
        db.execute("INSERT OR REPLACE INTO graph_build_bases VALUES(?,?,?)", (version, parent, rules))

    def metadata(self, db, version):
        revision = self._revision(db, version)
        rules = json.loads(revision["corrections"])
        return {"operation": revision["operation"], "pending_corrections": sum(r["status"] == "pending" for r in rules)}

    def history(self, novel_id, identity, offset=0, limit=20):
        with self.storage.connect() as db:
            active = self.storage.active(novel_id)
            total = db.execute("SELECT count(*) FROM graph_revisions r JOIN graph_versions v USING(version) WHERE novel_id=?", (novel_id,)).fetchone()[0]
            rows = db.execute("SELECT v.*,r.* FROM graph_revisions r JOIN graph_versions v USING(version) WHERE novel_id=? ORDER BY v.rowid DESC LIMIT ? OFFSET ?", (novel_id, limit, offset)).fetchall()
            items = []
            for row in rows:
                compatible = row["state"] in {"published", "superseded"} and json.loads(row["source_identity"]) == identity
                counts = json.loads(row["counts_json"])
                items.append({"version": row["version"], "parent_version": row["parent_version"], "operation": row["operation"],
                    "created_at": row["created_at"], "details": json.loads(row["details"]), "restored_from": row["restored_from"],
                    "entity_count": counts.get("entity_count", 0), "relation_count": counts.get("relation_count", 0),
                    "current": row["version"] == active, "can_restore": compatible and row["version"] != active,
                    "restore_blocked_reason": None if compatible else "正文或原文位置映射已变化，无法恢复此版本"})
        return {"items": items, "total": total, "offset": offset, "limit": limit, "graph_version": active}

    def corrections(self, novel_id, version):
        with self.storage.connect() as db:
            self._version(db, novel_id, version)
            return {"graph_version": version, "items": json.loads(self._revision(db, version)["corrections"])}

    def _clone(self, db, row):
        version = uuid.uuid4().hex
        db.execute("INSERT INTO graph_versions VALUES(?,?,'staging',?,?,?,?,NULL,?)", (version, row["novel_id"], row["fingerprint"],
                   row["source_identity"], row["model_identity"], now(), row["counts_json"]))
        for table in ("graph_entities", "graph_aliases", "graph_mentions", "graph_relations", "graph_evidence_groups", "graph_evidence_spans",
                      "graph_source_mentions", "graph_identity_decisions", "graph_fact_candidates"):
            columns = [r["name"] for r in db.execute(f"PRAGMA table_info({table})") if r["name"] != "version"]
            names = ",".join(columns)
            db.execute(f"INSERT INTO {table}(version,{names}) SELECT ?,{names} FROM {table} WHERE version=?", (version, row["version"]))
        return version

    def _save(self, db, version, snapshot):
        self.storage.materialize_in(db, version, list(snapshot["entities"].values()), snapshot["relations"])
        for binding in snapshot["bindings"]:
            db.execute("UPDATE graph_source_mentions SET entity_id=?,status=?,payload=? WHERE version=? AND mention_id=?",
                       (binding["entity_id"], binding["status"], encode(binding), version, binding["mention_id"]))
        for fact in snapshot["facts"]:
            db.execute("UPDATE graph_fact_candidates SET status=?,payload=? WHERE version=? AND candidate_id=?",
                       (fact["status"], encode(fact), version, fact["candidate_id"]))
        counts = json.loads(db.execute("SELECT counts_json FROM graph_versions WHERE version=?", (version,)).fetchone()[0])
        if "quality_stats" in counts:
            counts["quality_stats"]["entities_after"] = dict(Counter(e["type"] for e in snapshot["entities"].values()))
            counts["quality_stats"]["pending_facts"] = sum(f["status"] == "pending" for f in snapshot["facts"])
            db.execute("UPDATE graph_versions SET counts_json=? WHERE version=?", (encode(counts), version))

    def _publish(self, db, row, version, operation, rules, details, restored_from=None):
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("graph_foreign_key_failed")
        if db.execute("SELECT 1 FROM graph_source_mentions m WHERE m.version=? AND m.status='resolved' AND NOT EXISTS (SELECT 1 FROM graph_entities e WHERE e.version=m.version AND e.entity_id=m.entity_id)", (version,)).fetchone():
            raise ValueError("graph_identity_binding_invalid")
        if db.execute("SELECT 1 FROM graph_relations r WHERE r.version=? AND NOT EXISTS (SELECT 1 FROM graph_evidence_groups g WHERE g.version=r.version AND g.relation_id=r.relation_id)", (version,)).fetchone():
            raise ValueError("graph_evidence_missing")
        revision = self._revision(db, row["version"])
        db.execute("INSERT INTO graph_revisions VALUES(?,?,?,?,?,?,?)", (version, row["version"], operation, encode(details), encode(rules), revision["origin_job_id"], restored_from))
        db.execute("UPDATE graph_versions SET state='superseded' WHERE novel_id=? AND state='published'", (row["novel_id"],))
        db.execute("UPDATE graph_versions SET state='published',published_at=? WHERE version=?", (now(), version))
        db.execute("UPDATE graph_novels SET active_version=? WHERE novel_id=?", (version, row["novel_id"]))
        return {"graph_version": version, "operation": operation, "details": details}

    @retry_busy
    def merge(self, novel_id, body, identity, *, preview=False):
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE" if not preview else "BEGIN")
            row = self._check_write(db, novel_id, body.version, identity)
            snapshot = load_snapshot(db, body.version)
            rules = json.loads(self._revision(db, body.version)["corrections"])
            if body.replaces_correction_id:
                replaced = next((r for r in rules if r["id"] == body.replaces_correction_id and r["status"] in {"pending", "skipped"}), None)
                if not replaced:
                    raise GraphError("graph_correction_not_found", "待确认人工修正不存在。", 404)
                rules.remove(replaced)
            entity_ids = body.merge_ids
            # An internal anchor preserves IDs; users supply the resulting name.
            target_id = body.target_id or next((eid for eid in entity_ids if eid in snapshot["entities"]
                and normalize(snapshot["entities"][eid]["name"]) == normalize(body.canonical_name)), entity_ids[0])
            details = merge_entities_snapshot(snapshot, entity_ids, target_id, body.version, body.canonical_name)
            if preview:
                return {"graph_version": body.version, **details}
            names = [e["name"] for e in details["entities"]]
            rule = {"id": uuid.uuid4().hex, "basis_version": body.version, "entity_ids": entity_ids,
                    "target_entity_id": target_id, "entity_names": names, "source_name": "、".join(names), "target_name": details["target"]["name"],
                    "source_identity": identity, "status": "applied", "reason": None}
            rules.append(rule)
            version = self._clone(db, row)
            self._save(db, version, snapshot)
            return self._publish(db, row, version, "merge", rules, details)

    @retry_busy
    def restore(self, novel_id, expected, target_version, identity):
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._check_write(db, novel_id, expected, identity)
            target = self._version(db, novel_id, target_version)
            if target_version == expected or target["state"] not in {"published", "superseded"} or json.loads(target["source_identity"]) != identity:
                raise GraphError("graph_restore_unavailable", "此版本无法恢复，正文可能已变化或该版本尚未发布。")
            revision = self._revision(db, target_version)
            version = self._clone(db, target)
            result = self._publish(db, current, version, "restore", json.loads(revision["corrections"]), {"restored_from": target_version}, target_version)
            db.execute("UPDATE graph_revisions SET origin_job_id=? WHERE version=?", (revision["origin_job_id"], version))
            return result

    @retry_busy
    def skip(self, novel_id, expected, correction_id, identity):
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._check_write(db, novel_id, expected, identity)
            rules = json.loads(self._revision(db, expected)["corrections"])
            rule = next((r for r in rules if r["id"] == correction_id and r["status"] == "pending"), None)
            if not rule:
                raise GraphError("graph_correction_not_found", "待确认人工修正不存在。", 404)
            rule["status"] = "skipped"
            version = self._clone(db, row)
            return self._publish(db, row, version, "correction", rules, {"skipped_correction": correction_id})

    @retry_busy
    def apply_to_build(self, version, identity):
        """Apply rules to newly materialized model results; never alter checkpoints."""
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            base = db.execute("SELECT * FROM graph_build_bases WHERE version=?", (version,)).fetchone()
            if not base:
                return
            rules = json.loads(base["corrections"])
            if not rules:
                return
            snapshot = load_snapshot(db, version)
            for rule in rules:
                reason = None
                if rule["source_identity"] != identity:
                    reason = "正文或原文位置映射已变化"
                else:
                    basis = load_snapshot(db, rule["basis_version"])
                    ownership = {mention_key(m, e["type"]): e["id"] for e in snapshot["entities"].values() for m in e["mentions"]}
                    sides = []
                    participant_ids = rule.get("entity_ids") or [rule["source_entity_id"], rule["target_entity_id"]]
                    for eid in participant_ids:
                        entity = basis["entities"][eid]
                        keys = {mention_key(m, entity["type"]) for m in entity["mentions"]}
                        owners = {ownership.get(key) for key in keys}
                        sides.append(next(iter(owners)) if len(owners) == 1 and None not in owners else None)
                    if None in sides:
                        reason = "原文提及缺失或原身份已拆分，请重新选择实体"
                    elif any(ownership.get(mention_key(m, e["type"])) in sides for e in basis["entities"].values()
                             if e["id"] not in participant_ids for m in e["mentions"]):
                        reason = "新实体混入了原先其他身份，请核对原文"
                    elif len(set(sides)) > 1:
                        target_id = sides[participant_ids.index(rule["target_entity_id"])]
                        merge_entities_snapshot(snapshot, list(dict.fromkeys(sides)), target_id, version, rule["target_name"])
                        rule.update(status="applied", reason=None)
                    else:
                        entity = snapshot["entities"][sides[0]]
                        rename_entity(entity, rule["target_name"])
                        rule.update(status="satisfied", reason=None)
                if reason:
                    rule.update(status="pending", reason=reason)
            self._save(db, version, snapshot)
            db.execute("UPDATE graph_build_bases SET corrections=? WHERE version=?", (encode(rules), version))
