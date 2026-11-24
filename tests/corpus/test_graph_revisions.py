from __future__ import annotations

import copy
import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from novel_agent.corpus.knowledge_graph.models import GraphError, MergeEntitiesRequest
from novel_agent.corpus.knowledge_graph.revisions import GraphRevisions, load_snapshot, merge_snapshot
from novel_agent.corpus.knowledge_graph.storage import GraphStorage, REVISION_SCHEMA, SCHEMA, encode, now
from novel_agent.web.app import create_app
from test_knowledge_graph import ScriptModel, upload, wait_job

IDENTITY = {"source_hash": "book", "parser": "1", "mapping": "same"}


def span(name, start):
    return {"start_char": start, "end_char": start + len(name), "quote": name,
            "section_id": "s", "section_order": 0, "start_line": 1, "end_line": 1, "chunk_id": "c"}


def seed(storage, *, variant=None, identity=IDENTITY, apply=False, novel="n"):
    version, job = uuid.uuid4().hex, uuid.uuid4().hex
    revisions = GraphRevisions(storage)
    entities = {name: {"id": version + name, "name": name, "type": "item" if name == "剑" else "person",
                       "aliases": [name + "别名"], "description": name + "简介",
                       "name_forms": [{"name": name, "kind": "proper_name"}], "identity_evidence": [],
                       "mentions": [span(name, position)]}
                for name, position in (("甲", 0), ("乙", 10), ("丙", 20), ("丁", 30), ("剑", 40))}
    entities["甲"]["mentions"].append(span("甲", 2))
    if variant == "missing":
        entities["甲"]["mentions"].pop()
    if variant == "split":
        entities["另甲"] = {**copy.deepcopy(entities["甲"]), "id": version + "另甲", "mentions": [entities["甲"]["mentions"].pop()]}
    if variant == "outsider":
        entities["甲"]["mentions"].extend(entities["丙"]["mentions"])
        del entities["丙"]
    edges = []
    if variant != "outsider":
        for index, (left, right, kind, polarity) in enumerate([
            ("甲", "丙", "knows", "affirmed"), ("乙", "丙", "knows", "affirmed"),
            ("甲", "乙", "disciple_of", "affirmed"), ("甲", "丙", "knows", "denied"),
            ("丙", "甲", "disciple_of", "affirmed")]):
            edges.append({"id": version + str(index), "source": entities[left]["id"], "target": entities[right]["id"],
                          "type": kind, "polarity": polarity, "statement_mode": "narration", "groups": [[span("证据", 100 + index * 10)]]})
    inventory, bindings = {}, []
    for entity in entities.values():
        for mention in entity["mentions"]:
            mid = str(mention["start_char"])
            inventory[mid] = {"surface": mention["quote"], "type": entity["type"], "anchor": mention, "contexts": [mention]}
            bindings.append({"mention_id": mid, "entity_id": entity["id"], "status": "resolved",
                             "reason": "catalog_name", "candidate_ids": [entity["id"]], "supporting_mentions": []})
    inventory["pending"] = {"surface": "公子", "type": "person", "anchor": span("公子", 50), "contexts": [span("公子", 50)]}
    bindings.append({"mention_id": "pending", "entity_id": None, "status": "pending", "reason": "competing_identities",
                     "candidate_ids": [entities["甲"]["id"], entities["乙"]["id"]], "supporting_mentions": []})
    facts = [{"candidate_id": str(i), "source_id": e["source"], "target_id": e["target"], "status": "resolved",
              "evidence": e["groups"]} for i, e in enumerate(edges)]
    bundle = {"entities": {e["id"]: e for e in entities.values()}, "relations": edges,
              "bindings": [{**inventory[b["mention_id"]], **b} for b in bindings], "facts": facts}
    if variant == "satisfied":
        merge_snapshot(bundle, entities["甲"]["id"], entities["乙"]["id"], version)
    with storage.connect() as db:
        db.execute("INSERT INTO graph_versions VALUES(?,?,'staging','f',?,'{}',?,NULL,'{}')", (version, novel, encode(identity), now()))
        db.execute("INSERT INTO graph_jobs(job_id,novel_id,version,status,created_at,updated_at) VALUES(?,?,?,'building',?,?)", (job, novel, version, now(), now()))
        revisions.capture_build(db, novel, version)
    mentions = {b["mention_id"]: b for b in bundle["bindings"]}
    storage.save_identity(version, SimpleNamespace(inventory=SimpleNamespace(mentions=mentions),
                          bindings=bundle["bindings"], facts=bundle["facts"], decisions=[]))
    storage.materialize(version, list(bundle["entities"].values()), bundle["relations"])
    if apply:
        revisions.apply_to_build(version, identity)
    storage.publish(novel, version, job)
    return version, entities


@pytest.fixture
def graph(tmp_path):
    storage = GraphStorage(tmp_path)
    storage.open()
    try:
        version, entities = seed(storage)
        yield storage, GraphRevisions(storage), version, entities
    finally:
        storage.close()


def snapshot(storage, version):
    with storage.connect() as db:
        return load_snapshot(db, version)


