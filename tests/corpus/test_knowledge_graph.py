from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from novel_agent.agent.tools import build_tools
from novel_agent.corpus.knowledge_graph.extraction import locate, validate_batch, windows
from novel_agent.corpus.knowledge_graph.models import Anchor, ExtractionBatch, GraphError
from novel_agent.corpus.knowledge_graph.query import GraphReadSession
from novel_agent.corpus.knowledge_graph.storage import GraphStorage, encode, now
from novel_agent.corpus.knowledge_graph.resolution import resolve
from novel_agent.corpus.knowledge_graph.models import BatchResolutionDecision, CatalogDecision, ResolutionDecision
from novel_agent.web.app import create_app


TEXT = "第一章 拜师\r\n\r\n张三拜李四为师。张三又名阿三。李四持有青云剑。青云剑是开启秘境的唯一信物。\r\n" + "山路悠长，风吹过树梢。" * 35 + "\r\n第二章 重逢\r\n\r\n张三与李四再度相遇。阿三并非李四的弟子。\r\n" + "山路悠长，风吹过树梢。" * 35


def scripted_merge(payload):
    """Fixed identity-positive model for the authored single-person fixtures."""
    if "entities" in payload:
        return {"decisions": [{"entity_ref": entry["entity_ref"], **scripted_merge(entry)} for entry in payload["entities"]]}
    candidate = payload["candidates"][0]
    refs = (payload.get("identity_excerpts", []) or payload["entity"]["excerpts"])
    return {"decision": "merge", "candidate_id": candidate["candidate_id"],
            "supporting_mentions": list(dict.fromkeys([refs[0]["mention_id"], candidate["excerpts"][0]["mention_id"]]))}


def scripted_catalog(payload):
    cards = {c["name"]: c for c in payload["cards"]}
    groups = []
    for canonical, aliases in [("张三", ["阿三"]), ("孙悟空", ["悟空", "孙大圣", "齐天大圣"])]:
        selected = [name for name in [canonical, *aliases] if name in cards]
        if selected:
            groups.append((canonical if canonical in selected else selected[0], selected))
    covered = {name for _, names in groups for name in names}
    groups.extend((name, [name]) for name in cards if name not in covered)
    return {"profiles": [{"anchor_mention_id": cards[canonical]["excerpts"][0]["mention_id"],
        "canonical_name": canonical, "name_forms": [{"name": name, "kind": "proper_name"} for name in names],
        "supporting_mentions": [cards[name]["excerpts"][0]["mention_id"] for name in names]}
        for canonical, names in groups]}


class ScriptModel:
    def __init__(self):
        self.calls = []
        self.fail_at = None
        self.entered = threading.Event()
        self.release = None

    def with_structured_output(self, schema, **kwargs):
        assert kwargs == {"method": "function_calling", "include_raw": True}
        def invoke(messages):
            payload = json.loads(messages[1].content)
            self.calls.append((schema.__name__, payload))
            if self.release:
                self.entered.set()
                assert self.release.wait(5)
            if self.fail_at == len(self.calls):
                raise TimeoutError("provider-secret-must-not-leak")
            if schema in (ResolutionDecision, BatchResolutionDecision):
                return scripted_merge(payload)
            if schema is CatalogDecision:
                return scripted_catalog(payload)
            core = next(s["text"] for s in payload["spans"] if s["span_id"] == "core")
            def anchor(text):
                return {"span_id": "core", "quote": text, "occurrence": 1}
            entities = [{"local_id": name, "name": name, "type": "item" if name == "青云剑" else "person", "mentions": [anchor(name)]}
                        for name in ("张三", "李四", "阿三", "青云剑") if name in core]
            for entity in entities:
                if entity["name"] == "青云剑":
                    entity.update(plot_relevant=True, importance_evidence=[anchor("青云剑是开启秘境的唯一信物。")])
            relations, links = [], []
            if "张三拜李四为师。" in core:
                relations.append({"source": "张三", "target": "李四", "type": "disciple_of", "evidence": [anchor("张三拜李四为师。")]} )
            if "李四持有青云剑。" in core:
                relations.append({"source": "李四", "target": "青云剑", "type": "possesses", "evidence": [anchor("李四持有青云剑。")]} )
            if "阿三并非李四的弟子。" in core:
                relations.append({"source": "阿三", "target": "李四", "type": "disciple_of", "polarity": "denied", "evidence": [anchor("阿三并非李四的弟子。")]} )
            if "张三又名阿三。" in core:
                links.append({"source": "张三", "target": "阿三", "evidence": [anchor("张三又名阿三。")]} )
            return {"entities": entities, "relations": relations, "identity_links": links}
        return SimpleNamespace(invoke=invoke)


