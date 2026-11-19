from __future__ import annotations

import hashlib
import json
import unicodedata

ENTITY_TYPES = {"person", "place", "organization", "item", "skill", "concept"}
PEOPLE = {"person"}
ACTORS = {"person", "organization"}
SYMMETRIC = {"sibling_of", "spouse_of", "friend_of", "knows", "cooperates_with", "hostile_to"}
RELATIONS = {
    "disciple_of": (PEOPLE, PEOPLE, "师父是"),
    "parent_of": (PEOPLE, PEOPLE, "子女是"),
    "sibling_of": (PEOPLE, PEOPLE, "兄弟姐妹"),
    "spouse_of": (PEOPLE, PEOPLE, "配偶"),
    "friend_of": (PEOPLE, PEOPLE, "朋友"),
    "knows": (PEOPLE, PEOPLE, "认识"),
    "cooperates_with": (ACTORS, ACTORS, "合作"),
    "hostile_to": (ACTORS, ACTORS, "敌对"),
    "member_of": (PEOPLE, {"organization"}, "隶属"),
    "subordinate_to": (ACTORS, ACTORS, "上级是"),
    "located_in": (ENTITY_TYPES, {"place"}, "位于"),
    "possesses": (ACTORS, {"item"}, "持有"),
    "uses": (ACTORS, {"item", "skill"}, "使用"),
    "part_of": ({"place", "organization", "item"}, {"place", "organization", "item"}, "属于整体"),
}
RULE_VERSION = "2"
PARSER_VERSION = "1"


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def valid_relation(kind: str, source_type: str, target_type: str) -> bool:
    rule = RELATIONS.get(kind)
    return bool(rule and source_type in rule[0] and target_type in rule[1]
                and (kind not in {"subordinate_to", "part_of"} or source_type == target_type))


def ontology_payload() -> dict:
    return {"entity_types": sorted(ENTITY_TYPES), "relation_types": [
        {"type": key, "label": value[2], "symmetric": key in SYMMETRIC}
        for key, value in RELATIONS.items()
    ]}