def merge_body(version, entities, source="甲", target="乙", **extra):
    return MergeEntitiesRequest(version=version, source_id=entities[source]["id"], target_id=entities[target]["id"], **extra)


def test_merge_updates_all_references_evidence_and_keeps_original_snapshot(graph):
    storage, revisions, version, entities = graph
    original = snapshot(storage, version)
    body = merge_body(version, entities)
    preview = revisions.merge("n", body, IDENTITY, preview=True)
    assert preview["relation_count_before"] == 5 and preview["relation_count_after"] == 3
    assert preview["self_relations_removed"] == 1
    assert preview["target"]["mention_count_before"] == 1
    assert storage.active("n") == version and revisions.history("n", IDENTITY)["total"] == 1
    result = revisions.merge("n", body, IDENTITY)
    merged = snapshot(storage, result["graph_version"])
    target = merged["entities"][entities["乙"]["id"]]
    assert len(target["mentions"]) == 3 and "甲" in target["aliases"]
    assert target["description"] == "乙简介"
    pending = next(b for b in merged["bindings"] if b["mention_id"] == "pending")
    assert pending["entity_id"] is None and pending["status"] == "pending"
    assert pending["candidate_ids"] == [entities["乙"]["id"]]
    assert next(f for f in merged["facts"] if f["candidate_id"] == "2")["status"] == "filtered"
    assert sorted(len(e["groups"]) for e in merged["relations"]) == [1, 1, 2]
    assert {e["polarity"] for e in merged["relations"]} == {"affirmed", "denied"}
    assert snapshot(storage, version) == original
    with storage.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM graph_jobs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM graph_attempts").fetchone()[0] == 0


def test_multi_step_restore_keeps_branches_and_controls_future_rebuilds(graph):
    storage, revisions, original, entities = graph
    first = revisions.merge("n", merge_body(original, entities), IDENTITY)["graph_version"]
    second = revisions.merge("n", merge_body(first, entities, "丁"), IDENTITY)["graph_version"]
    rebuilt, _ = seed(storage, apply=True)
    assert len(snapshot(storage, rebuilt)["entities"]) == 3
    restored = revisions.restore("n", rebuilt, original, IDENTITY)["graph_version"]
    assert snapshot(storage, restored) == snapshot(storage, original)
    assert revisions.corrections("n", restored)["items"] == []
    rebuilt_again, _ = seed(storage, apply=True)
    assert len(snapshot(storage, rebuilt_again)["entities"]) == 5
    branch = revisions.restore("n", rebuilt_again, first, IDENTITY)["graph_version"]
    assert len(revisions.corrections("n", branch)["items"]) == 1
    branch_build, _ = seed(storage, apply=True)
    assert len(snapshot(storage, branch_build)["entities"]) == 4
    history = revisions.history("n", IDENTITY, limit=3)
    assert history["total"] == 8 and len(history["items"]) == 3
    assert revisions.history("n", IDENTITY, offset=3)["items"]
    assert revisions.restore("n", branch_build, second, IDENTITY)["graph_version"]


@pytest.mark.parametrize("variant,status", [(None, "applied"), ("satisfied", "satisfied"),
    ("missing", "pending"), ("split", "pending"), ("outsider", "pending")])
def test_rebuild_matches_source_mentions_and_exposes_conflicts(graph, variant, status):
    storage, revisions, version, entities = graph
    revisions.merge("n", merge_body(version, entities), IDENTITY)
    rebuilt, fresh = seed(storage, variant=variant, apply=True)
    rule = revisions.corrections("n", rebuilt)["items"][0]
    assert rule["status"] == status
    if status == "applied":
        assert fresh["甲"]["id"] not in snapshot(storage, rebuilt)["entities"]
        assert rule["reason"] is None
    elif status == "pending":
        assert rule["reason"]
        assert fresh["甲"]["id"] in snapshot(storage, rebuilt)["entities"]


def test_pending_correction_can_be_skipped_or_replaced_and_wrong_source_is_not_replayed(graph):
    storage, revisions, version, entities = graph
    revisions.merge("n", merge_body(version, entities), IDENTITY)
    rebuilt, fresh = seed(storage, variant="split", apply=True)
    rule = revisions.corrections("n", rebuilt)["items"][0]
    skipped = revisions.skip("n", rebuilt, rule["id"], IDENTITY)["graph_version"]
    assert revisions.corrections("n", skipped)["items"][0]["status"] == "skipped"
    replaced = revisions.merge("n", merge_body(skipped, fresh, replaces_correction_id=rule["id"]), IDENTITY)["graph_version"]
    rules = revisions.corrections("n", replaced)["items"]
    assert len(rules) == 1 and rules[0]["id"] != rule["id"] and rules[0]["status"] == "applied"
    changed_identity = {**IDENTITY, "mapping": "different"}
    incompatible, _ = seed(storage, identity=changed_identity, apply=True)
    assert revisions.corrections("n", incompatible)["items"][0]["status"] == "pending"
    with pytest.raises(GraphError, match="正文"):
        revisions.restore("n", incompatible, version, changed_identity)


