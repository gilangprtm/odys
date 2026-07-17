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
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

# TF-IDF cosine similarity (Phase 2 — upgrade from bag-of-words Jaccard)
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity as _cosine
    import numpy as np
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False

DATA_DIR = Path("data/odys_neurons")
GRAPH_FILE = DATA_DIR / "graph.json"
GRAPH_BAK = DATA_DIR / "graph.json.bak"

NodeType = Literal["memory", "vault_note", "project", "session"]

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
    """Simple lowercase word tokens (Phase 2 — TF-IDF backend)."""
    if not text:
        return []
    words = re.findall(r"[a-zA-Z0-9_\-]{2,}", text.lower())
    # drop ultra-common noise
    stop = {"the", "and", "for", "with", "from", "that", "this", "ada", "yang", "dan", "untuk"}
    return [w for w in words if w not in stop]


# ── TF-IDF Similarity Cache (Phase 2) ──────────────────────────────────────
class _TfidfCache:
    """Lazy-built TF-IDF matrix over all node tokens. Rebuilt on save() or explicit refresh."""
    def __init__(self):
        self._vectorizer: TfidfVectorizer | None = None
        self._matrix = None  # sparse CSR matrix
        self._node_ids: list[str] = []
        self._dirty = True
        self._lock = threading.Lock()

    def mark_dirty(self):
        self._dirty = True

    def _rebuild(self, nodes: dict[str, NeuronNode]) -> None:
        if not _HAS_SKLEARN:
            return
        active = [(nid, n) for nid, n in nodes.items() if not n.archived and n.tokens]
        if not active:
            self._node_ids = []
            self._matrix = None
            self._dirty = False
            return
        self._node_ids = [nid for nid, _ in active]
        corpus = [" ".join(n.tokens) for _, n in active]
        self._vectorizer = TfidfVectorizer(
            max_features=2000,
            ngram_range=(1, 2),
            sublinear_tf=True,
        )
        self._matrix = self._vectorizer.fit_transform(corpus)
        self._dirty = False

    def query_similarity(self, query: str, nodes: dict[str, NeuronNode]) -> dict[str, float]:
        """Return {node_id: tfidf_score} for all active nodes."""
        if not _HAS_SKLEARN or not query.strip():
            return {}
        with self._lock:
            if self._dirty or self._matrix is None:
                self._rebuild(nodes)
            if self._matrix is None or self._vectorizer is None:
                return {}
            try:
                q_vec = self._vectorizer.transform([" ".join(_tokenize(query))])
                sims = _cosine(q_vec, self._matrix).flatten()
                return {self._node_ids[i]: float(sims[i]) for i in range(len(self._node_ids)) if sims[i] > 0.01}
            except Exception:
                return {}

_tfidf = _TfidfCache()


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _token_sim(query: set[str], doc: set[str]) -> float:
    """Fallback Jaccard similarity (when sklearn unavailable)."""
    if not query or not doc:
        return 0.0
    inter = len(query & doc)
    if inter == 0:
        return 0.0
    j = inter / len(query | doc)
    coverage = inter / len(query)
    return max(j, coverage * 0.85)


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
        try:
            from services.odys_neuron_db import init_db, migrate_from_json, load_all
            init_db()
            migrate_from_json()
            nodes_raw, edges_raw = load_all()
            self.nodes = {nid: NeuronNode.from_dict(nd) for nid, nd in nodes_raw.items()}
            self.edges = {}
            for ek, ed in edges_raw.items():
                e = NeuronEdge.from_dict(ed)
                self.edges[e.key()] = e
        except Exception:
            # Fallback to JSON if SQLite unavailable
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
        nodes = {nid: n.to_dict() for nid, n in self.nodes.items()}
        edges = {ek: e.to_dict() for ek, e in self.edges.items()}
        try:
            from services.odys_neuron_db import save_all
            save_all(nodes, edges)
        except Exception:
            # Fallback to JSON
            payload = {
                "version": 2,
                "saved_at": _now(),
                "nodes": nodes,
                "edges": edges,
            }
            text = json.dumps(payload, indent=2, ensure_ascii=False)
            if GRAPH_FILE.exists():
                try:
                    GRAPH_BAK.write_text(GRAPH_FILE.read_text(encoding="utf-8"), encoding="utf-8")
                except OSError:
                    pass
            GRAPH_FILE.write_text(text, encoding="utf-8")
        _tfidf.mark_dirty()

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

    def delete_node(self, node_id: str) -> dict[str, Any]:
        """Delete a node and all its connected edges. Permanent, not archive."""
        self.load()
        if node_id not in self.nodes:
            return {"ok": False, "message": f"node {node_id} not found"}
        deleted_edges = 0
        remaining = {}
        for key, e in self.edges.items():
            if e.source == node_id or e.target == node_id:
                deleted_edges += 1
            else:
                remaining[key] = e
        self.edges = remaining
        del self.nodes[node_id]
        self.save()
        return {"ok": True, "deleted_node": node_id, "deleted_edges": deleted_edges}

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

        # TF-IDF similarity (preferred) or fallback to Jaccard
        tfidf_scores: dict[str, float] = {}
        if query and _HAS_SKLEARN:
            tfidf_scores = _tfidf.query_similarity(query, self.nodes)

        # adjacency for spread
        adj: dict[str, list[tuple[str, float]]] = {n.id: [] for n in active_nodes}
        for e in self.edges.values():
            if e.source in adj and e.target in adj:
                adj[e.source].append((e.target, e.weight))
                adj[e.target].append((e.source, e.weight))

        scored: list[dict[str, Any]] = []
        for n in active_nodes:
            # TF-IDF cosine similarity (preferred) or Jaccard fallback
            if tfidf_scores:
                sim = tfidf_scores.get(n.id, 0.0)
            else:
                n_set = set(n.tokens)
                sim = _token_sim(q_tokens, n_set) if q_tokens else 0.0
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
                    # TF-IDF or Jaccard for neighbor similarity
                    if tfidf_scores:
                        nb_sim = tfidf_scores.get(nb_id, 0.0)
                    else:
                        nb_sim = _token_sim(q_tokens, set(nb_node.tokens)) if q_tokens else 0.0
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
        # Use SQLite stats if available (faster than iterating all nodes)
        try:
            from services.odys_neuron_db import stats as db_stats
            s = db_stats()
            s["cold_start"] = s["node_count"] < 20
            return {
                "ok": True,
                "path": str(DB_FILE.resolve()) if DB_FILE.exists() else str(GRAPH_FILE),
                "storage": "sqlite" if DB_FILE.exists() else "json",
                "stats": s,
            }
        except Exception:
            pass
        # Fallback to in-memory stats
        nodes = list(self.nodes.values())
        active = [n for n in nodes if not n.archived]
        by_type: dict[str, int] = {}
        for n in active:
            by_type[n.type] = by_type.get(n.type, 0) + 1
        return {
            "ok": True,
            "path": str(GRAPH_FILE.resolve()) if GRAPH_FILE.exists() else str(GRAPH_FILE),
            "storage": "json",
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


def delete_node(node_id: str) -> dict:
    return get_graph().delete_node(node_id)


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