def upload(client, text=TEXT):
    response = client.post("/api/novels", files={"file": ("sample.txt", text.encode(), "text/plain")})
    assert response.status_code == 201
    return response.json()["novel_id"]


def wait_job(client, novel_id, expected="completed"):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        status = client.get(f"/api/novels/{novel_id}/graph/status").json()
        if status["job"] and status["job"]["status"] == expected:
            return status
        if status["job"] and status["job"]["status"] == "failed" and expected != "failed":
            pytest.fail(str(status))
        time.sleep(0.01)
    pytest.fail(f"Job did not reach {expected}: {status}")


def test_build_query_alias_evidence_and_agent_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = ScriptModel()
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=lambda: model)) as client:
        novel = upload(client)
        base = f"/api/novels/{novel}/graph"
        assert client.get(base + "/status").json()["available"] is False
        assert client.get(base + "/entities", params={"version": "none", "q": "张三"}).status_code == 409
        job = client.post(base + "/builds", json={"mode": "start"})
        assert job.status_code == 202
        status = wait_job(client, novel)
        version = status["active_version"]
        quality = status["job"]["quality_stats"]
        assert quality["candidates_before"]["item"] == quality["candidates_after"]["item"] == 1
        assert quality["entities_after"]["item"] == 1
        assert quality["batch_request_count"] > 0
        assert quality["minimum_mentions"] == 3
        search = client.get(base + "/entities", params={"version": version, "q": "阿三"}).json()
        assert len(search["items"]) == 1
        assert search["items"][0]["name"] == "张三"
        root = search["items"][0]["id"]
        graph = client.get(base + "/neighbors", params={"version": version, "entity_id": root, "depth": 2}).json()
        assert {e["type"] for e in graph["edges"]} == {"disciple_of", "possesses"}
        assert {e["polarity"] for e in graph["edges"]} == {"affirmed", "denied"}
        sword = next(n["id"] for n in graph["nodes"] if n["name"] == "青云剑")
        paths = client.get(base + "/paths", params={"version": version, "from_id": root, "to_id": sword}).json()
        assert len(paths["paths"]) == 1
        assert len(paths["paths"][0]["edge_ids"]) == 2
        assert all(e["polarity"] == "affirmed" for e in paths["edges"])
        edge = paths["edges"][0]
        evidence = client.get(base + f"/relations/{edge['id']}/evidence", params={"version": version}).json()
        assert evidence["total"] >= 1
        corpus = client.app.state.novels.get(novel).corpus
        for e in graph["edges"]:
            for group in e["evidence_groups"]:
                for span in group["spans"]:
                    assert corpus.document.source.text[span["start_char"]:span["end_char"]] == span["quote"]
                    assert corpus.validate_quote(span["chunk_id"], span["quote"])
        reader = client.app.state.knowledge_graph.capture(novel)
        tools = {t.name: t for t in build_tools(corpus, reader)}
        assert {"search_graph_entities", "get_graph_entity", "get_graph_relations", "find_graph_paths"} <= tools.keys()
        detail = json.loads(tools["get_graph_entity"].invoke({"entity_id": root}))
        assert detail["entity"]["aliases"] == ["阿三"]
        assert "error" in json.loads(tools["get_graph_entity"].invoke({"entity_id": "missing"}))
        assert json.loads(tools["get_graph_relations"].invoke({"entity_id": root}))["graph_version"] == version
        assert "error" in json.loads(tools["get_graph_relations"].invoke({"entity_id": "missing"}))
        assert client.post(base + "/builds", json={"mode": "start"}).json()["cache_hit"]
        assert status["job"]["usage"]["unknown_request_count"] == status["job"]["usage"]["request_count"]
        assert "test-key" not in json.dumps(status)