def test_merge_guards_and_transaction_rollback(graph, monkeypatch):
    storage, revisions, version, entities = graph
    for body, code in [(merge_body(version, entities, "甲", "甲"), "graph_invalid_merge"),
                       (merge_body(version, entities, "甲", "剑"), "graph_invalid_merge"),
                       (MergeEntitiesRequest(version=version, source_id="missing", target_id=entities["乙"]["id"]), "graph_object_not_found")]:
        with pytest.raises(GraphError) as error:
            revisions.merge("n", body, IDENTITY)
        assert error.value.code == code
    with pytest.raises(GraphError) as error:
        revisions.merge("other", merge_body(version, entities), IDENTITY)
    assert error.value.code == "graph_version_changed"
    other, _ = seed(storage)
    with pytest.raises(GraphError) as error:
        revisions.merge("n", merge_body(version, entities), IDENTITY)
    assert error.value.code == "graph_version_changed"
    restored = revisions.restore("n", other, version, IDENTITY)["graph_version"]
    original_materialize = storage.materialize_in
    def broken(*args):
        original_materialize(*args)
        raise RuntimeError("transaction fails after materialization")
    monkeypatch.setattr(storage, "materialize_in", broken)
    count = revisions.history("n", IDENTITY)["total"]
    with pytest.raises(RuntimeError):
        revisions.merge("n", merge_body(restored, entities), IDENTITY)
    assert storage.active("n") == restored
    with storage.connect() as db:
        assert db.execute("SELECT count(*) FROM graph_versions").fetchone()[0] == count
        db.execute("UPDATE graph_jobs SET status='queued' WHERE version=?", (version,))
    with pytest.raises(GraphError) as error:
        revisions.merge("n", merge_body(restored, entities), IDENTITY, preview=True)
    assert error.value.code == "graph_job_conflict"


def test_schema_three_upgrade_backfills_history_without_touching_model_data(tmp_path):
    folder = tmp_path / "knowledge_graph"
    folder.mkdir()
    with sqlite3.connect(folder / "graph.sqlite3") as db:
        db.executescript(SCHEMA.replace(REVISION_SCHEMA, "").replace("user_version=4", "user_version=3"))
        db.execute("INSERT INTO graph_versions VALUES('old','n','published','f',?,'{}',?,?,'{}')", (encode(IDENTITY), now(), now()))
        db.execute("INSERT INTO graph_novels VALUES('n','old')")
    storage = GraphStorage(tmp_path)
    storage.open()
    try:
        assert storage.active("n") == "old"
        assert GraphRevisions(storage).history("n", IDENTITY)["items"][0]["operation"] == "build"
        with storage.connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        storage.close()


def test_http_merge_rebuild_restore_restart_and_old_agent_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = ScriptModel()
    root = tmp_path / "runtime"
    with TestClient(create_app(data_root=root, graph_model_factory=lambda: model)) as client:
        novel = upload(client)
        base = f"/api/novels/{novel}/graph"
        client.post(base + "/builds", json={"mode": "start"})
        initial_status = wait_job(client, novel)
        original = initial_status["active_version"]
        reader = client.app.state.knowledge_graph.capture(novel)
        left = reader.search("张三")["items"][0]["id"]
        right = reader.search("李四")["items"][0]["id"]
        body = {"version": original, "source_id": left, "target_id": right}
        assert client.post(base + "/merges/preview", json=body).status_code == 200
        calls = len(model.calls)
        merged = client.post(base + "/merges", json=body)
        assert merged.status_code == 200
        merged_version = merged.json()["graph_version"]
        assert len(model.calls) == calls
        status = client.get(base + "/status").json()
        assert status["counts"]["entity_count"] == 2
        assert status["job"]["usage"] == initial_status["job"]["usage"]
        assert status["revision"]["operation"] == "merge"
        assert reader.search("张三")["items"][0]["id"] == left
        assert client.get(base + "/entities", params={"version": original, "q": "张三"}).status_code == 409
        assert client.post(base + "/merges", json=body).status_code == 409
        client.post(base + "/builds", json={"mode": "rebuild"})
        rebuilt = wait_job(client, novel)["active_version"]
        assert rebuilt != merged_version
        assert client.get(base + "/status").json()["counts"]["entity_count"] == 2
        assert client.get(base + "/corrections", params={"version": rebuilt}).json()["items"][0]["status"] == "applied"
        restore = client.post(base + f"/history/{original}/restore", json={"version": rebuilt})
        assert restore.status_code == 200
        assert restore.json()["graph_version"] != original
        assert client.get(base + "/status").json()["counts"]["entity_count"] == 3
        client.post(base + "/builds", json={"mode": "rebuild"})
        final = wait_job(client, novel)["active_version"]
        assert client.get(base + "/status").json()["counts"]["entity_count"] == 3
        assert client.get(base + "/history").json()["total"] == 5
    with TestClient(create_app(data_root=root, graph_model_factory=lambda: model)) as client:
        assert client.get(base + "/status").json()["active_version"] == final
        assert client.get(base + "/history").json()["total"] == 5
        assert client.post(base + f"/history/{merged_version}/restore", json={"version": final}).status_code == 200
        assert client.get(base + "/status").json()["counts"]["entity_count"] == 2
