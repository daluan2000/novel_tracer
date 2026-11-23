from __future__ import annotations

import copy

import pytest

from novel_agent.corpus.knowledge_graph.concurrency import BuildStopped
from novel_agent.corpus.knowledge_graph.extraction import (
    compact_window, validate_batch, validate_compact_batch, windows,
)
from novel_agent.corpus.knowledge_graph.identity import explicit_identity
from novel_agent.corpus.knowledge_graph.models import (
    BatchResolutionDecision, CompactExtractionBatch, ExtractionBatch, GraphOutputLimitError,
    ResolutionDecision,
)
from novel_agent.corpus.knowledge_graph.resolution import resolve
from novel_agent.corpus.knowledge_graph.selection import select_candidates, select_resolved
from novel_agent.corpus.knowledge_graph.storage import GraphStorage, now
from novel_agent.corpus.repository import NovelCorpus
from novel_agent.runtime.config import graph_min_entity_mentions

from test_knowledge_graph import scripted_merge


def entity(name, position, kind="person", context=None):
    mention = {"start_char": position, "end_char": position + len(name), "quote": name,
               "section_id": "s", "section_order": 0}
    return {"local_id": name, "name": name, "type": kind, "description": "",
            "mentions": [mention], "contexts": [{**mention, "quote": context or name}]}


def batch(unit, entities, links=None, relations=None):
    return {"unit_id": unit, "entities": entities, "identity_links": links or [], "relations": relations or []}


class Checkpoints:
    def __init__(self):
        self.records = {}

    def __call__(self, uid, fingerprint, result=None, sequence=0):
        if result is not None:
            self.records[uid] = (fingerprint, copy.deepcopy(result))
        elif uid in self.records:
            assert self.records[uid][0] == fingerprint
            return copy.deepcopy(self.records[uid][1])


def positive_model(uid, schema, prompt, payload):
    return schema.model_validate(scripted_merge(payload))


@pytest.mark.parametrize("left,right,quote,expected", [
    ("孙悟空", "齐天大圣", "孙悟空又称齐天大圣。", True),
    ("林黛玉", "黛玉", "林黛玉简称黛玉。", True),
    ("袭人", "珍珠", "袭人本名珍珠。", True),
    ("苏禾", "清秋", "苏禾，字清秋。", True),
    ("陆遥", "行云", "行云是陆遥的笔名。", True),
    ("白露", "白霜", "白霜实为白露。", True),
    ("甲", "乙", "甲与乙是同一个人。", True),
    ("甲", "乙", "甲就是乙本人。", True),
    ("宝玉", "林妹妹", "接着茜雪捧上茶来．宝玉因让\"林妹妹吃茶。", False),
    ("宝玉", "姑苏林黛玉", "宝玉道：“姑苏林黛玉。”", False),
    ("宝玉", "黛玉", "宝玉和黛玉二人之亲密友爱处。", False),
    ("宝玉", "林丫头", "宝玉和林丫头是从小儿在一处的。", False),
    ("甲", "乙", "甲不是乙。", False),
    ("甲", "乙", "甲假冒乙，声称甲又名乙。", False),
    ("甲", "乙", "据说甲又名乙。", False),
    ("甲", "乙", "甲是乙的师父。", False),
    ("甲", "乙", "甲又名丙，乙在门外。", False),
    ("甲", "乙", "甲又名乙丙。", False),
    ("姑娘", "林黛玉", "姑娘又名林黛玉。", False),
])
def test_identity_gate_binds_exact_names(left, right, quote, expected):
    assert explicit_identity(left, right, [{"quote": quote}]) is expected


def test_selection_counts_positions_and_preserves_people_and_key_clues():
    ordinary = entity("茶杯", 10, "item")
    critical = entity("血玉", 20, "item")
    critical.update(plot_relevant=True, importance_evidence=[{"quote": "血玉是打开密室的唯一钥匙。"}])
    unsupported = entity("饭碗", 30, "item")
    unsupported["plot_relevant"] = True
    values = [batch("a", [ordinary, critical, unsupported, entity("路人甲", 40), entity("姑娘", 50),
                           entity("青帮", 60, "organization"), entity("庭院", 70, "place")]),
              batch("overlap", [copy.deepcopy(ordinary)]),
              *[batch(str(i), [entity("大观园", 100 + i * 10, "place")]) for i in range(3)]]
    selected, stats = select_candidates(values, 3)
    names = {e["name"] for b in selected for e in b["entities"]}
    assert names == {"血玉", "路人甲", "青帮", "大观园"}
    assert stats["filtered_reasons"] == {"low_frequency_background": 4, "generic_name": 1}
    assert stats["candidates_after"] == {"item": 1, "person": 1, "organization": 1, "place": 3}
    assert values[0]["entities"][0] == ordinary  # Checkpoint candidates stay intact.


