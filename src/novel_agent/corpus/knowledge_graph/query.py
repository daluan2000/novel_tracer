from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from novel_agent.corpus.repository import NovelCorpus
from .models import GraphError
from .ontology import ENTITY_TYPES, RELATIONS, normalize
from .storage import GraphStorage


@dataclass(frozen=True)
class GraphReadSession:
    storage: GraphStorage
    corpus: NovelCorpus
    novel_id: str
    graph_version: str

    def scope(self, start_section_id=None, end_section_id=None, entity_types=(), relation_types=()):
        sections = {s.section_id: i for i, s in enumerate(self.corpus.document.sections)}
        if (start_section_id is not None and start_section_id not in sections
                or end_section_id is not None and end_section_id not in sections):
            raise GraphError("invalid_chapter", "章节不存在或不属于当前小说。", 400)
        start = sections[start_section_id] if start_section_id is not None else 0
        end = sections[end_section_id] if end_section_id is not None else len(sections) - 1
        if start > end:
            raise GraphError("invalid_chapter", "起始章节不能晚于结束章节。", 400)
        if set(entity_types) - ENTITY_TYPES or set(relation_types) - RELATIONS.keys():
            raise GraphError("invalid_type", "无效的实体或关系类型。", 422)
        return start, end

    def meta(self, reason=None):
        return {"graph_version": self.graph_version, "truncated": bool(reason),
                "truncation_reason": reason, "search_complete": reason is None}

    def _check(self, db):
        row = db.execute("SELECT state FROM graph_versions WHERE novel_id=? AND version=?", (self.novel_id, self.graph_version)).fetchone()
        if not row or row[0] not in {"published", "superseded"}:
            raise GraphError("graph_not_ready", "图谱不可用，请构建或重建。")

    def _node(self, row):
        return {"id": row["entity_id"], "name": row["name"], "type": row["type"], **json.loads(row["payload"])}

    def search(self, query: str, entity_type=None, offset=0, limit=20, start_section_id=None, end_section_id=None):
        q = normalize(query)
        if not 1 <= len(q) <= 200 or not 0 <= offset or not 1 <= limit <= 100:
            raise GraphError("invalid_query", "搜索词或分页参数无效。", 422)
        start, end = self.scope(start_section_id, end_section_id, [entity_type] if entity_type else [])
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.storage.connect() as db:
            self._check(db)
            rows = db.execute("""SELECT e.*, MIN(CASE WHEN e.normalized_name=? THEN 0
                WHEN a.normalized_alias=? THEN 1 WHEN e.normalized_name LIKE ? ESCAPE '\\' THEN 2 ELSE 3 END) AS rank
                FROM graph_entities e JOIN graph_aliases a ON a.version=e.version AND a.entity_id=e.entity_id
                WHERE e.version=? AND (? IS NULL OR e.type=?) AND a.normalized_alias LIKE ? ESCAPE '\\'
                AND EXISTS (SELECT 1 FROM graph_mentions m WHERE m.version=e.version AND m.entity_id=e.entity_id AND m.section_order BETWEEN ? AND ?)
                GROUP BY e.entity_id ORDER BY rank,e.first_section_order,e.entity_id""",
                (q, q, escaped + "%", self.graph_version, entity_type, entity_type, "%" + escaped + "%", start, end)).fetchall()
            items = []
            for row in rows[offset:offset + limit]:
                mention = db.execute("SELECT payload FROM graph_mentions WHERE version=? AND entity_id=? AND section_order BETWEEN ? AND ? ORDER BY start_char LIMIT 1", (self.graph_version, row["entity_id"], start, end)).fetchone()
                items.append({**self._node(row), "match_kind": ["name_exact", "alias_exact", "prefix", "contains"][row["rank"]], "first_mention": json.loads(mention[0])})
        return {**self.meta(), "items": items, "offset": offset, "limit": limit, "total": len(rows)}

    def entity(self, entity_id: str, offset=0, limit=20, start_section_id=None, end_section_id=None):
        start, end = self.scope(start_section_id, end_section_id)
        with self.storage.connect() as db:
            self._check(db)
            row = db.execute("SELECT * FROM graph_entities WHERE version=? AND entity_id=?", (self.graph_version, entity_id)).fetchone()
            if not row:
                raise GraphError("graph_object_not_found", "实体不存在或不属于当前小说。", 404)
            params = (self.graph_version, entity_id, start, end)
            total = db.execute("SELECT COUNT(*) FROM graph_mentions WHERE version=? AND entity_id=? AND section_order BETWEEN ? AND ?", params).fetchone()[0]
            mentions = db.execute("SELECT payload FROM graph_mentions WHERE version=? AND entity_id=? AND section_order BETWEEN ? AND ? ORDER BY start_char LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
            return {**self.meta(), "entity": self._node(row), "items": [json.loads(m[0]) for m in mentions], "offset": offset, "limit": limit, "total": total}

    def _groups(self, db, relation_id, start, end, offset=0, limit=1):
        params = (self.graph_version, relation_id, start, end)
        total = db.execute("SELECT COUNT(*) FROM graph_evidence_groups WHERE version=? AND relation_id=? AND first_section>=? AND last_section<=?", params).fetchone()[0]
        groups = db.execute("SELECT group_id FROM graph_evidence_groups WHERE version=? AND relation_id=? AND first_section>=? AND last_section<=? ORDER BY first_section,group_id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return total, [{"id": g[0], "spans": [json.loads(s[0]) for s in db.execute("SELECT payload FROM graph_evidence_spans WHERE version=? AND group_id=? ORDER BY span_index", (self.graph_version, g[0]))]} for g in groups]

    def evidence(self, relation_id, offset=0, limit=20, start_section_id=None, end_section_id=None):
        start, end = self.scope(start_section_id, end_section_id)
        with self.storage.connect() as db:
            self._check(db)
            if not db.execute("SELECT 1 FROM graph_relations WHERE version=? AND relation_id=?", (self.graph_version, relation_id)).fetchone():
                raise GraphError("graph_object_not_found", "关系不存在或不属于当前小说。", 404)
            total, groups = self._groups(db, relation_id, start, end, offset, limit)
        return {**self.meta(), "items": groups, "offset": offset, "limit": limit, "total": total}

    def _graph(self, roots, entity_types, relation_types, start, end, deadline, node_limit=20000, edge_limit=50000):
        # SQLite is the bounded-memory fallback; no full-graph cache is required for correctness.
        with self.storage.connect() as db:
            self._check(db)
            root_rows = []
            for root, _ in roots:
                row = db.execute("SELECT * FROM graph_entities WHERE version=? AND entity_id=?", (self.graph_version, root)).fetchone()
                if not row:
                    raise GraphError("graph_object_not_found", "实体不存在或不属于当前小说。", 404)
                root_rows.append(row)
            nodes = {r["entity_id"]: self._node(r) for r in root_rows}
            adjacent = defaultdict(list)
            edges = {}
            queue, expanded = deque(roots), set()
            checked, reason = 0, None
            # Fetch adjacency lazily, applying source/type/evidence scope in SQL before traversal.
            while queue:
                if time.monotonic() > deadline or len(expanded) >= 20000 or checked >= 50000:
                    reason = "search_budget"
                    break
                current, depth = queue.popleft()
                if current in expanded or depth == 0:
                    continue
                expanded.add(current)
                clauses = ["r.version=?",
                           "EXISTS (SELECT 1 FROM graph_evidence_groups g WHERE g.version=r.version AND g.relation_id=r.relation_id AND g.first_section>=? AND g.last_section<=?)"]
                args = [self.graph_version, start, end]
                if relation_types:
                    clauses.append("r.relation_type IN (" + ",".join("?" for _ in relation_types) + ")")
                    args.extend(relation_types)
                rows = []
                for side, index in (("source_id", "graph_sources"), ("target_id", "graph_targets")):
                    rows.extend(db.execute(f"SELECT r.* FROM graph_relations r INDEXED BY {index} WHERE "
                        + " AND ".join([*clauses, f"r.{side}=?"]) + " ORDER BY r.relation_id LIMIT ?",
                        (*args, current, 50001 - checked)).fetchall())
                rows.sort(key=lambda r: r["relation_id"])
                for row in rows:
                    checked += 1
                    if checked > 50000 or time.monotonic() > deadline:
                        reason = "search_budget"
                        break
                    other = row["target_id"] if row["source_id"] == current else row["source_id"]
                    if row["relation_id"] not in edges and len(edges) >= edge_limit:
                        reason = "edge_limit" if edge_limit < 50000 else "search_budget"
                        break
                    if other not in nodes:
                        other_row = db.execute("SELECT * FROM graph_entities WHERE version=? AND entity_id=?", (self.graph_version, other)).fetchone()
                        if other not in {r for r, _ in roots} and entity_types and other_row["type"] not in entity_types:
                            continue
                        if len(nodes) >= node_limit:
                            reason = "node_limit" if node_limit < 20000 else "search_budget"
                            break
                        nodes[other] = self._node(other_row)
                    rid = row["relation_id"]
                    if rid not in edges:
                        edges[rid] = {"id": rid, "source": row["source_id"], "target": row["target_id"], "type": row["relation_type"], "polarity": row["polarity"], "statement_mode": row["statement_mode"]}
                        adjacent[row["source_id"]].append((row["target_id"], rid))
                        adjacent[row["target_id"]].append((row["source_id"], rid))
                    queue.append((other, depth - 1))
                if reason:
                    break
            return nodes, edges, adjacent, reason

    def _result(self, nodes, edges, paths, start, end, reason):
        with self.storage.connect() as db:
            for edge in edges.values():
                total, groups = self._groups(db, edge["id"], start, end)
                edge.update(evidence_total=total, evidence_groups=groups)
        return {**self.meta(reason), "nodes": list(nodes.values()), "edges": list(edges.values()), "paths": paths}

    def neighbors(self, entity_id, depth=1, entity_types=(), relation_types=(), start_section_id=None, end_section_id=None, node_limit=100, edge_limit=200):
        if not 1 <= depth <= 3:
            raise GraphError("invalid_depth", "跳数必须为 1 至 3。", 422)
        start, end = self.scope(start_section_id, end_section_id, entity_types, relation_types)
        nodes, edges, adjacency, reason = self._graph([(entity_id, depth)], entity_types, relation_types, start, end,
            time.monotonic() + 2, node_limit, edge_limit)
        selected_nodes, selected_edges = {entity_id: nodes[entity_id]}, {}
        queue, visited = deque([(entity_id, 0)]), {entity_id}
        while queue:
            node, level = queue.popleft()
            if level >= depth:
                continue
            for other, rid in sorted(adjacency[node]):
                if rid in selected_edges:
                    continue
                if len(selected_edges) >= edge_limit:
                    reason = reason or "edge_limit"
                    break
                if other not in selected_nodes and len(selected_nodes) >= node_limit:
                    reason = reason or "node_limit"
                    continue
                selected_edges[rid] = edges[rid]
                selected_nodes[other] = nodes[other]
                if other not in visited:
                    visited.add(other)
                    queue.append((other, level + 1))
        return self._result(selected_nodes, selected_edges, [], start, end, reason)

    def paths(self, from_id, to_id, max_hops=3, limit=5, entity_types=(), relation_types=(), start_section_id=None, end_section_id=None):
        if not 1 <= max_hops <= 3 or not 1 <= limit <= 5:
            raise GraphError("invalid_depth", "路径最多三跳、五条。", 422)
        start, end = self.scope(start_section_id, end_section_id, entity_types, relation_types)
        deadline = time.monotonic() + 2
        nodes, edges, adjacent, reason = self._graph([(from_id, max_hops), (to_id, 0)], entity_types, relation_types, start, end, deadline)
        queue, found, shortest, checked = deque([([from_id], [])]), [], None, 0
        while queue:
            node_ids, edge_ids = queue.popleft()
            checked += 1
            if checked > 20000 or time.monotonic() > deadline:
                reason = reason or "search_budget"
                break
            if shortest is not None and len(edge_ids) > shortest:
                break
            if node_ids[-1] == to_id:
                if len(found) == limit:
                    reason = reason or "path_limit"
                    break
                found.append({"node_ids": node_ids, "edge_ids": edge_ids})
                shortest = len(edge_ids)
                continue
            if len(edge_ids) >= max_hops or shortest is not None:
                continue
            for other, rid in sorted(adjacent[node_ids[-1]]):
                if other not in node_ids and edges[rid]["polarity"] == "affirmed":
                    if len(queue) + checked >= 20000:
                        reason = reason or "search_budget"
                        break
                    queue.append(([*node_ids, other], [*edge_ids, rid]))
        used_nodes = {from_id, to_id} | {n for p in found for n in p["node_ids"]}
        used_edges = {e for p in found for e in p["edge_ids"]}
        return self._result({n: nodes[n] for n in sorted(used_nodes)}, {e: edges[e] for e in sorted(used_edges)}, found, start, end, reason)