def test_failed_build_resume_checkpoint_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_EXTRACTION_CONCURRENCY", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_REQUEST_RETRIES", "0")
    model = ScriptModel()
    model.fail_at = 2
    root = tmp_path / "data"
    with TestClient(create_app(data_root=root, graph_model_factory=lambda: model)) as client:
        novel = upload(client)
        base = f"/api/novels/{novel}/graph"
        client.post(base + "/builds", json={"mode": "start"})
        status = wait_job(client, novel, "failed")
        assert status["job"]["extraction"]["completed"] == 1
        assert "provider-secret" not in json.dumps(status)
        job = status["job"]["job_id"]
        assert client.post(base + "/builds", json={"mode": "start"}).json()["code"] == "graph_resume_required"
    model.fail_at = None
    with TestClient(create_app(data_root=root, graph_model_factory=lambda: model)) as client:
        assert client.post(base + "/builds", json={"mode": "resume", "job_id": job}).status_code == 202
        status = wait_job(client, novel)
        extraction_calls = [p for schema, p in model.calls if schema == "ExtractionBatch"]
        first_text = extraction_calls[0]["spans"][0]["text"]
        assert sum(p["spans"][0]["text"] == first_text for p in extraction_calls) == 1
        assert status["job"]["usage"]["failed_request_count"] == 1
    calls = len(model.calls)
    with TestClient(create_app(data_root=root, graph_model_factory=lambda: model)) as client:
        assert client.get(base + "/status").json()["available"]
        assert len(model.calls) == calls


def test_stop_saves_current_batch_and_duplicate_does_not_enqueue(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_EXTRACTION_CONCURRENCY", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = ScriptModel()
    model.release = threading.Event()
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=lambda: model)) as client:
        novel = upload(client)
        base = f"/api/novels/{novel}/graph"
        first = client.post(base + "/builds", json={"mode": "start"}).json()
        assert model.entered.wait(3)
        duplicate = client.post(base + "/builds", json={"mode": "rebuild"}).json()
        assert duplicate["job_id"] == first["job_id"]
        stopped = client.post(base + f"/builds/{first['job_id']}/stop").json()
        assert stopped["status"] == "stopping"
        model.release.set()
        status = wait_job(client, novel, "stopped")
        assert status["job"]["extraction"]["completed"] == 1
        assert len(model.calls) == 1
        assert not status["available"]


def test_rebuild_keeps_old_agent_version_and_web_rejects_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = ScriptModel()
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=lambda: model)) as client:
        novel = upload(client)
        base = f"/api/novels/{novel}/graph"
        client.post(base + "/builds", json={"mode": "start"})
        old = wait_job(client, novel)["active_version"]
        reader = client.app.state.knowledge_graph.capture(novel)
        client.post(base + "/builds", json={"mode": "rebuild"})
        status = wait_job(client, novel)
        assert status["active_version"] != old
        stale = client.get(base + "/entities", params={"version": old, "q": "张三"})
        assert stale.status_code == 409 and stale.json()["code"] == "graph_version_changed"
        assert reader.search("张三")["items"]
        other = upload(client, TEXT + "另一部书的结尾。")
        root = reader.search("张三")["items"][0]["id"]
        assert client.get(f"/api/novels/{other}/graph/entities/{root}", params={"version": old}).status_code == 409
        assert client.get(base + "/entities", params={"version": status["active_version"], "q": "张三", "startChapterId": other + ":section_0001"}).status_code == 400


def test_extraction_rejects_fabricated_quote_and_preserves_occurrence(corpus):
    window = windows(corpus)[0]
    text = corpus.document.source.text
    first = locate(corpus, window, Anchor(span_id="core", quote="吕树", occurrence=1))
    second = locate(corpus, window, Anchor(span_id="core", quote="吕树", occurrence=2))
    assert second["start_char"] > first["start_char"]
    assert text[second["start_char"]:second["end_char"]] == "吕树"
    batch = ExtractionBatch.model_validate({"entities": [
        {"local_id": "a", "name": "吕树", "type": "person", "mentions": [{"span_id": "core", "quote": "吕树"}]},
        {"local_id": "b", "name": "吕小鱼", "type": "person", "mentions": [{"span_id": "core", "quote": "吕小鱼"}]}],
        "relations": [{"source": "a", "target": "b", "type": "friend_of", "evidence": [{"span_id": "core", "quote": "不存在的原文"}]}]})
    result = validate_batch(corpus, window, batch)
    assert len(result["entities"]) == 2 and not result["relations"]
    assert result["rejected"]["relations"] == 1