def test_selection_removes_incident_facts_without_dangling_references():
    items = [entity("张三", 0), entity("桌椅", 10, "item"), entity("茶杯", 20, "item")]
    values = [batch("u", items, links=[{"source": "桌椅", "target": "茶杯", "evidence": [{"quote": "桌椅和茶杯。"}]}],
                    relations=[{"source": "张三", "target": "茶杯"}])]
    selected, stats = select_candidates(values, 3)
    assert [e["name"] for e in selected[0]["entities"]] == ["张三"]
    assert selected[0]["relations"] == selected[0]["identity_links"] == []
    assert stats["filtered_reasons"]["identity_evidence"] == 1


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_importance_requires_anchored_evidence_in_both_formats(tmp_path, compact, valid):
    source = tmp_path / "story.txt"
    source.write_text("血玉是打开密室的唯一钥匙。张三拿起茶杯。", encoding="utf-8")
    corpus = NovelCorpus.from_path(source)
    window = windows(corpus)[0]
    if compact:
        window, payload = compact_window(corpus, window)
        span = payload["segments"][0]["id"]
        extracted = CompactExtractionBatch.model_validate({"entities": [{"local_id": "e", "name": "血玉", "type": "item",
            "span_id": span, "plot_relevant": True, "importance_evidence_ids": [span if valid else "invalid"]}]})
        result = validate_compact_batch(corpus, window, extracted)
    else:
        extracted = ExtractionBatch.model_validate({"entities": [{"local_id": "e", "name": "血玉", "type": "item",
            "mentions": [{"span_id": "core", "quote": "血玉"}], "plot_relevant": True,
            "importance_evidence": [{"span_id": "core", "quote": "血玉是打开密室的唯一钥匙。" if valid else "不存在的依据"}]}]})
        result = validate_batch(corpus, window, extracted)
    selected, _ = select_candidates([result], 3)
    assert bool(selected[0]["entities"]) is valid
    assert result["entities"][0]["plot_relevant"] is valid


def test_alias_frequency_is_rechecked_after_resolution():
    values = [batch("u", [entity("血玉", 0, "item"), entity("赤玉", 10, "item")], links=[
        {"source": "血玉", "target": "赤玉", "evidence": [{"start_char": 20, "end_char": 27,
         "quote": "血玉又名赤玉。", "section_id": "s"}]}])]
    selected, _ = select_candidates(values, 3)
    assert len(selected[0]["entities"]) == 2  # Keep alias endpoints for resolution.
    entities, relations = resolve(selected, "v", positive_model, Checkpoints(), lambda _: None)
    assert len(entities) == 1
    assert select_resolved(entities, relations, 3) == ([], [])
    assert len(select_resolved(entities, relations, 2)[0]) == 1


def test_alias_endpoints_protect_their_mentions_in_other_windows():
    values = [batch("u", [entity("血玉", 0, "item"), entity("赤玉", 10, "item")], links=[
        {"source": "血玉", "target": "赤玉", "evidence": [{"start_char": 20, "end_char": 27,
         "quote": "血玉又名赤玉。", "section_id": "s"}]}]), batch("later", [entity("赤玉", 100, "item")])]
    selected, _ = select_candidates(values, 3)
    assert len(selected[1]["entities"]) == 1
    entities, relations = resolve(selected, "v", positive_model, Checkpoints(), lambda _: None)
    assert len(select_resolved(entities, relations, 3)[0]) == 1


def test_nested_aliases_at_one_position_count_as_one_mention():
    value = {"id": "e", "type": "item", "mentions": [{"start_char": 0, "end_char": 4},
             {"start_char": 2, "end_char": 4}, {"start_char": 10, "end_char": 12}]}
    assert select_resolved([value], [], 3) == ([], [])


