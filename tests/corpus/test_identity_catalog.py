from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novel_agent.corpus.knowledge_graph.catalog import build_catalog, candidate_groups
from novel_agent.corpus.knowledge_graph.concurrency import BuildStopped
from novel_agent.corpus.knowledge_graph.inventory import collect_inventory
from novel_agent.corpus.knowledge_graph.models import CatalogDecision, GraphOutputLimitError, MentionBindingBatch, PersonName
from novel_agent.corpus.knowledge_graph.storage import GraphStorage, IDENTITY_SCHEMA, SCHEMA, now
from novel_agent.web.app import create_app

from test_graph_quality import Checkpoints, batch, entity
from test_knowledge_graph import ScriptModel, upload, wait_job


def response(payload, identities):
    cards = {c["name"]: c for c in payload["cards"]}
    profiles = []
    for canonical, forms in identities:
        selected = [name for name in forms if name in cards]
        if not selected or canonical not in cards:
            continue
        profiles.append({"canonical_name": canonical,
            "anchor_mention_id": cards[canonical]["excerpts"][0]["mention_id"],
            "name_forms": [{"name": name, "kind": "full_name" if name == canonical else "given_name"} for name in selected],
            "supporting_mentions": [c["mention_id"] for name in selected for c in cards[name]["excerpts"]]})
    return CatalogDecision.model_validate({"profiles": profiles})


def novel_fixture():
    forms = ["黛玉", "黛玉", "林黛玉", "林妹妹", "宝玉", "宝玉哥哥", "贾宝玉", "甄宝玉", "宝玉"]
    return [batch(f"u{i}", [entity(name, i * 100, context=(
        "甄宝玉听见有人唤宝玉，便走了过去。" if i == 8 else f"{name}在贾府说话。"))]) for i, name in enumerate(forms)]


def test_full_name_given_name_and_address_share_identity_not_mention_nodes():
    values = novel_fixture()
    calls, records = [], Checkpoints()
    identities = [("林黛玉", ["林黛玉", "黛玉", "林妹妹"]),
                  ("贾宝玉", ["贾宝玉", "宝玉", "宝玉哥哥"]), ("甄宝玉", ["甄宝玉", "宝玉"])]
    def invoke(uid, schema, prompt, payload):
        calls.append(uid)
        if schema is CatalogDecision:
            return response(payload, identities)
        choices = []
        for mention in payload["mentions"]:
            canonical = "甄宝玉" if "甄宝玉" in "".join(mention["context"]) else "贾宝玉"
            eid = next(c["entity_id"] for c in mention["candidates"] if c["canonical_name"] == canonical)
            choices.append({"mention_id": mention["mention_id"], "entity_id": eid,
                            "supporting_mentions": [mention["mention_id"]]})
        return MentionBindingBatch.model_validate({"bindings": choices})
    result = build_catalog(values, "v", invoke, records, lambda _: None)
    assert {e["name"] for e in result.entities} == {"林黛玉", "贾宝玉", "甄宝玉"}
    assert len(next(e for e in result.entities if e["name"] == "林黛玉")["mentions"]) == 4
    assert result.stats["pending_mentions"] == 0
    ids = {b["mention_id"]: b["entity_id"] for b in result.bindings}
    assert ids["m400_402_person"] != ids["m800_802_person"]
    replay = build_catalog(values, "v", lambda *_: pytest.fail("replay called model"), records, lambda _: None, concurrency=1)
    assert replay == result
    assert len(calls) <= 3


def test_uncertainty_preserves_pending_facts_and_does_not_create_nodes():
    values = [batch("a", [entity("张三", 0), entity("阿三", 10)], relations=[{
        "source": "张三", "target": "阿三", "type": "knows", "polarity": "affirmed", "statement_mode": "narration",
        "evidence": [{"start_char": 0, "end_char": 13, "quote": "张三认识阿三。", "section_id": "s", "section_order": 0}]}])]
    values[0]["identity_links"] = [{"source": "张三", "target": "阿三", "evidence": values[0]["relations"][0]["evidence"]}]
    def invoke(uid, schema, prompt, payload):
        return CatalogDecision(profiles=[])
    result = build_catalog(values, "v", invoke, Checkpoints(), lambda _: None)
    assert result.entities == result.relations == []
    assert len(result.bindings) == 2 and all(b["status"] == "pending" for b in result.bindings)
    assert result.stats["pending_facts"] == 1
    assert result.facts[0]["evidence"][0]["quote"] == "张三认识阿三。"


