"""Odys Neural Activation Layer — Phase 1 skeleton.

Overlay on top of MemoryVectorStore + vault. NOT a GNN.
- Node: memory | vault_note | project
- Edge: co_activated (weight, count, last_seen)
- activate / strengthen / decay

Store: data/odys_neurons/graph.json (+ .bak on save)
"""
from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

DATA_DIR = Path("data/odys_neurons")
GRAPH_FILE = DATA_DIR / "graph.json"
GRAPH_BAK = DATA_DIR / "graph.json.bak"

NodeType = Literal["memory", "vault_note", "project"]

# Activation weights (PRD §7)
W_BASE = 0.3
W_COSINE = 0.5
W_SPREAD = 0.2
COSINE_MIN = 0.15  # below this, ignore (noise floor)
STRENGTHEN_DELTA = 0.05
MAX_WEIGHT = 2.0
DECAY_FACTOR = 0.9
EDGE_DROP_THRESHOLD = 0.01
NODE_ARCHIVE_DAYS = 30


@dataclass
class NeuronNode:
    id: str
    type: str
    label: str
    ref: str
    base_weight: float = 0.5
    # Bag-of-words tokens for lightweight similarity (no embed model required in Phase 1)
    tokens: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    last_activated_at: str | None = None
    archived: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NeuronNode":
        return cls(
            id=d["id"],
            type=d.get("type", "memory"),
            label=d.get("label", ""),
            ref=d.get("ref", ""),
            base_weight=float(d.get("base_weight", 0.5)),
            tokens=list(d.get("tokens") or []),
            created_at=d.get("created_at") or "",
            updated_at=d.get("updated_at") or "",
            last_activated_at=d.get("last_activated_at"),
            archived=bool(d.get("archived", False)),
        )