def test_real_bad_identity_proposals_cannot_merge_even_with_positive_model():
    values = [batch("u", [entity("宝玉", 0), entity("林妹妹", 10), entity("黛玉", 20)], links=[
        {"source": "林妹妹", "target": "黛玉", "evidence": [{"start_char": 30, "end_char": 40,
         "quote": "黛玉又称林妹妹。", "section_id": "s"}]},
        {"source": "宝玉", "target": "林妹妹", "evidence": [{"start_char": 40, "end_char": 60,
         "quote": "宝玉因让\"林妹妹吃茶。", "section_id": "s"}]}])]
    events = []
    entities, _ = resolve(values, "v", positive_model, Checkpoints(), events.append)
    assert len(entities) == 2
    assert not any({"宝玉", "黛玉"} <= {e["name"], *e["aliases"]} for e in entities)
    assert "entity_identity_evidence_rejected" in events


def test_distinct_people_constraint_survives_transitive_alias_union():
    values = [batch("u", [entity("甲", 0, context="甲与丙不是同一人。"), entity("乙", 10), entity("丙", 20)], links=[
        {"source": "甲", "target": "乙", "evidence": [{"start_char": 30, "end_char": 36, "quote": "甲又名乙。", "section_id": "s"}]},
        {"source": "乙", "target": "丙", "evidence": [{"start_char": 40, "end_char": 46, "quote": "乙又名丙。", "section_id": "s"}]}])]
    entities, _ = resolve(values, "v", positive_model, Checkpoints(), lambda _: None)
    assert len(entities) == 2
    assert not any({"甲", "丙"} <= {e["name"], *e["aliases"]} for e in entities)


def repeated_people(count=65):
    return [batch(f"u{i}", [entity("张三", i * 100)]) for i in range(count)]


def test_batching_reduces_calls_and_matches_serial_parallel_and_replay():
    calls = []
    def invoke(uid, schema, prompt, payload):
        calls.append(uid)
        return positive_model(uid, schema, prompt, payload)
    original, records = repeated_people(), Checkpoints()
    single = resolve(original, "v", invoke, Checkpoints(), lambda _: None, batch_size=1)
    assert len(calls) == 64
    calls.clear()
    batched = resolve(original, "v", invoke, records, lambda _: None, batch_size=16)
    assert len(calls) == 4
    assert batched == single
    assert len(batched[0][0]["mentions"]) == 65
    assert resolve(original, "v", lambda *_: pytest.fail("replay called model"), records,
                   lambda _: None, batch_size=16, concurrency=4) == batched
    parallel = resolve(original, "v", invoke, Checkpoints(), lambda _: None, batch_size=16, concurrency=4)
    assert parallel == batched


def test_stop_after_one_applied_prediction_reuses_remaining_batch():
    records, calls = Checkpoints(), []
    def invoke(*args):
        calls.append(args[0])
        return positive_model(*args)
    def stop():
        if "resolve:1" in records.records:
            raise BuildStopped()
    with pytest.raises(BuildStopped):
        resolve(repeated_people(10), "v", invoke, records, lambda _: None, batch_size=16, check=stop)
    assert len(calls) == 1
    assert len(resolve(repeated_people(10), "v", lambda *_: pytest.fail("cached predictions called model"),
                       records, lambda _: None, batch_size=16)[0]) == 1


def test_batch_can_reference_an_earlier_record_and_preserves_checkpoint_replay():
    records = Checkpoints()
    def invoke(uid, schema, prompt, payload):
        decisions = scripted_merge(payload)["decisions"]
        for index in range(1, len(decisions)):
            previous, current = payload["entities"][index - 1:index + 1]
            decisions[index]["candidate_id"] = previous["entity_ref"]
            decisions[index]["supporting_mentions"] = [previous["entity"]["excerpts"][0]["mention_id"],
                                                         current["entity"]["excerpts"][0]["mention_id"]]
        return schema.model_validate({"decisions": decisions})
    result = resolve(repeated_people(10), "v", invoke, records, lambda _: None, batch_size=16)
    assert len(result[0]) == 1 and len(result[0][0]["mentions"]) == 10
    assert resolve(repeated_people(10), "v", lambda *_: pytest.fail("replay called model"), records,
                   lambda _: None, batch_size=16) == result