@pytest.fixture
def indexed_graph(tmp_path, corpus):
    storage = GraphStorage(tmp_path)
    storage.open()
    version = "v1"
    with storage.connect() as db:
        db.execute("INSERT INTO graph_versions(version,novel_id,state,fingerprint,source_identity,model_identity,created_at) VALUES(?,?,'staging','f','{}','{}',?)", (version, "n", now()))
        db.execute("INSERT INTO graph_jobs(job_id,novel_id,version,status,created_at,updated_at) VALUES('j','n',?,'building',?,?)", (version, now(), now()))
    chunk = corpus.document.chunks[0]
    span = {"chunk_id": chunk.chunk_id, "section_id": chunk.section_id, "section_title": chunk.section_title,
            "section_order": 0, "start_char": chunk.start_char, "end_char": chunk.start_char + 2,
            "start_line": chunk.start_line, "end_line": chunk.start_line, "quote": chunk.text[:2]}
    entities = [{"id": name, "name": name, "type": "person", "aliases": ["别名" + name],
                 "mentions": [{**span, "start_char": span["start_char"] + i * 3, "end_char": span["end_char"] + i * 3}]}
                for i, name in enumerate(["a", "b", "c", "d", "x%_"])]
    edges = [{"id": source + target, "source": source, "target": target, "type": "knows", "polarity": polarity,
              "statement_mode": "narration", "groups": [[span]]}
             for source, target, polarity in [("a", "b", "affirmed"), ("a", "c", "affirmed"), ("b", "d", "affirmed"),
                                             ("c", "d", "affirmed"), ("d", "a", "denied")]]
    storage.materialize(version, entities, edges)
    storage.publish("n", version, "j")
    yield GraphReadSession(storage, corpus, "n", version)
    storage.close()


def test_bfs_keeps_all_shortest_paths_and_excludes_denial(indexed_graph):
    result = indexed_graph.paths("a", "d")
    assert {tuple(p["node_ids"]) for p in result["paths"]} == {("a", "b", "d"), ("a", "c", "d")}
    assert len(indexed_graph.paths("a", "a")["paths"][0]["edge_ids"]) == 0
    assert indexed_graph.paths("a", "x%_")["paths"] == []
    assert len(indexed_graph.paths("a", "d", limit=1)["paths"]) == 1
    assert indexed_graph.paths("a", "d", limit=1)["truncated"]


def test_limits_scope_and_literal_search(indexed_graph):
    result = indexed_graph.neighbors("a", depth=3, node_limit=2, edge_limit=2)
    ids = {n["id"] for n in result["nodes"]}
    assert len(ids) <= 2 and result["truncated"]
    assert all(e["source"] in ids and e["target"] in ids for e in result["edges"])
    assert indexed_graph.search("%_")["total"] == 1
    assert indexed_graph.search("别名a")["items"][0]["id"] == "a"
    sections = indexed_graph.corpus.document.sections
    if len(sections) > 1:
        result = indexed_graph.neighbors("a", start_section_id=sections[-1].section_id)
        assert not result["edges"]
    with pytest.raises(GraphError):
        indexed_graph.neighbors("a", entity_types=["unknown"])


def test_process_lock_is_exclusive(tmp_path):
    first, second = GraphStorage(tmp_path), GraphStorage(tmp_path)
    first.open()
    try:
        with pytest.raises(GraphError, match="另一服务进程"):
            second.open()
    finally:
        first.close(); second.close()


def test_joint_evidence_does_not_survive_partial_chapter_filter(indexed_graph):
    reader = indexed_graph
    sections = reader.corpus.document.sections
    assert len(sections) >= 2
    with reader.storage.connect() as db:
        group = db.execute("SELECT group_id FROM graph_evidence_groups WHERE relation_id='ab'").fetchone()[0]
        span = json.loads(db.execute("SELECT payload FROM graph_evidence_spans WHERE group_id=?", (group,)).fetchone()[0])
        span.update(section_id=sections[1].section_id, section_order=1, quote="后续章节依据")
        db.execute("INSERT INTO graph_evidence_spans VALUES(?,?,?,?,?)", (reader.graph_version, group, 1, 1, encode(span)))
        db.execute("UPDATE graph_evidence_groups SET last_section=1 WHERE group_id=?", (group,))
    partial = reader.evidence("ab", end_section_id=sections[0].section_id)
    assert partial["total"] == 0 and partial["items"] == []
    graph = reader.neighbors("a", end_section_id=sections[0].section_id)
    assert "ab" not in {edge["id"] for edge in graph["edges"]}
    assert reader.evidence("ab")["total"] == 1


def test_types_are_filtered_before_path_traversal(indexed_graph):
    result = indexed_graph.paths("a", "d", entity_types=["place"])
    assert result["paths"] == []
    assert {n["id"] for n in result["nodes"]} == {"a", "d"}


