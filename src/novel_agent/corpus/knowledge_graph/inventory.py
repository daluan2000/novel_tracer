"""Immutable source mentions and fact candidates, independent of graph identities."""
from __future__ import annotations

from dataclasses import dataclass

from .ontology import digest


@dataclass
class Inventory:
    mentions: dict[str, dict]
    facts: list[dict]
    identity_hints: list[dict]


def collect_inventory(batches: list[dict]) -> Inventory:
    mentions, local, facts, hints = {}, {}, {}, {}
    for batch in sorted(batches, key=lambda b: b["unit_id"]):
        for entry in batch["entities"]:
            ids = []
            for anchor in entry["mentions"]:
                mid = f"m{anchor['start_char']}_{anchor['end_char']}_{entry['type']}"
                mention = mentions.setdefault(mid, {"mention_id": mid, "surface": entry["name"],
                    "type": entry["type"], "anchor": anchor, "contexts": [], "plot_relevant": False,
                    "importance_evidence": [], "descriptions": []})
                if mention["surface"] != anchor["quote"]:
                    raise ValueError("mention_surface_mismatch")
                mention["contexts"].extend(entry.get("contexts", [anchor]))
                mention["contexts"] = list({(c["start_char"], c["end_char"], c["quote"]): c
                                            for c in mention["contexts"]}.values())
                if entry.get("description"):
                    mention["descriptions"] = sorted(set([*mention["descriptions"], entry["description"]]))
                if entry.get("plot_relevant") and entry.get("importance_evidence"):
                    mention["plot_relevant"] = True
                    mention["importance_evidence"].extend(entry["importance_evidence"])
                ids.append(mid)
            local[(batch["unit_id"], entry["local_id"])] = sorted(set(ids))
    for batch in sorted(batches, key=lambda b: b["unit_id"]):
        for relation in batch["relations"]:
            source = local[(batch["unit_id"], relation["source"])]
            target = local[(batch["unit_id"], relation["target"])]
            # The new extraction contract uses one occurrence per local_id.
            # Older multi-anchor outputs are retained as alternatives, never
            # resolved by arbitrarily choosing their first occurrence.
            payload = {**relation, "source_mentions": source, "target_mentions": target}
            payload.pop("source")
            payload.pop("target")
            fid = digest([source, target, relation["type"], relation["polarity"], relation["statement_mode"],
                          [(e["start_char"], e["end_char"]) for e in relation["evidence"]]])
            facts[fid] = {"candidate_id": fid, **payload}
        for link in batch["identity_links"]:
            source = local[(batch["unit_id"], link["source"])]
            target = local[(batch["unit_id"], link["target"])]
            hid = digest([source, target, [(e["start_char"], e["end_char"]) for e in link["evidence"]]])
            hints[hid] = {"source_mentions": source, "target_mentions": target, "evidence": link["evidence"]}
    return Inventory(dict(sorted(mentions.items(), key=lambda item: (
        item[1]["anchor"]["start_char"], item[1]["anchor"]["end_char"], item[0]))),
        list(facts.values()), list(hints.values()))