def test_batch_skips_duplicate_original_mentions_before_requesting_model():
    values = repeated_people(34)
    values += [{**copy.deepcopy(value), "unit_id": "overlap:" + value["unit_id"]} for value in values]
    sizes = []
    def invoke(uid, schema, prompt, payload):
        sizes.append(len(payload["entities"]))
        return positive_model(uid, schema, prompt, payload)
    result = resolve(values, "v", invoke, Checkpoints(), lambda _: None, batch_size=16)
    assert sizes == [16, 16, 1]
    assert len(result[0]) == 1 and len(result[0][0]["mentions"]) == 34


@pytest.mark.parametrize("failure", [TimeoutError, GraphOutputLimitError])
def test_resolution_batch_split_is_cached_and_replayable(failure):
    records, sizes, events = Checkpoints(), [], []
    def invoke(uid, schema, prompt, payload):
        sizes.append(len(payload["entities"]))
        if len(payload["entities"]) > 2:
            raise failure()
        return positive_model(uid, schema, prompt, payload)
    result = resolve(repeated_people(9), "v", invoke, records, events.append, batch_size=16)
    assert sizes == [8, 4, 2, 2, 4, 2, 2]
    assert len(result[0]) == 1
    assert "graph_resolution_batch_split" in events
    assert resolve(repeated_people(9), "v", lambda *_: pytest.fail("split replay called model"),
                   records, events.append, batch_size=16) == result


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "forward", "unknown", "one_sided"])
def test_invalid_batch_decisions_keep_people_separate(invalid):
    def invoke(uid, schema, prompt, payload):
        decisions = scripted_merge(payload)["decisions"]
        if invalid == "missing":
            decisions = []
        elif invalid == "duplicate":
            decisions = [decisions[0], decisions[0]]
        elif invalid == "forward":
            decisions[0]["candidate_id"] = "r2"
        elif invalid == "unknown":
            decisions[0]["supporting_mentions"] = ["invented"]
        else:
            decisions[0]["supporting_mentions"] = [payload["entities"][0]["entity"]["excerpts"][0]["mention_id"]]
        return schema.model_validate({"decisions": decisions})
    entities, _ = resolve(repeated_people(3), "v", invoke, Checkpoints(), lambda _: None, batch_size=16)
    assert len(entities) >= 2


def test_minimum_mentions_configuration(monkeypatch):
    monkeypatch.delenv("NOVEL_AGENT_GRAPH_MIN_ENTITY_MENTIONS", raising=False)
    assert graph_min_entity_mentions() == 3
    for value in ["0", "101", "many", "1.5", ""]:
        monkeypatch.setenv("NOVEL_AGENT_GRAPH_MIN_ENTITY_MENTIONS", value)
        with pytest.raises(RuntimeError):
            graph_min_entity_mentions()
    for value in ["1", "100"]:
        monkeypatch.setenv("NOVEL_AGENT_GRAPH_MIN_ENTITY_MENTIONS", value)
        assert graph_min_entity_mentions() == int(value)


def test_resolution_preparation_preserves_checkpoints_and_reserves_progress(tmp_path):
    storage = GraphStorage(tmp_path)
    storage.open()
    try:
        with storage.connect() as db:
            db.execute("INSERT INTO graph_versions(version,novel_id,state,fingerprint,source_identity,model_identity,created_at) VALUES('v','n','staging','f','{}','{}',?)", (now(),))
        decision = {"decision": "ambiguous", "candidate_id": None, "supporting_mentions": []}
        storage.save_unit("v", "resolve:0", "resolve", 0, "existing", decision)
        storage.save_predictions("v", [("resolve:prediction:1", "prediction", {"decision": decision}, 1)])
        loaded = storage.prepare_resolution("v", 2, 1)
        assert loaded["resolve:0"] == {"input_hash": "existing", "result": decision}
        assert loaded["resolve:prediction:1"]["input_hash"] == "prediction"
        assert storage.unit("v", "resolve:1")["status"] == "pending"
        assert storage.unit("v", "resolve:identity:0")["sequence"] == 2
        assert storage.prepare_resolution("v", 2, 1) == loaded
    finally:
        storage.close()