def test_resolution_keeps_homonyms_separate_with_distinguishing_evidence():
    def batch(unit, position):
        mention = {"start_char": position, "end_char": position + 2, "quote": "张三", "section_id": unit, "section_order": position}
        return {"unit_id": unit, "entities": [{"local_id": "a", "name": "张三", "type": "person", "description": "",
            "mentions": [mention], "contexts": [{**mention, "quote": "城东的木匠叫张三。" if position == 0 else "城西还有另一个张三，是医生，与木匠不是同一人。"}]}],
            "identity_links": [], "relations": []}
    saved = {}
    def checkpoint(uid, fingerprint, result=None, sequence=0):
        if result is not None:
            saved[uid] = result
        return saved.get(uid)
    def distinguish(uid, schema, prompt, payload):
        return ResolutionDecision(decision="new", supporting_mentions=[payload["entity"]["excerpts"][0]["mention_id"]])
    entities, _ = resolve([batch("s1", 0), batch("s2", 100)], "v",
        distinguish, checkpoint, lambda *_: None)
    assert len(entities) == 2
    # Replaying completed decisions must not call the model.
    replay, _ = resolve([batch("s1", 0), batch("s2", 100)], "v",
        lambda *_: pytest.fail("checkpoint replay called model"), checkpoint, lambda *_: None)
    assert [e["id"] for e in replay] == [e["id"] for e in entities]


@pytest.mark.parametrize("decision", ["ambiguous", "new", "invalid_merge", "invalid_new"])
def test_same_name_uncertainty_keeps_entities_and_their_relations_separate(decision):
    def batch(index, name="祥子"):
        position = index * 1000
        mention = {"start_char": position, "end_char": position + len(name), "quote": name, "section_id": str(index)}
        context = {**mention, "quote": ["《骆驼祥子》TXT全集", "祥子给夏先生拉车。", "夏太太叫祥子去买栗子。", "祥子又碰见了小福子。"][index]}
        entity = {"local_id": "x", "name": name, "type": "person", "description": "", "mentions": [mention], "contexts": [context]}
        other = {"local_id": "o", "name": "夏先生", "type": "person", "description": "",
                 "mentions": [{**mention, "start_char": position + 20, "end_char": position + 23, "quote": "夏先生"}]}
        return {"unit_id": str(index), "entities": [entity, other], "identity_links": [],
                "relations": [{"source": "x", "target": "o", "type": "knows", "polarity": "affirmed",
                               "statement_mode": "narration", "evidence": [context]}]}
    payloads = []
    def invoke(uid, schema, prompt, payload):
        payloads.append(payload)
        if decision == "invalid_merge":
            return ResolutionDecision(decision="merge", candidate_id="invented", supporting_mentions=["invented"])
        if decision == "invalid_new":
            return ResolutionDecision(decision="new", supporting_mentions=["invented"])
        return ResolutionDecision(decision=decision)
    saved, events = {}, []
    def checkpoint(uid, fingerprint, result=None, sequence=0):
        if result is not None:
            saved[uid] = (fingerprint, result)
            return
        if uid in saved:
            assert saved[uid][0] == fingerprint
            return saved[uid][1]
    entities, relations = resolve([batch(i) for i in range(4)], "v", invoke, checkpoint, events.append)
    assert len(entities) == 8
    assert len([e for e in entities if e["name"] == "祥子"]) == 4
    assert all(len(e["mentions"]) == 1 for e in entities)
    assert len(relations) == 4 and all(len(r["groups"]) == 1 for r in relations)
    # The latest narrative context must accompany the title, rather than only
    # repeating the first two excerpts throughout the entire novel.
    last = [p for p in payloads if p["entity"]["name"] == "祥子"][-1]
    assert any("夏太太叫祥子去买栗子。" in [m["quote"] for m in c["excerpts"]] for c in last["candidates"])
    assert "entity_same_name_reused" not in events
    replay = resolve([batch(i) for i in range(4)], "v", lambda *_: pytest.fail("checkpoint called model"), checkpoint, lambda *_: None)
    assert replay == (entities, relations)