def test_unsupported_catalog_names_and_conflicting_aliases_are_rejected():
    values = [batch("u", [entity("宝玉", 0, context="宝玉和黛玉二人同行。"), entity("黛玉", 10)], links=[{
        "source": "宝玉", "target": "黛玉", "evidence": [{"quote": "宝玉和黛玉二人同行。", "start_char": 0, "end_char": 10}]}])]
    result = build_catalog(values, "v", lambda uid, schema, prompt, payload:
        response(payload, [("宝玉", ["宝玉", "黛玉"])]), Checkpoints(), lambda _: None)
    assert not result.entities
    assert all(b["status"] == "pending" for b in result.bindings)
    assert any(d["reason"] == "distinct_identity_conflict" for d in result.decisions)


def test_catalog_review_replaces_a_split_and_rebinds_earlier_mentions():
    values = [batch("u0", [entity("林黛玉", 0)]), batch("u1", [entity("黛玉", 100)])]
    def invoke(uid, schema, prompt, payload):
        if uid.startswith("catalog:"):
            return response(payload, [("林黛玉", ["林黛玉"])])
        return response(payload, [("林黛玉", ["林黛玉", "黛玉"])])
    result = build_catalog(values, "v", invoke, Checkpoints(), lambda _: None)
    assert len(result.entities) == 1 and result.entities[0]["name"] == "林黛玉"
    assert len(result.entities[0]["mentions"]) == 2


def test_binding_invalid_references_and_duplicate_responses_remain_pending():
    values = [batch(f"u{i}", [entity("张三", i * 100)]) for i in range(3)]
    def invoke(uid, schema, prompt, payload):
        if schema is CatalogDecision:
            excerpts = payload["cards"][0]["excerpts"]
            return CatalogDecision.model_validate({"profiles": [{"anchor_mention_id": e["mention_id"],
                "canonical_name": "张三", "name_forms": [{"name": "张三"}], "supporting_mentions": [e["mention_id"]]}
                for e in excerpts[:2]]})
        current = payload["mentions"][0]
        bad = {"mention_id": current["mention_id"], "entity_id": current["candidates"][0]["entity_id"], "supporting_mentions": ["invented"]}
        return MentionBindingBatch.model_validate({"bindings": [bad, bad]})
    result = build_catalog(values, "v", invoke, Checkpoints(), lambda _: None)
    assert result.entities == []
    assert result.stats["pending_mentions"] == 3


def test_pending_storage_api_scope_and_old_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    class PendingModel(ScriptModel):
        def with_structured_output(self, schema, **kwargs):
            if schema is CatalogDecision:
                from types import SimpleNamespace
                return SimpleNamespace(invoke=lambda _: {"profiles": []})
            return super().with_structured_output(schema, **kwargs)
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=PendingModel)) as client:
        novel = upload(client)
        base = f"/api/novels/{novel}/graph"
        client.post(base + "/builds", json={"mode": "start"})
        status = wait_job(client, novel)
        version = status["active_version"]
        page = client.get(base + "/unresolved", params={"version": version, "limit": 1}).json()
        assert page["total"] > 1 and len(page["items"]) == 1
        assert page["items"][0]["reason"] == "insufficient_identity_evidence"
        assert page["items"][0]["contexts"]
        assert client.get(base + "/unresolved", params={"version": version, "startChapterId": "another:section_0001"}).status_code == 400
        client.post(base + "/builds", json={"mode": "rebuild"})
        wait_job(client, novel)
        # Wait for a different published version; wait_job can initially see the queued job.
        assert client.get(base + "/unresolved", params={"version": version}).status_code == 409


def test_schema_two_migration_preserves_graph_and_adds_pending_tables(tmp_path):
    folder = tmp_path / "knowledge_graph"
    folder.mkdir()
    with sqlite3.connect(folder / "graph.sqlite3") as db:
        db.executescript(SCHEMA.replace(IDENTITY_SCHEMA, "").replace("user_version=3", "user_version=2"))
        db.execute("INSERT INTO graph_versions VALUES('v','n','published','f','{}','{}',?,NULL,'{}')", (now(),))
        db.execute("INSERT INTO graph_novels VALUES('n','v')")
    storage = GraphStorage(tmp_path)
    storage.open()
    try:
        assert storage.active("n") == "v"
        with storage.connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 3
            assert db.execute("SELECT COUNT(*) FROM graph_source_mentions").fetchone()[0] == 0
    finally:
        storage.close()


