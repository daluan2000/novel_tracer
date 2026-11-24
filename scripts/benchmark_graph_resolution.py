"""Model-free comparison of per-record and batched identity resolution.

Run: python -X utf8 scripts/benchmark_graph_resolution.py --delay 0.01
Simulated latency is fixed; this does not predict live-model quality or latency.
"""
from __future__ import annotations

import argparse
import json
import time

from novel_agent.corpus.knowledge_graph.catalog import build_catalog


def fixture():
    batches = []
    for index in range(65):
        for person, name in enumerate(("林岚", "沈砚", "苏禾", "顾舟")):
            start = (index * 4 + person) * 100
            mention = {"start_char": start, "end_char": start + len(name), "quote": name,
                       "section_id": str(index), "section_order": index}
            batches.append({"unit_id": f"u{index}:{person}", "entities": [{
                "local_id": "e", "name": name, "type": "person", "description": "",
                "mentions": [mention], "contexts": [{**mention, "quote": f"{name}继续赶路。"}]}],
                "relations": [], "identity_links": []})
    return batches


def run(batches, workers, delay):
    requests, saved = 0, {}

    def checkpoint(uid, fingerprint, result=None, sequence=0):
        if result is not None:
            saved[uid] = (fingerprint, result)
        elif uid in saved:
            assert saved[uid][0] == fingerprint
            return saved[uid][1]

    def invoke(uid, schema, prompt, payload):
        nonlocal requests
        requests += 1
        time.sleep(delay)
        value = {"profiles": [{"anchor_mention_id": card["excerpts"][0]["mention_id"],
            "canonical_name": card["name"], "name_forms": [{"name": card["name"]}],
            "supporting_mentions": [card["excerpts"][0]["mention_id"]]} for card in payload["cards"]]}
        return schema.model_validate(value)

    started = time.monotonic()
    result = build_catalog(batches, "benchmark", invoke, checkpoint, lambda _: None, concurrency=workers)
    elapsed = time.monotonic() - started
    assert len(result.entities) == 4 and all(len(e["mentions"]) == 65 for e in result.entities)
    assert build_catalog(batches, "benchmark", lambda *_: (_ for _ in ()).throw(AssertionError("replay called model")),
                         checkpoint, lambda _: None, concurrency=1) == result
    return result, {"workers": workers, "requests": requests, "elapsed_seconds": elapsed,
                    "entity_count": len(result.entities), "mention_count": sum(len(e["mentions"]) for e in result.entities)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=0.01)
    args = parser.parse_args()
    if not 0 <= args.delay <= 1:
        parser.error("--delay must be between 0 and 1 second")
    batches = fixture()
    single_result, single = run(batches, 1, args.delay)
    batch_result, batched = run(batches, 4, args.delay)
    assert batch_result == single_result
    reduction = 1 - batched["requests"] / (4 * 64)
    assert reduction >= 0.7
    print(json.dumps({"fixture": "4 people, 65 anchored occurrences each; no network",
                      "simulated_request_seconds": args.delay, "serial_catalog": single, "concurrent_catalog": batched,
                      "legacy_per_occurrence_requests": 4 * 64, "request_reduction": reduction,
                      "speedup": single["elapsed_seconds"] / batched["elapsed_seconds"]}, indent=2))


if __name__ == "__main__":
    main()
