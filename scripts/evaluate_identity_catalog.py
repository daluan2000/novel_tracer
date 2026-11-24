"""Book-wide identity evaluation on annotated, source-anchored novel samples.

Default: inspect the dataset without model calls. --run-model explicitly enables
the configured model in an isolated temporary database, never the user's graph.
This evaluates catalog/binding quality on saved extraction candidates, not NER
recall or semantic accuracy of the original candidate predicates.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from novel_agent.corpus.knowledge_graph.catalog import build_catalog
from novel_agent.corpus.knowledge_graph.ontology import digest
from novel_agent.corpus.knowledge_graph.service import KnowledgeGraphService, model_identity
from novel_agent.corpus.knowledge_graph.storage import now
from novel_agent.runtime.config import ModelConfig


def sample_batches(dataset):
    entries = [{"local_id": m["mention_id"], "name": m["surface"], "type": m["type"],
                "mentions": [m["anchor"]], "contexts": m["contexts"],
                "description": " ".join(m.get("descriptions", [])),
                "plot_relevant": m.get("plot_relevant", False),
                "importance_evidence": m.get("importance_evidence", [])} for m in dataset["mentions"]]
    facts = [{**{key: f[key] for key in ("type", "polarity", "statement_mode", "evidence")},
              "source": f["source_mentions"][0], "target": f["target_mentions"][0]} for f in dataset["facts"]]
    hints = [{"source": h["source_mentions"][0], "target": h["target_mentions"][0], "evidence": h["evidence"]}
             for h in dataset.get("identity_hints", []) if len(h["source_mentions"]) == len(h["target_mentions"]) == 1]
    return [{"unit_id": "annotated-sample", "entities": entries, "relations": facts, "identity_links": hints}]


def score(dataset, result):
    gold = {m["mention_id"]: m["gold_identity"] for m in dataset["mentions"]}
    canonical = {i["id"]: i["canonical_name"] for i in dataset["identities"]}
    predicted = {b["mention_id"]: b["entity_id"] for b in result.bindings if b["status"] == "resolved"}
    names = {e["id"]: e["name"] for e in result.entities}
    truth, clusters = defaultdict(set), defaultdict(set)
    for mid, label in gold.items():
        truth[label].add(mid)
        clusters[predicted.get(mid, "pending:" + mid)].add(mid)
    precision, recall, correct = 0.0, 0.0, 0
    for mid, label in gold.items():
        cluster = clusters[predicted.get(mid, "pending:" + mid)]
        overlap = len(cluster & truth[label])
        precision += overlap / len(cluster)
        recall += overlap / len(truth[label])
        correct += mid in predicted and names[predicted[mid]] == canonical[label]
    n = len(gold)
    critical = {}
    for label in ("林黛玉", "贾宝玉", "甄宝玉"):
        if label not in truth:
            continue
        ids = {predicted[mid] for mid in truth[label] if mid in predicted}
        resolved = sum(mid in predicted for mid in truth[label])
        critical[label] = {"mentions": len(truth[label]), "resolved": resolved, "clusters": len(ids),
            "passed": resolved == len(truth[label]) and len(ids) == 1 and names[next(iter(ids))] == canonical[label]}
    endpoint_correct = 0
    fact_records = {f["candidate_id"]: f for f in result.facts}
    for fact in dataset["facts"]:
        a, b = fact["source_mentions"][0], fact["target_mentions"][0]
        materialized = fact_records.get(fact["candidate_id"], {})
        endpoint_correct += (a in predicted and b in predicted and materialized.get("status") == "resolved"
            and names[predicted[a]] == canonical[fact["source_gold"]]
            and names[predicted[b]] == canonical[fact["target_gold"]])
    lookup = defaultdict(set)
    for entity in result.entities:
        for form in [entity["name"], *entity["aliases"]]:
            lookup[form].add(entity["name"])
    critical_aliases = {}
    for form in ("黛玉", "林黛玉", "林妹妹", "宝玉", "宝玉哥哥", "贾宝玉", "甄宝玉"):
        expected = {canonical[m["gold_identity"]] for m in dataset["mentions"] if m["surface"] == form}
        if expected:
            critical_aliases[form] = {"expected": sorted(expected), "actual": sorted(lookup[form]), "passed": expected == lookup[form]}
    metrics = {"mentions": n, "identity_count": len(truth), "fact_candidates": len(dataset["facts"]),
        "coverage": len(predicted) / n, "binding_accuracy": correct / len(predicted) if predicted else 0,
        "cluster_precision": precision / n, "cluster_recall": recall / n,
        "false_merge_clusters": sum(len({gold[mid] for mid in members}) > 1 for members in clusters.values()),
        "split_identities": sum(len({predicted[mid] for mid in members if mid in predicted}) > 1 for members in truth.values()),
        "fact_endpoint_accuracy": endpoint_correct / len(dataset["facts"]) if dataset["facts"] else 1,
        "critical": critical, "critical_aliases": critical_aliases, "pending_mentions": n - len(predicted)}
    metrics["passed"] = (metrics["coverage"] >= .95 and metrics["binding_accuracy"] >= .98
        and metrics["cluster_precision"] >= .98 and metrics["cluster_recall"] >= .95
        and metrics["fact_endpoint_accuracy"] >= .95 and all(c["passed"] for c in critical.values())
        and all(c["passed"] for c in critical_aliases.values()))
    return metrics


def main():
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "tests/fixtures/red_chamber_identity.json")
    parser.add_argument("--run-model", action="store_true")
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--resume-dir", type=Path, help="Reuse paid candidate decisions in a prior isolated evaluation directory")
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 200 or not 1 <= args.workers <= 8:
        parser.error("max-requests must be 1–200; workers must be 1–8")
    data = json.loads(args.dataset.read_text(encoding="utf-8"))
    summary = {"mentions": len(data["mentions"]), "identities": len(data["identities"]),
        "facts": len(data["facts"]), "chapters": len({m["anchor"]["section_id"] for m in data["mentions"]}),
        "annotation_scope": data["annotation_scope"], "dataset_hash": digest(data), "model_calls": 0}
    if not args.run_model:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    config = replace(ModelConfig.from_env(), request_timeout_seconds=90)
    previous = {}
    if args.resume_dir:
        root = args.resume_dir.resolve()
        if not root.is_relative_to((workspace / "tmp").resolve()):
            parser.error("resume-dir must be inside the workspace tmp directory")
        previous = json.loads((root / "report.json").read_text(encoding="utf-8"))
        if previous["dataset_hash"] != digest(data) or previous["model"] != model_identity(config):
            parser.error("dataset or model differs from the cached evaluation")
    else:
        root = Path(tempfile.mkdtemp(prefix="identity-eval-", dir=workspace / "tmp"))
    service = KnowledgeGraphService(root, SimpleNamespace())
    service.storage.open()
    with service.storage.connect() as db:
        if previous:
            version, job_id = db.execute("SELECT version,job_id FROM graph_jobs LIMIT 1").fetchone()
            db.execute("UPDATE graph_jobs SET status='building',stop_requested=0,error_code=NULL,phase='cataloging' WHERE job_id=?", (job_id,))
        else:
            version, job_id = uuid.uuid4().hex, uuid.uuid4().hex
            db.execute("INSERT INTO graph_versions VALUES(?,?,'staging',?,'{}','{}',?,NULL,'{}')", (version, "sample", digest(data), now()))
            db.execute("INSERT INTO graph_jobs(job_id,novel_id,version,status,phase,created_at,updated_at) VALUES(?,'sample',?,'building','cataloging',?,?)", (job_id, version, now(), now()))
    job = {"job_id": job_id, "version": version, "novel_id": "sample", "phase": "cataloging"}
    local, mutex, saved = threading.local(), threading.Lock(), {}
    with service.storage.connect() as db:
        saved = {r["unit_id"]: (r["input_hash"], json.loads(r["result_json"]))
                 for r in db.execute("SELECT * FROM graph_units WHERE version=? AND status='completed'", (version,))}
    prior_requests = requests = previous.get("model_calls", 0)
    class BudgetedModel:
        def __init__(self):
            self.model = config.create_model(max_retries=0, max_tokens=6144)
        def with_structured_output(self, schema, **kwargs):
            runnable = self.model.with_structured_output(schema, **kwargs)
            def invoke(messages):
                nonlocal requests
                with mutex:
                    if requests >= args.max_requests:
                        raise RuntimeError("evaluation_request_budget_exceeded")
                    requests += 1
                return runnable.invoke(messages)
            return SimpleNamespace(invoke=invoke)
    def checkpoint(uid, fingerprint, result=None, sequence=0):
        if result is not None:
            service.storage.save_unit(version, uid, uid.split(":")[0], sequence, fingerprint, result)
            with mutex:
                saved[uid] = (fingerprint, result)
        else:
            with mutex:
                cached = saved.get(uid)
            if cached is not None:
                if cached[0] == fingerprint:
                    return cached[1]
    def invoke(uid, schema, prompt, payload):
        if not hasattr(local, "model"):
            local.model = BudgetedModel()
        return service._invoke(job, local.model, uid, schema, prompt, payload, timeout_retries=0)
    started = time.monotonic()
    report = {**summary, "model": model_identity(config), "scope": "catalog and binding on saved candidates"}
    result = None
    try:
        result = build_catalog(sample_batches(data), version, invoke, checkpoint,
            lambda code: service.storage.event(job_id, code), concurrency=args.workers,
            on_phase=lambda phase: service._set_phase(job, phase))
        report["metrics"] = score(data, result)
        report["stats"] = result.stats
        service.storage.save_identity(version, result)
        (root / "result.json").write_text(json.dumps({"entities": result.entities, "bindings": result.bindings,
            "decisions": result.decisions}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        service._finish_phase()
        with service.storage.connect() as db:
            attempts = db.execute("SELECT usage_json,status FROM graph_attempts WHERE version=?", (version,)).fetchall()
        usage = [json.loads(a[0]) if a[0] else {} for a in attempts]
        report.update(model_calls=requests, new_model_calls=requests - prior_requests,
            elapsed_seconds=previous.get("elapsed_seconds", 0) + time.monotonic() - started,
            reported_input_tokens=sum(u.get("input_tokens") or 0 for u in usage),
            reported_output_tokens=sum(u.get("output_tokens") or 0 for u in usage),
            unknown_usage_attempts=sum(u.get("input_tokens") is None for u in usage))
        service.storage.close()
        path = root / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(path), **report}, ensure_ascii=False, indent=2), flush=True)
    if not report.get("metrics", {}).get("passed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