def test_unknown_extra_alias_does_not_erase_a_grounded_identity():
    values = [batch("a", [entity("林黛玉", 0)]), batch("b", [entity("黛玉", 100)])]
    def invoke(uid, schema, prompt, payload):
        value = response(payload, [("林黛玉", ["林黛玉", "黛玉"])])
        value.profiles[0].name_forms.append(type(value.profiles[0].name_forms[0])(name="并未出现的别名"))
        return value
    result = build_catalog(values, "v", invoke, Checkpoints(), lambda _: None)
    assert len(result.entities) == 1
    assert result.entities[0]["aliases"] == ["黛玉"]


def test_surname_given_name_assertion_uses_observed_full_name_anchor():
    values = [batch(f"u{i}", [entity(name, i * 100)]) for i, name in enumerate(["宝玉", "贾宝玉", "宝玉哥哥"])]
    def invoke(uid, schema, prompt, payload):
        cards = {c["name"]: c for c in payload["cards"]}
        return CatalogDecision.model_validate({"profiles": [{
            "anchor_mention_id": cards["宝玉"]["excerpts"][0]["mention_id"], "canonical_name": "宝玉",
            "name_forms": [{"name": "宝玉", "kind": "given_name"}, {"name": "宝玉哥哥", "kind": "address"}],
            "supporting_mentions": [cards[name]["excerpts"][0]["mention_id"] for name in ("宝玉", "宝玉哥哥")],
            "person_name": {"family_name": "贾", "given_name": "宝玉"}}]})
    result = build_catalog(values, "v", invoke, Checkpoints(), lambda _: None)
    assert len(result.entities) == 1 and result.entities[0]["name"] == "贾宝玉"
    assert result.entities[0]["person_name"] == {"family_name": "贾", "given_name": "宝玉"}
    assert len(result.entities[0]["mentions"]) == 3


def test_catalog_capacity_retry_is_smaller_and_checkpointed():
    values = [batch(f"u{i}", [entity("张三", i * 100)]) for i in range(20)]
    records, sizes = Checkpoints(), []
    def invoke(uid, schema, prompt, payload):
        size = len(payload["cards"][0]["excerpts"])
        sizes.append(size)
        if size > 3:
            raise GraphOutputLimitError()
        return response(payload, [("张三", ["张三"])])
    result = build_catalog(values, "v", invoke, records, lambda _: None)
    assert sizes == [12, 3]
    assert len(result.entities[0]["mentions"]) == 20
    assert build_catalog(values, "v", lambda *_: pytest.fail("replay called model"), records, lambda _: None) == result


def test_stop_after_paid_catalog_result_reuses_it_on_resume():
    values = [batch("u0", [entity("林黛玉", 0)]), batch("u1", [entity("黛玉", 100)])]
    records = Checkpoints()
    def stop():
        if any(uid.startswith("catalog:") for uid in records.records):
            raise BuildStopped()
    with pytest.raises(BuildStopped):
        build_catalog(values, "v", lambda uid, schema, prompt, payload:
            response(payload, [("林黛玉", ["林黛玉", "黛玉"])]), records, lambda _: None, check=stop)
    result = build_catalog(values, "v", lambda *_: pytest.fail("resume called model"), records, lambda _: None)
    assert len(result.entities) == 1


def test_real_novel_dataset_has_identity_labels_and_fact_endpoints():
    dataset = json.loads((Path(__file__).parents[1] / "fixtures/red_chamber_identity.json").read_text(encoding="utf-8"))
    assert len(dataset["mentions"]) >= 500 and len(dataset["identities"]) >= 20 and len(dataset["facts"]) >= 100
    assert len({m["anchor"]["section_id"] for m in dataset["mentions"]}) >= 20
    labels = {m["mention_id"]: m["gold_identity"] for m in dataset["mentions"]}
    for mention in dataset["mentions"]:
        assert mention["anchor"]["quote"] == mention["surface"]
        assert mention["anchor"]["end_char"] - mention["anchor"]["start_char"] == len(mention["surface"])
    assert labels["m815807_815809_person"] == "甄宝玉"
    assert labels["m22379_22381_person"] == "袭人"
    for fact in dataset["facts"]:
        assert labels[fact["source_mentions"][0]] == fact["source_gold"]
        assert labels[fact["target_mentions"][0]] == fact["target_gold"]


