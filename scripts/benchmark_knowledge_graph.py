"""Reproducible, model-free graph benchmark; writes only generated data under tmp/."""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from novel_agent.corpus.knowledge_graph.query import GraphReadSession
from novel_agent.corpus.knowledge_graph.storage import GraphStorage, now
from novel_agent.corpus.repository import NovelCorpus


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    workspace = Path(__file__).resolve().parents[1]
    (workspace / "tmp").mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="graph-benchmark-", dir=workspace / "tmp"))
    source = root / "synthetic.txt"
    source.write_text("甲认识乙。\n" + "仅供生成数据测试。" * 100, encoding="utf-8")
    corpus = NovelCorpus.from_path(source)
    chunk = corpus.document.chunks[0]
    span = {"chunk_id": chunk.chunk_id, "section_id": chunk.section_id, "section_title": chunk.section_title,
            "section_order": 0, "start_char": chunk.start_char, "end_char": chunk.start_char + 5,
            "start_line": chunk.start_line, "end_line": chunk.start_line, "quote": chunk.text[:5]}
    storage = GraphStorage(root)
    storage.open()
    try:
        with storage.connect() as db:
            db.execute("INSERT INTO graph_versions(version,novel_id,state,fingerprint,source_identity,model_identity,created_at) VALUES('v','n','staging','f','{}','{}',?)", (now(),))
            db.execute("INSERT INTO graph_jobs(job_id,novel_id,version,status,created_at,updated_at) VALUES('j','n','v','building',?,?)", (now(), now()))
        entities = [{"id": f"n{i:05d}", "name": f"人物{i:05d}", "type": "person", "aliases": [f"别名{i:05d}"],
                     "mentions": [{**span, "start_char": i * 10, "end_char": i * 10 + 5}]} for i in range(10000)]
        # Storage/query load fixture only: synthetic evidence is not an extraction quality fixture.
        relations = []
        for i in range(10000):
            for step in (1, 3, 7, 17, 43):
                target = (i + step) % 9000 if i < 9000 else 9000 + (i - 9000 + step) % 1000
                relations.append({"id": f"r{i:05d}_{step}", "source": f"n{i:05d}", "target": f"n{target:05d}",
                    "type": "knows", "polarity": "affirmed", "statement_mode": "narration", "groups": [[span]]})
        relations[:1000] = [{"id": f"hub{i}", "source": "n00000", "target": f"n{i + 5000:05d}",
            "type": "knows", "polarity": "affirmed", "statement_mode": "narration", "groups": [[span]]} for i in range(1000)]
        storage.materialize("v", entities, relations)
        storage.publish("n", "v", "j")
        reader = GraphReadSession(storage, corpus, "n", "v")
        results = {}
        for name, query in {
            "entity_search": lambda i: reader.search(f"人物{i:05d}"),
            "one_hop": lambda i: reader.neighbors(f"n{i:05d}"),
            "high_degree_one_hop": lambda i: reader.neighbors("n00000"),
            "three_hop": lambda i: reader.paths(f"n{i:05d}", f"n{(i + 61) % 10000:05d}"),
            "no_path": lambda i: reader.paths(f"n{i % 9000:05d}", f"n{9000 + i % 1000:05d}"),
        }.items():
            cold = time.perf_counter()
            query(0)
            cold_ms = (time.perf_counter() - cold) * 1000
            timings, complete, reasons = [], 0, {}
            for i in range(args.iterations):
                started = time.perf_counter()
                result = query((i * 73) % 10000)
                if name == "no_path":
                    assert not result["paths"]
                timings.append((time.perf_counter() - started) * 1000)
                complete += int(result["search_complete"])
                reason = result["truncation_reason"] or "none"
                reasons[reason] = reasons.get(reason, 0) + 1
            timings.sort()
            results[name] = {"cold_ms": round(cold_ms, 2), "p50_ms": round(statistics.median(timings), 2),
                             "p95_ms": round(timings[math.ceil(len(timings) * 0.95) - 1], 2),
                             "complete_ratio": complete / args.iterations, "truncation_reasons": reasons}
            print(f"Finished {name}: P95={results[name]['p95_ms']}ms", flush=True)
        report = {"nodes": 10000, "edges": 50000, "iterations": args.iterations,
                  "python": sys.version.split()[0], "platform": sys.platform, "results": results,
                  "hardware": {"machine": platform.machine(), "processor": platform.processor(), "logical_cpus": os.cpu_count()},
                  "fixture_note": "synthetic storage/query benchmark, not model quality evaluation"}
        report_path = root / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(report_path), **report}, ensure_ascii=False, indent=2))
        assert results["entity_search"]["p95_ms"] <= 1000
        assert results["one_hop"]["p95_ms"] <= 1000
        assert results["high_degree_one_hop"]["p95_ms"] <= 1000
        assert results["three_hop"]["p95_ms"] <= 3000
    finally:
        storage.close()


if __name__ == "__main__":
    main()