@pytest.mark.parametrize("reverse_links", [False, True])
def test_late_alias_links_merge_existing_entities_and_replay_checkpoints(reverse_links):
    names = ["孙悟空", "悟空", "孙大圣", "齐天大圣"]
    def entity(name, index):
        mention = {"start_char": index * 20, "end_char": index * 20 + len(name),
                   "quote": name, "section_id": "s1", "section_order": 0}
        return {"local_id": name, "name": name, "type": "person", "description": "", "mentions": [mention]}
    first = {"unit_id": "early", "entities": [entity(name, i) for i, name in enumerate(names)],
             "identity_links": [], "relations": []}
    links = [{"source": "孙悟空", "target": name, "evidence": [{"start_char": 100 + i * 30,
              "end_char": 100 + i * 30 + len(f"孙悟空又称{name}。"), "quote": f"孙悟空又称{name}。", "section_id": "s2"}]}
             for i, name in enumerate(names[1:])]
    # The later extraction reuses the same original mentions (overlap/context).
    # Shared-mention fast paths must not swallow the newly discovered identity links.
    later = {"unit_id": "late", "entities": list(reversed(first["entities"])),
             "identity_links": list(reversed(links)) if reverse_links else links,
             "relations": [{"source": "齐天大圣", "target": "悟空", "type": "knows", "polarity": "affirmed",
                            "statement_mode": "narration", "evidence": [links[0]["evidence"][0]]}]}
    saved, calls = {}, []
    def checkpoint(uid, fingerprint, result=None, sequence=0):
        if result is not None:
            saved[uid] = (fingerprint, result)
            return
        if uid in saved:
            assert fingerprint == saved[uid][0]
            return saved[uid][1]
    def invoke(uid, schema, prompt, payload):
        calls.append(uid)
        assert payload["identity_excerpts"]
        return ResolutionDecision(decision="merge", candidate_id=payload["candidates"][0]["candidate_id"],
                                  supporting_mentions=[payload["identity_excerpts"][0]["mention_id"]])
    entities, relations = resolve([first, later], "v", invoke, checkpoint, lambda *_: None)
    assert len(entities) == 1
    assert entities[0]["name"] == "孙悟空"
    assert set(entities[0]["aliases"]) == set(names[1:])
    assert {m["quote"] for m in entities[0]["mentions"]} == set(names)
    assert len(entities[0]["mentions"]) == 4
    assert relations == []  # Former cross-alias edges cannot become self-relations.
    assert len(calls) == 3
    replay = resolve([first, later], "v", lambda *_: pytest.fail("replayed identity called model"), checkpoint, lambda *_: None)
    assert replay == (entities, relations)


@pytest.mark.parametrize("decision", ["ambiguous", "name_only", "invalid_reference"])
def test_alias_link_requires_identity_evidence_for_merge(decision):
    def entity(name, position):
        return {"local_id": name, "name": name, "type": "person", "description": "",
                "mentions": [{"start_char": position, "end_char": position + len(name), "quote": name, "section_id": "s"}]}
    batch = {"unit_id": "u", "entities": [entity("孙悟空", 0), entity("齐天大圣", 10)], "relations": [],
             "identity_links": [{"source": "孙悟空", "target": "齐天大圣", "evidence": [
                 {"start_char": 20, "end_char": 40, "quote": "此人假冒齐天大圣，并非孙悟空。", "section_id": "s"}]}]}
    def invoke(uid, schema, prompt, payload):
        if decision == "ambiguous":
            return ResolutionDecision(decision="ambiguous")
        ref = payload["entity"]["excerpts"][0]["mention_id"] if decision == "name_only" else "invented"
        return ResolutionDecision(decision="merge", candidate_id=payload["candidates"][0]["candidate_id"], supporting_mentions=[ref])
    entities, _ = resolve([batch], "v", invoke, lambda *_: None, lambda *_: None)
    assert len(entities) == 2
    assert all(not entity["aliases"] for entity in entities)


