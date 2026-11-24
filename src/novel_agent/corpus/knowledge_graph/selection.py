"""Select graph candidates before paid resolution, then recheck resolved aliases."""
from __future__ import annotations

from collections import Counter, defaultdict

from .identity import GENERIC_NAMES, explicit_identity
from .ontology import normalize


FILTERED_TYPES = {"place", "item", "skill", "concept"}


def select_candidates(batches: list[dict], minimum: int) -> tuple[list[dict], dict]:
    mentions, important = defaultdict(set), set()
    before, after, reasons = Counter(), Counter(), Counter()
    for batch in batches:
        for entity in batch["entities"]:
            key = (entity["type"], normalize(entity["name"]))
            before[entity["type"]] += 1
            mentions[key].update((m["start_char"], m["end_char"]) for m in entity["mentions"])
            if entity.get("plot_relevant") and entity.get("importance_evidence"):
                important.add(key)
    selected, accepted_links, protected_names = [], {}, set()
    for batch in batches:
        entities = {e["local_id"]: e for e in batch["entities"]}
        links = []
        for link in batch["identity_links"]:
            source, target = entities[link["source"]], entities[link["target"]]
            if explicit_identity(source["name"], target["name"], link["evidence"]):
                links.append(link)
                protected_names.update((e["type"], normalize(e["name"])) for e in (source, target))
            else:
                reasons["identity_evidence"] += 1
        accepted_links[batch["unit_id"]] = links
    for batch in batches:
        entities = {e["local_id"]: e for e in batch["entities"]}
        retained = []
        for entity in entities.values():
            key = (entity["type"], normalize(entity["name"]))
            if entity["name"] in GENERIC_NAMES:
                reasons["generic_name"] += 1
            elif (entity["type"] not in FILTERED_TYPES or len(mentions[key]) >= minimum
                  or key in important or key in protected_names):
                retained.append(entity)
                after[entity["type"]] += 1
            else:
                reasons["low_frequency_background"] += 1
        ids = {e["local_id"] for e in retained}
        selected.append({**batch, "entities": retained,
                         "relations": [r for r in batch["relations"] if r["source"] in ids and r["target"] in ids],
                         "identity_links": [l for l in accepted_links[batch["unit_id"]] if l["source"] in ids and l["target"] in ids]})
    return selected, {"candidates_before": dict(before), "candidates_after": dict(after),
                      "filtered_reasons": dict(reasons), "minimum_mentions": minimum}


def select_resolved(entities: list[dict], relations: list[dict], minimum: int):
    def mention_count(entity):
        # Full names and their substrings at the same source occurrence count once.
        end, count = -1, 0
        for mention in sorted(entity["mentions"], key=lambda m: (m["start_char"], -m["end_char"])):
            if mention["start_char"] >= end:
                count += 1
            end = max(end, mention["end_char"])
        return count
    retained = [e for e in entities if e["type"] not in FILTERED_TYPES
                or mention_count(e) >= minimum or e.get("plot_relevant")]
    ids = {e["id"] for e in retained}
    return retained, [r for r in relations if r["source"] in ids and r["target"] in ids]