@dataclass
class NeuronEdge:
    source: str
    target: str
    weight: float = 0.1
    count: int = 1
    last_seen: str = ""

    def key(self) -> str:
        a, b = sorted([self.source, self.target])
        return f"{a}:{b}"

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "weight": self.weight,
            "count": self.count,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NeuronEdge":
        return cls(
            source=d["source"],
            target=d["target"],
            weight=float(d.get("weight", 0.1)),
            count=int(d.get("count", 1)),
            last_seen=d.get("last_seen") or "",
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _tokenize(text: str) -> list[str]:
    """Simple lowercase word tokens (Phase 1 — no embed model)."""
    if not text:
        return []
    words = re.findall(r"[a-zA-Z0-9_\-]{2,}", text.lower())
    # drop ultra-common noise
    stop = {"the", "and", "for", "with", "from", "that", "this", "ada", "yang", "dan", "untuk"}
    return [w for w in words if w not in stop]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _edge_key(a: str, b: str) -> str:
    x, y = sorted([a, b])
    return f"{x}:{y}"


class NeuronGraph:
    def __init__(self):
        self.nodes: dict[str, NeuronNode] = {}
        self.edges: dict[str, NeuronEdge] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        if GRAPH_FILE.exists():
            try:
                raw = json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
                self.nodes = {
                    nid: NeuronNode.from_dict(nd)
                    for nid, nd in (raw.get("nodes") or {}).items()
                }
                self.edges = {}
                for ek, ed in (raw.get("edges") or {}).items():
                    e = NeuronEdge.from_dict(ed)
                    self.edges[e.key()] = e
            except Exception:
                self.nodes = {}
                self.edges = {}
        self._loaded = True

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": _now(),
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
            "edges": {ek: e.to_dict() for ek, e in self.edges.items()},
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        # backup previous
        if GRAPH_FILE.exists():
            try:
                GRAPH_BAK.write_text(GRAPH_FILE.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        GRAPH_FILE.write_text(text, encoding="utf-8")

    def add_node(
        self,
        *,
        type: NodeType,
        label: str,
        ref: str,
        base_weight: float = 0.5,
        node_id: str | None = None,
        text: str | None = None,
    ) -> NeuronNode:
        self.load()
        # upsert by type+ref
        for n in self.nodes.values():
            if n.type == type and n.ref == ref and not n.archived:
                n.label = label or n.label
                n.base_weight = max(n.base_weight, base_weight)
                n.tokens = _tokenize(text or label) or n.tokens
                n.updated_at = _now()
                self.save()
                return n

        nid = node_id or str(uuid.uuid4())[:12]
        now = _now()
        node = NeuronNode(
            id=nid,
            type=type,
            label=label,
            ref=ref,
            base_weight=min(max(base_weight, 0.0), 1.0),
            tokens=_tokenize(text or label),
            created_at=now,
            updated_at=now,
        )
        self.nodes[nid] = node
        self.save()
        return node

    def get_node(self, node_id: str) -> NeuronNode | None:
        self.load()
        return self.nodes.get(node_id)

    def list_nodes(self, include_archived: bool = False) -> list[NeuronNode]:
        self.load()
        return [
            n for n in self.nodes.values()
            if include_archived or not n.archived
        ]

    def strengthen(self, ids: list[str]) -> dict[str, Any]:
        """Hebbian-ish: co-activate all pairs in ids."""
        self.load()
        ids = [i for i in ids if i in self.nodes and not self.nodes[i].archived]
        if len(ids) < 1:
            return {"ok": False, "message": "no valid node ids", "updated_edges": 0}

        now = _now()
        updated = 0
        # mark activation
        for nid in ids:
            self.nodes[nid].last_activated_at = now
            self.nodes[nid].updated_at = now

        # pairwise strengthen
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                key = _edge_key(a, b)
                if key in self.edges:
                    e = self.edges[key]
                    e.weight = min(e.weight + STRENGTHEN_DELTA, MAX_WEIGHT)
                    e.count += 1
                    e.last_seen = now
                else:
                    e = NeuronEdge(source=a, target=b, weight=STRENGTHEN_DELTA, count=1, last_seen=now)
                    self.edges[key] = e
                updated += 1

        self.save()
        return {"ok": True, "message": f"strengthened {len(ids)} nodes", "updated_edges": updated, "ids": ids}

    def decay(self) -> dict[str, Any]:
        """All edges *= DECAY_FACTOR; drop weak edges; archive stale isolated nodes."""
        self.load()
        now = _now()
        dropped_edges = 0
        new_edges: dict[str, NeuronEdge] = {}
        for key, e in self.edges.items():
            e.weight *= DECAY_FACTOR
            if e.weight < EDGE_DROP_THRESHOLD:
                dropped_edges += 1
                continue
            new_edges[key] = e
        self.edges = new_edges

        # archive nodes with no edges and old last_activated
        archived = 0
        for n in self.nodes.values():
            if n.archived:
                continue
            has_edge = any(n.id in (e.source, e.target) for e in self.edges.values())
            if has_edge:
                continue
            last = n.last_activated_at or n.created_at
            if not last:
                continue
            try:
                # rough day diff from ISO prefix
                t0 = time.mktime(time.strptime(last[:19], "%Y-%m-%dT%H:%M:%S"))
                age_days = (time.time() - t0) / 86400
                if age_days >= NODE_ARCHIVE_DAYS:
                    n.archived = True
                    n.updated_at = now
                    archived += 1
            except Exception:
                pass

        self.save()
        return {
            "ok": True,
            "message": f"decayed edges; dropped {dropped_edges}; archived {archived}",
            "dropped_edges": dropped_edges,
            "archived_nodes": archived,
            "edge_count": len(self.edges),
            "node_count": sum(1 for n in self.nodes.values() if not n.archived),
        }

    def activate(
        self,
        query: str = "",
        node_ids: list[str] | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        """Return top-K active nodes with scores + explanation."""
        self.load()
        active_nodes = [n for n in self.nodes.values() if not n.archived]
        if not active_nodes:
            return {
                "ok": True,
                "mode": "empty",
                "results": [],
                "message": "no nodes — cold start",
            }

        # Cold start fallback flag
        if len(active_nodes) < 20:
            mode = "vector_only_cold_start"
        else:
            mode = "neural"

        # Seed set
        if node_ids:
            seeds = {i for i in node_ids if i in self.nodes and not self.nodes[i].archived}
        else:
            seeds = set()

        q_tokens = set(_tokenize(query)) if query else set()

        # adjacency for spread
        adj: dict[str, list[tuple[str, float]]] = {n.id: [] for n in active_nodes}
        for e in self.edges.values():
            if e.source in adj and e.target in adj:
                adj[e.source].append((e.target, e.weight))
                adj[e.target].append((e.source, e.weight))

        scored: list[dict[str, Any]] = []
        for n in active_nodes:
            # cosine-like: jaccard on tokens (Phase 1 proxy)
            n_set = set(n.tokens)
            sim = _jaccard(q_tokens, n_set) if q_tokens else 0.0
            if seeds and n.id in seeds:
                sim = max(sim, 0.8)

            if sim < COSINE_MIN and n.id not in seeds and not q_tokens:
                # pure structural: base only if no query
                sim = 0.0

            # spread from neighbors that are similar
            spread = 0.0
            if adj.get(n.id):
                for nb_id, w in adj[n.id]:
                    nb_node = self.nodes.get(nb_id)
                    if not nb_node or nb_node.archived:
                        continue
                    nb_sim = _jaccard(q_tokens, set(nb_node.tokens)) if q_tokens else 0.0
                    if nb_id in seeds:
                        nb_sim = max(nb_sim, 0.6)
                    if nb_sim >= COSINE_MIN or nb_id in seeds:
                        spread += w * max(nb_sim, 0.3)
                # normalize rough
                spread = min(spread / max(len(adj[n.id]), 1), 1.0)

            if sim < COSINE_MIN and spread < 0.05 and n.id not in seeds:
                continue

            score = (
                n.base_weight * W_BASE
                + sim * W_COSINE
                + spread * W_SPREAD
            )
            scored.append({
                "id": n.id,
                "type": n.type,
                "label": n.label,
                "ref": n.ref,
                "score": round(score, 4),
                "sim": round(sim, 4),
                "spread": round(spread, 4),
                "base_weight": n.base_weight,
                "why": _explain(n, sim, spread, n.id in seeds),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[: max(1, min(top_k, 50))]

        # mark last_activated on top results (soft — no strengthen yet)
        now = _now()
        for r in top:
            node = self.nodes.get(r["id"])
            if node:
                node.last_activated_at = now
        if top:
            self.save()

        return {
            "ok": True,
            "mode": mode,
            "query": query,
            "result_count": len(top),
            "results": top,
            "stats": self.status()["stats"],
        }

    def status(self) -> dict[str, Any]:
        self.load()
        nodes = list(self.nodes.values())
        active = [n for n in nodes if not n.archived]
        by_type: dict[str, int] = {}
        for n in active:
            by_type[n.type] = by_type.get(n.type, 0) + 1
        return {
            "ok": True,
            "path": str(GRAPH_FILE.resolve()) if GRAPH_FILE.exists() else str(GRAPH_FILE),
            "stats": {
                "node_count": len(active),
                "archived_count": sum(1 for n in nodes if n.archived),
                "edge_count": len(self.edges),
                "by_type": by_type,
                "cold_start": len(active) < 20,
            },
        }


def _explain(n: NeuronNode, sim: float, spread: float, is_seed: bool) -> str:
    parts = []
    if is_seed:
        parts.append("seed")
    if sim >= 0.4:
        parts.append(f"text-match {sim:.2f}")
    elif sim >= COSINE_MIN:
        parts.append(f"weak-match {sim:.2f}")
    if spread >= 0.1:
        parts.append(f"spread {spread:.2f}")
    if n.base_weight >= 0.8:
        parts.append("high base")
    if not parts:
        parts.append("base only")
    return ", ".join(parts)


# ── Module-level singleton ───────────────────────────────

_graph: NeuronGraph | None = None


def get_graph() -> NeuronGraph:
    global _graph
    if _graph is None:
        _graph = NeuronGraph()
        _graph.load()
    return _graph


def add_node(**kwargs) -> dict:
    g = get_graph()
    n = g.add_node(**kwargs)
    return {"ok": True, "node": n.to_dict()}


def strengthen(ids: list[str]) -> dict:
    return get_graph().strengthen(ids)


def decay() -> dict:
    return get_graph().decay()


def activate(query: str = "", node_ids: list[str] | None = None, top_k: int = 10) -> dict:
    return get_graph().activate(query=query, node_ids=node_ids, top_k=top_k)


def status() -> dict:
    return get_graph().status()


def list_nodes(include_archived: bool = False) -> dict:
    nodes = get_graph().list_nodes(include_archived=include_archived)
    return {"ok": True, "nodes": [n.to_dict() for n in nodes]}