def test_multiple_aliases_build_search_details_and_relations(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    names = ["孙悟空", "悟空", "孙大圣", "齐天大圣"]
    text = ("第一章 相逢\n孙悟空持有金箍棒。金箍棒是降伏妖王的关键兵器。悟空拜唐僧为师。孙大圣认识唐僧。齐天大圣使用金箍棒。\n"
            + "山路悠长，风吹过树梢。" * 35
            + "\n第二章 身份\n孙悟空又称悟空。孙悟空又称孙大圣。孙悟空又称齐天大圣。\n"
            + "山路悠长，风吹过树梢。" * 35)
    class AliasModel(ScriptModel):
        def with_structured_output(self, schema, **kwargs):
            if schema in (ResolutionDecision, BatchResolutionDecision, CatalogDecision):
                return super().with_structured_output(schema, **kwargs)
            def invoke(messages):
                core = json.loads(messages[1].content)["spans"][0]["text"]
                def anchor(quote):
                    return {"span_id": "core", "quote": quote}
                entities = [{"local_id": name, "name": name, "type": "item" if name == "金箍棒" else "person",
                             "mentions": [anchor(name)]} for name in [*names, "唐僧", "金箍棒"] if name in core]
                for entity in entities:
                    if entity["name"] == "金箍棒":
                        entity.update(plot_relevant=True, importance_evidence=[anchor("金箍棒是降伏妖王的关键兵器。")])
                links = [{"source": "孙悟空", "target": name, "evidence": [anchor(f"孙悟空又称{name}。")]}
                         for name in names[1:] if f"孙悟空又称{name}。" in core]
                relations = [{"source": source, "target": target, "type": kind, "evidence": [anchor(quote)]}
                             for source, target, kind, quote in [
                                 ("孙悟空", "金箍棒", "possesses", "孙悟空持有金箍棒。"),
                                 ("悟空", "唐僧", "disciple_of", "悟空拜唐僧为师。"),
                                 ("孙大圣", "唐僧", "knows", "孙大圣认识唐僧。"),
                                 ("齐天大圣", "金箍棒", "uses", "齐天大圣使用金箍棒。")]
                             if quote in core]
                return {"entities": entities, "relations": relations, "identity_links": links}
            return SimpleNamespace(invoke=invoke)
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=AliasModel)) as client:
        novel = upload(client, text)
        base = f"/api/novels/{novel}/graph"
        assert client.post(base + "/builds", json={"mode": "start"}).status_code == 202
        status = wait_job(client, novel)
        version = status["active_version"]
        assert status["counts"] == {"entity_count": 3, "relation_count": 4}
        assert status["job"]["resolution"]["completed"] == status["job"]["resolution"]["total"]
        ids = set()
        for name in names:
            search = client.get(base + "/entities", params={"version": version, "q": name}).json()
            assert search["total"] == 1
            node = search["items"][0]
            ids.add(node["id"])
            assert node["name"] == "孙悟空"
            assert set(node["aliases"]) == set(names[1:])
        assert len(ids) == 1
        root = ids.pop()
        detail = client.get(base + f"/entities/{root}", params={"version": version}).json()
        assert set(detail["entity"]["aliases"]) == set(names[1:])
        assert {m["quote"] for m in detail["items"]} == set(names)
        neighbors = client.get(base + "/neighbors", params={"version": version, "entity_id": root}).json()
        assert len(neighbors["edges"]) == 4
        assert all(root in {edge["source"], edge["target"]} for edge in neighbors["edges"])
    # Existing published alias data remains readable after restart without a model.
    with TestClient(create_app(data_root=tmp_path)) as client:
        detail = client.get(base + f"/entities/{root}", params={"version": version}).json()
        assert set(detail["entity"]["aliases"]) == set(names[1:])


def test_oversized_extraction_splits_and_publishes_empty_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []
    names = ["神" + chr(0x4E00 + i) for i in range(81)]
    class DenseModel:
        def with_structured_output(self, schema, **kwargs):
            def invoke(messages):
                text = json.loads(messages[1].content)["spans"][0]["text"]
                calls.append(len(text))
                entities = [{"local_id": f"e{i}", "name": name, "type": "person", "mentions": [{"span_id": "core", "quote": name}]} for i, name in enumerate(names)]
                return {"entities": entities if len(text) > 1000 else []}
            return SimpleNamespace(invoke=invoke)
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=DenseModel)) as client:
        novel = upload(client, ("、".join(names) + "。") * 12)
        client.post(f"/api/novels/{novel}/graph/builds", json={"mode": "start"})
        status = wait_job(client, novel)
        assert max(calls) > 1000 and min(calls) <= 1000
        assert status["counts"] == {"entity_count": 0, "relation_count": 0}
        assert status["job"]["extraction"]["completed"] == status["job"]["extraction"]["total"]


@pytest.mark.parametrize("setting,value", [("MODEL_NAME", "different-model"), ("NOVEL_AGENT_GRAPH_MIN_ENTITY_MENTIONS", "4")])
def test_fingerprint_changes_block_resume_but_keep_published_graph(tmp_path, monkeypatch, setting, value):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NOVEL_AGENT_GRAPH_REQUEST_RETRIES", "0")
    model = ScriptModel()
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=lambda: model)) as client:
        novel = upload(client)
        base = f"/api/novels/{novel}/graph"
        client.post(base + "/builds", json={"mode": "start"})
        version = wait_job(client, novel)["active_version"]
        model.fail_at = len(model.calls) + 1
        client.post(base + "/builds", json={"mode": "rebuild"})
        job = wait_job(client, novel, "failed")["job"]
        monkeypatch.setenv(setting, value)
        response = client.post(base + "/builds", json={"mode": "resume", "job_id": job["job_id"]})
        assert response.json()["code"] == "graph_fingerprint_changed"
        monkeypatch.setenv("OPENAI_API_KEY", "")
        assert client.get(base + "/status").json()["configured"] is False
        assert client.get(base + "/entities", params={"version": version, "q": "张三"}).json()["items"]