def test_explicit_source_introduction_survives_a_model_omission():
    values = [batch("u0", [entity("林如海", 0, context="这林如海姓林名海，表字如海。")]),
              batch("u1", [entity("林如海", 100, context="雨村央烦林如海。")])]
    calls = []
    def invoke(uid, schema, prompt, payload):
        calls.append(uid)
        return CatalogDecision()
    result = build_catalog(values, "v", invoke, Checkpoints(), lambda _: None)
    assert len(calls) == 1  # A grounded introduction does not trigger repeated audits.
    assert result.entities[0]["name"] == "林如海"
    assert result.entities[0]["person_name"] == {"family_name": "林", "given_name": "海"}
    assert result.stats["pending_mentions"] == 0


@pytest.mark.parametrize("review_resolves", [False, True])
def test_household_scope_conflict_gets_focused_review_not_a_surname_override(review_resolves):
    context = "钱家的太太带了他们家的明远来了。"
    target = entity("明远", 100, context=context)
    target["contexts"][0].update(start_char=100 - context.index("明远"), end_char=100 - context.index("明远") + len(context))
    values = [batch("a", [entity("赵明远", 0)]), batch("b", [entity("钱明远", 20)]), batch("c", [target])]
    reviews = []
    def invoke(uid, schema, prompt, payload):
        if schema is CatalogDecision:
            result = response(payload, [("赵明远", ["赵明远", "明远"]), ("钱明远", ["钱明远", "明远"])])
            for profile in result.profiles:
                profile.person_name = PersonName(family_name=profile.canonical_name[0], given_name="明远")
            return result
        item = payload["mentions"][0]
        if uid.startswith("audit:"):
            reviews.append(uid)
            if not review_resolves:
                return MentionBindingBatch()
        chosen = "钱明远" if uid.startswith("audit:") else "赵明远"
        eid = next(c["entity_id"] for c in item["candidates"] if c["canonical_name"] == chosen)
        return MentionBindingBatch.model_validate({"bindings": [{"mention_id": item["mention_id"], "entity_id": eid,
            "supporting_mentions": [item["mention_id"]]}]})
    result = build_catalog(values, "v", invoke, Checkpoints(), lambda _: None)
    assert len(reviews) == 1
    current = next(b for b in result.bindings if b["mention_id"] == "m100_102_person")
    if review_resolves:
        profile = next(e for e in result.entities if e["id"] == current["entity_id"])
        assert profile["name"] == "钱明远"
    else:
        assert current["status"] == "pending" and current["reason"] == "context_conflict"


def test_recorded_real_model_run_preserves_book_identity_quality_without_network():
    from scripts.evaluate_identity_catalog import sample_batches, score
    from novel_agent.corpus.knowledge_graph.ontology import digest
    folder = Path(__file__).parents[1] / "fixtures"
    dataset = json.loads((folder / "red_chamber_identity.json").read_text(encoding="utf-8"))
    recording = json.loads((folder / "red_chamber_catalog_recording.json").read_text(encoding="utf-8"))
    assert digest(dataset) == recording["dataset_hash"]
    def checkpoint(uid, fingerprint, result=None, sequence=0):
        assert result is None, "a recorded run must only read paid checkpoints"
        saved = recording["checkpoints"].get(uid)
        if saved is not None:
            assert saved["input_hash"] == fingerprint
            return copy.deepcopy(saved["result"])
    result = build_catalog(sample_batches(dataset), recording["version"],
        lambda *_: pytest.fail("recorded replay attempted a model request"), checkpoint, lambda _: None, concurrency=4)
    metrics = score(dataset, result)
    assert metrics["passed"]
    assert metrics["binding_accuracy"] == metrics["coverage"] == 1
    assert metrics["false_merge_clusters"] == metrics["split_identities"] == 0
    assert metrics["fact_endpoint_accuracy"] == 1
