"""Model-free end-to-end build benchmark with fixed simulated request latency.

Run: python -X utf8 scripts/benchmark_graph_concurrency.py
Only generated files below tmp/ are written. No network requests are made.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from novel_agent.corpus.knowledge_graph.service import KnowledgeGraphService
from novel_agent.corpus.repository import NovelCorpus


class DelayedModel:
    def __init__(self, delay):
        self.delay = delay

    def with_structured_output(self, schema, **kwargs):
        def invoke(messages):
            time.sleep(self.delay)
            payload = json.loads(messages[1].content)
            if schema.__name__ == "CatalogDecision":
                return {"profiles": [{"anchor_mention_id": c["excerpts"][0]["mention_id"],
                    "canonical_name": c["name"], "name_forms": [{"name": c["name"], "kind": "proper_name"}],
                    "supporting_mentions": [c["excerpts"][0]["mention_id"]]} for c in payload["cards"]]}
            if schema.__name__ == "BatchResolutionDecision":
                return {"decisions": [{"entity_ref": entry["entity_ref"], "decision": "merge",
                    "candidate_id": entry["candidates"][0]["candidate_id"], "supporting_mentions": [
                        entry["entity"]["excerpts"][0]["mention_id"], entry["candidates"][0]["excerpts"][0]["mention_id"]]}
                    for entry in payload["entities"]]}
            if schema.__name__ == "ResolutionDecision":
                return {"decision": "merge", "candidate_id": payload["candidates"][0]["candidate_id"],
                        "supporting_mentions": [payload["entity"]["excerpts"][0]["mention_id"],
                                                payload["candidates"][0]["excerpts"][0]["mention_id"]]}
            core = payload["spans"][0]["text"]
            name = re.search(r"人物[甲乙丙丁戊己庚辛]", core).group()
            return {"entities": [{"local_id": "e", "name": name, "type": "person",
                                  "mentions": [{"span_id": "core", "quote": name, "occurrence": 1}]}]}

        return SimpleNamespace(invoke=invoke)


def run(root, workers, delay):
    root.mkdir(parents=True)
    source = root / "source.txt"
    source.write_text("\n".join(
        f"第{round_no * 8 + index + 1}章 散步\n{name}走过山路。" + "山路悠长，风吹过树梢。" * 40
        for round_no in range(4) for index, name in enumerate(f"人物{x}" for x in "甲乙丙丁戊己庚辛")),
        encoding="utf-8")
    corpus = NovelCorpus.from_path(source)
    item = SimpleNamespace(novel_id="benchmark", corpus=corpus,
                           source_hash=hashlib.sha256(source.read_bytes()).hexdigest())
    novels = SimpleNamespace(get=lambda _: item, list=lambda: [item])
    service = KnowledgeGraphService(root, novels, model_factory=lambda: DelayedModel(delay))
    with patch.dict(os.environ, {"OPENAI_API_KEY": "benchmark-no-network",
                    "NOVEL_AGENT_GRAPH_EXTRACTION_CONCURRENCY": str(workers),
                    "NOVEL_AGENT_GRAPH_RESOLUTION_CONCURRENCY": str(workers)}):
        service.open()
        try:
            service.build(item.novel_id)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                job = service.storage.job(item.novel_id)
                if job["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("benchmark build timed out")
        finally:
            service.close()
        result = service.status(item.novel_id)
        assert result["job"]["status"] == "completed", result["job"]
        return {"workers": workers, "elapsed_seconds": result["job"]["elapsed_seconds"],
                "phase_seconds": result["job"]["phase_elapsed_seconds"],
                "model_seconds": result["job"]["phase_model_seconds"],
                "requests": result["job"]["usage"]["request_count"], "counts": result["counts"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=0.15)
    args = parser.parse_args()
    if not 0.05 <= args.delay <= 1:
        parser.error("--delay must be between 0.05 and 1 second")
    root = Path(__file__).resolve().parents[1] / "tmp" / f"graph-concurrency-{uuid.uuid4().hex[:8]}"
    serial = run(root / "serial", 1, args.delay)
    concurrent = run(root / "concurrent", 4, args.delay)
    assert serial["requests"] == concurrent["requests"]
    assert serial["counts"] == concurrent["counts"]
    speedup = {phase: serial["phase_seconds"][phase] / concurrent["phase_seconds"][phase]
               for phase in ("extracting", "cataloging")}
    report = {"fixture": "8 independent names, 4 occurrences each, fixed model latency; no network",
              "delay_seconds": args.delay, "serial": serial, "concurrent": concurrent,
              "phase_speedup": speedup,
              "total_speedup": serial["elapsed_seconds"] / concurrent["elapsed_seconds"]}
    report_path = root / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), **report}, ensure_ascii=False, indent=2))
    assert all(ratio >= 2.5 for ratio in speedup.values()), "expected at least 2.5x phase speedup"


if __name__ == "__main__":
    main()