def test_queue_is_global_and_queued_jobs_can_stop_without_model_call(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = ScriptModel()
    model.release = threading.Event()
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=lambda: model)) as client:
        first = upload(client)
        second = upload(client, TEXT + "结尾不同")
        client.post(f"/api/novels/{first}/graph/builds", json={"mode": "start"})
        assert model.entered.wait(3)
        queued = client.post(f"/api/novels/{second}/graph/builds", json={"mode": "start"}).json()
        assert queued["status"] == "queued"
        response = client.post(f"/api/novels/{second}/graph/builds/{queued['job_id']}/stop").json()
        assert response["status"] == "stopped"
        model.release.set()
        wait_job(client, first)
        assert all("结尾不同" not in json.dumps(payload, ensure_ascii=False) for _, payload in model.calls)


def test_graph_database_corruption_does_not_block_novel_search(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with TestClient(create_app(data_root=tmp_path)) as client:
        novel = upload(client)
    path = tmp_path / "knowledge_graph" / "graph.sqlite3"
    path.write_bytes(b"corrupt database")
    with TestClient(create_app(data_root=tmp_path)) as client:
        status = client.get(f"/api/novels/{novel}/graph/status").json()
        assert status["error_code"] == "graph_storage_unavailable"
        assert client.get(f"/api/novels/{novel}/search", params={"q": "张三"}).json()
        assert client.post(f"/api/novels/{novel}/graph/builds", json={"mode": "start"}).status_code == 503


def test_graph_evidence_passes_through_assessor_and_fabrication_is_rejected(tmp_path, monkeypatch):
    from novel_agent.application.service import execute_agent
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    script = ScriptModel()
    class ResearchModel(BaseChatModel):
        root_id: str
        writer_payload: ClassVar[dict] = {}
        @property
        def _llm_type(self):
            return "graph-research-test"
        def bind_tools(self, tools, **kwargs):
            assert "get_graph_relations" in {t.name for t in tools}
            return self
        def _generate(self, messages, **kwargs):
            if "小说解读写作者" in messages[0].content:
                type(self).writer_payload = json.loads(messages[-1].content.split("\n", 1)[1])
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="关系已由原文支持。"))])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
                "name": "get_graph_relations", "args": {"entity_id": self.root_id}, "id": "g1", "type": "tool_call"}]))])
        def with_structured_output(self, schema, **kwargs):
            def produce(messages):
                if schema.__name__ == "PlanOutput":
                    return {"tasks": [{"task_id": "T1", "description": "调查师承", "status": "pending"}]}
                if schema.__name__ == "AssessmentOutput":
                    payload = json.loads(messages[-1].content.split("\n", 1)[1])
                    graph = payload["tool_results"][0]["tool_result"]
                    edge = next(e for e in graph["edges"] if e["polarity"] == "affirmed")
                    span = edge["evidence_groups"][0]["spans"][0]
                    valid = {"evidence_id": "E1", "task_id": "T1", "claim": "张三拜李四为师", "quote": span["quote"],
                        "chunk_id": span["chunk_id"], "section_title": span["section_title"], "start_line": span["start_line"], "end_line": span["end_line"], "interpretation": "原文直接陈述"}
                    forged = {**valid, "evidence_id": "E2", "quote": "模型伪造的原文不得通过"}
                    return {"evidence": [valid, forged], "sufficient": True, "completed_task_ids": ["T1"]}
                raise AssertionError(schema)
            return RunnableLambda(produce)
    with TestClient(create_app(data_root=tmp_path, graph_model_factory=lambda: script)) as client:
        novel = upload(client)
        client.post(f"/api/novels/{novel}/graph/builds", json={"mode": "start"})
        version = wait_job(client, novel)["active_version"]
        reader = client.app.state.knowledge_graph.capture(novel)
        root = reader.search("张三")["items"][0]["id"]
        result = execute_agent(reader.corpus, "调查张三与李四的师承关系", max_steps=6, thread_id="graph-evidence-test",
            graph_reader=reader, model=ResearchModel(root_id=root), structured_retries=0)
        assert [e["evidence_id"] for e in result.state["evidence"]] == ["E1"]
        assert result.state["graph_version"] == version
        history = result.state["tool_call_history"][0]
        assert history["graph_version"] == version and "E2" in history["rejected_evidence_ids"]
        assert ResearchModel.writer_payload["evidence"][0]["quote"] == result.state["evidence"][0]["quote"]
