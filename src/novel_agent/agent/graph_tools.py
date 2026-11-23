from __future__ import annotations

import json
import sqlite3

from langchain_core.tools import StructuredTool

from novel_agent.corpus.knowledge_graph.models import GraphError


def bounded_payload(payload: dict, budget: int = 12000) -> str:
    def encode():
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(encode()) > budget:
        payload.update(truncated=True, truncation_reason="output_budget", search_complete=False)
        if payload.get("paths"):
            payload["paths"].pop()
            used = {e for p in payload["paths"] for e in p["edge_ids"]}
            payload["edges"] = [e for e in payload["edges"] if e["id"] in used]
        elif payload.get("edges"):
            payload["edges"].pop()
        elif payload.get("identity_evidence"):
            payload["identity_evidence"].pop()
        elif payload.get("items"):
            payload["items"].pop()
        elif payload.get("nodes"):
            payload["nodes"].pop()
        else:
            break
        if "edges" in payload:
            used_nodes = {n for e in payload["edges"] for n in (e["source"], e["target"])}
            payload["nodes"] = [n for n in payload["nodes"] if n["id"] in used_nodes]
    return encode()


def build_graph_tools(reader):
    def run(operation):
        try:
            return bounded_payload(operation())
        except (GraphError, KeyError, ValueError, sqlite3.Error):
            return json.dumps({"error": "图谱查询不可用或参数无效，请改用原文检索。", "graph_version": reader.graph_version}, ensure_ascii=False)

    def search_graph_entities(query: str, entity_type: str | None = None, top_k: int = 5,
                              start_section_id: str | None = None, end_section_id: str | None = None) -> str:
        """按名称或别名查图谱实体，返回主名称及 aliases 全部别名；重名需结合出现位置消歧，最多8条。"""
        return run(lambda: reader.search(query, entity_type, limit=min(max(top_k, 1), 8),
            start_section_id=start_section_id, end_section_id=end_section_id))

    def get_graph_entity(entity_id: str) -> str:
        """查询实体规范名称、名称类别、别名、身份依据及前8处原文提及。"""
        return run(lambda: reader.entity(entity_id, limit=8))

    def get_graph_relations(entity_id: str, depth: int = 1, entity_types: list[str] | None = None,
                             relation_types: list[str] | None = None,
                             start_section_id: str | None = None, end_section_id: str | None = None) -> str:
        """查询实体周围最多两跳的关系及逐字原文；denied为否认，character_claim为人物说法。"""
        return run(lambda: reader.neighbors(entity_id, min(max(depth, 1), 2), entity_types or [], relation_types or [],
            start_section_id, end_section_id, node_limit=20, edge_limit=20))

    def find_graph_paths(from_id: str, to_id: str, max_hops: int = 3, entity_types: list[str] | None = None,
                          relation_types: list[str] | None = None,
                          start_section_id: str | None = None, end_section_id: str | None = None) -> str:
        """查两实体间最多三跳、三条关联路径。多跳不是新事实；无路径不能证明没有关系。"""
        return run(lambda: reader.paths(from_id, to_id, min(max(max_hops, 1), 3), 3,
            entity_types or [], relation_types or [], start_section_id, end_section_id))

    return [StructuredTool.from_function(fn) for fn in (search_graph_entities, get_graph_entity, get_graph_relations, find_graph_paths)]
