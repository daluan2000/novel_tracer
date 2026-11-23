"""Opt-in live-model quality evaluation on authored, synthetic reference cases."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from novel_agent.corpus.repository import NovelCorpus
from novel_agent.corpus.knowledge_graph.ontology import SYMMETRIC, digest
from novel_agent.corpus.knowledge_graph.service import KnowledgeGraphService, model_identity
from novel_agent.runtime.config import ModelConfig


def main():
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "tests/fixtures/knowledge_graph_eval.json")
    parser.add_argument("--run-model", action="store_true", help="Send synthetic reference text to the configured model; consumes paid model usage.")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = ([("identity", c) for c in dataset["identity_cases"]]
             + [("relation", c) for c in dataset["relation_cases"]]
             + [("selection", c) for c in dataset.get("selection_cases", [])])
    if not args.run_model:
        print(json.dumps({"identity_cases": len(dataset["identity_cases"]), "relation_cases": len(dataset["relation_cases"]),
                          "selection_cases": len(dataset.get("selection_cases", [])), "model_calls": 0,
                          "instruction": "Use --run-model explicitly to run the paid quality evaluation."}, indent=2))
        return
    config = ModelConfig.from_env()
    (workspace / "tmp").mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="graph-quality-", dir=workspace / "tmp"))
    items = {}
    for _, case in cases:
        path = root / (case["id"] + ".txt")
        path.write_text(case["text"], encoding="utf-8")
        corpus = NovelCorpus.from_path(path)
        items[case["id"]] = SimpleNamespace(novel_id=case["id"], corpus=corpus,
            source_hash=__import__("hashlib").sha256(corpus.document.source.text.encode()).hexdigest())
    service = KnowledgeGraphService(root, SimpleNamespace(list=lambda: list(items.values()), get=lambda key: items[key]))
    records = []
    started = time.monotonic()
    prior_minimum = os.environ.get("NOVEL_AGENT_GRAPH_MIN_ENTITY_MENTIONS")
    service.open()
    try:
        if service.error_code:
            raise RuntimeError(service.error_code)
        for kind, case in cases:
            # Tiny relation cases assess extraction separately from selection;
            # salience cases always exercise the production default threshold.
            os.environ["NOVEL_AGENT_GRAPH_MIN_ENTITY_MENTIONS"] = "1" if kind == "relation" else "3"
            job, _ = service.build(case["id"])
            while True:
                status = service.status(case["id"])
                if status["job"]["status"] in {"completed", "failed", "stopped"}:
                    break
                if time.monotonic() - started > args.timeout:
                    service.stop(case["id"], job["job_id"])
                    raise TimeoutError("Quality evaluation timeout; current request stops at its boundary.")
                time.sleep(0.2)
            record = {"id": case["id"], "kind": kind, "passed": False, "status": status["job"]["status"],
                      "usage": status["job"]["usage"], "elapsed_seconds": status["job"]["elapsed_seconds"]}
            if status["available"]:
                version = status["active_version"]
                with service.storage.connect() as db:
                    if kind == "identity":
                        ids = []
                        for side in ("left", "right"):
                            name, occurrence = case[side], case.get(side + "_occurrence", 1)
                            position = -1
                            for _ in range(occurrence):
                                position = case["text"].find(name, position + 1)
                            row = db.execute("SELECT entity_id FROM graph_mentions WHERE version=? AND start_char=? AND end_char=?", (version, position, position + len(name))).fetchone()
                            ids.append(row[0] if row else None)
                        record["covered"] = all(ids)
                        record["same_expected"] = case["same"]
                        record["predicted_same"] = ids[0] == ids[1] if all(ids) else None
                        record["passed"] = bool(all(ids) and ((ids[0] == ids[1]) == case["same"]))
                    elif kind == "selection":
                        names = {r[0] for r in db.execute("SELECT name FROM graph_entities WHERE version=?", (version,))}
                        names.update(r[0] for r in db.execute("SELECT alias FROM graph_aliases WHERE version=?", (version,)))
                        record["retained_key_entities"] = sum(name in names for name in case["keep"])
                        record["retained_background_entities"] = sum(name in names for name in case["remove"])
                        record["passed"] = record["retained_key_entities"] == len(case["keep"]) and record["retained_background_entities"] == 0
                    else:
                        predictions = db.execute("SELECT * FROM graph_relations WHERE version=?", (version,)).fetchall()
                        names = {e["entity_id"]: {e["name"]} for e in db.execute("SELECT entity_id,name FROM graph_entities WHERE version=?", (version,))}
                        for alias in db.execute("SELECT entity_id,alias FROM graph_aliases WHERE version=?", (version,)):
                            names[alias["entity_id"]].add(alias["alias"])
                        for edge in predictions:
                            same_ends = case["source"] in names[edge["source_id"]] and case["target"] in names[edge["target_id"]]
                            if case["type"] in SYMMETRIC:
                                same_ends |= case["target"] in names[edge["source_id"]] and case["source"] in names[edge["target_id"]]
                            if same_ends and edge["relation_type"] == case["type"] and edge["polarity"] == case.get("polarity", "affirmed") and edge["statement_mode"] == case.get("statement_mode", "narration"):
                                record["passed"] = True
                        record["predicted_relations"] = len(predictions)
            records.append(record)
            print(f"{case['id']}: {'PASS' if record['passed'] else 'FAIL'}", flush=True)
    finally:
        service.close()
        if prior_minimum is None:
            os.environ.pop("NOVEL_AGENT_GRAPH_MIN_ENTITY_MENTIONS", None)
        else:
            os.environ["NOVEL_AGENT_GRAPH_MIN_ENTITY_MENTIONS"] = prior_minimum
        relations = [r for r in records if r["kind"] == "relation"]
        true_positive = sum(r["passed"] for r in relations)
        predicted = sum(r.get("predicted_relations", 0) for r in relations)
        identity = [r for r in records if r["kind"] == "identity"]
        selections = [r for r in records if r["kind"] == "selection"]
        expected_keys = sum(len(c["keep"]) for c in dataset.get("selection_cases", []))
        expected_background = sum(len(c["remove"]) for c in dataset.get("selection_cases", []))
        report = {"model": model_identity(config), "dataset_hash": digest(dataset), "completed_cases": len(records),
                  "elapsed_seconds": time.monotonic() - started, "relation_precision": true_positive / predicted if predicted else 0,
                  "relation_recall": true_positive / len(dataset["relation_cases"]),
                  "identity_accuracy": sum(r["passed"] for r in identity) / len(dataset["identity_cases"]),
                  "false_merges": sum(not r.get("same_expected", True) and r.get("predicted_same") is True for r in identity),
                  "identity_splits": sum(r.get("same_expected", False) and r.get("predicted_same") is False for r in identity),
                  "identity_coverage": sum(bool(r.get("covered")) for r in identity) / len(dataset["identity_cases"]),
                  "key_entity_recall": sum(r.get("retained_key_entities", 0) for r in selections) / expected_keys if expected_keys else None,
                  "background_retention": sum(r.get("retained_background_entities", 0) for r in selections) / expected_background if expected_background else None,
                  "relation_minimum_mentions": 1, "selection_minimum_mentions": 3, "cases": records}
        path = root / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Report: {path}", flush=True)


if __name__ == "__main__":
    main()
