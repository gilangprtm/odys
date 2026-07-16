"""Odys Neuron hooks — Phase 2.

Bridge chat memory retrieve + memory extract → neuron graph.
Fail-soft: never raise into chat path.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


def _safe_import():
    try:
        from services import odys_neuron_service as neurons
        return neurons
    except Exception as e:
        logger.debug("neuron service unavailable: %s", e)
        return None


def ensure_memory_node(
    memory_id: str,
    text: str,
    *,
    category: str = "fact",
    pinned: bool = False,
) -> Optional[str]:
    """Upsert a memory fact as a neuron node. Returns neuron node id or None."""
    neurons = _safe_import()
    if not neurons or not memory_id or not (text or "").strip():
        return None
    try:
        base = 0.9 if pinned or category == "identity" else 0.5
        label = (text or "").strip()[:60]
        r = neurons.add_node(
            type="memory",
            label=label,
            ref=str(memory_id),
            base_weight=base,
            text=text,
            node_id=None,  # upsert by type+ref
        )
        return (r.get("node") or {}).get("id")
    except Exception as e:
        logger.warning("neuron ensure_memory_node failed: %s", e)
        return None


def ensure_project_node(project_id: str, label: str = "", text: str = "") -> Optional[str]:
    neurons = _safe_import()
    if not neurons or not project_id:
        return None
    try:
        r = neurons.add_node(
            type="project",
            label=label or project_id,
            ref=str(project_id),
            base_weight=0.7,
            text=text or label or project_id,
        )
        return (r.get("node") or {}).get("id")
    except Exception as e:
        logger.warning("neuron ensure_project_node failed: %s", e)
        return None


def _node_ids_for_memory_refs(memory_ids: Iterable[str]) -> list[str]:
    """Resolve memory entry ids → neuron node ids via ref match."""
    neurons = _safe_import()
    if not neurons:
        return []
    try:
        g = neurons.get_graph()
        g.load()
        ref_set = {str(m) for m in memory_ids if m}
        out = []
        for n in g.nodes.values():
            if n.archived:
                continue
            if n.type == "memory" and n.ref in ref_set:
                out.append(n.id)
        return out
    except Exception as e:
        logger.debug("neuron ref resolve failed: %s", e)
        return []


def on_memory_injected(
    memory_entries: list[dict],
    *,
    query: str = "",
) -> dict[str, Any]:
    """Call after memories are injected into chat context.

    - Ensures each memory has a neuron node
    - strengthen() all co-injected ids (Hebbian co-activation)
    - activate(query) for side-effect last_activated timestamps
    """
    neurons = _safe_import()
    if not neurons or not memory_entries:
        return {"ok": False, "message": "skip", "strengthened": 0}

    try:
        neuron_ids: list[str] = []
        for m in memory_entries:
            mid = m.get("id")
            text = m.get("text") or ""
            if not mid:
                continue
            nid = ensure_memory_node(
                mid,
                text,
                category=m.get("category") or "fact",
                pinned=bool(m.get("pinned")),
            )
            if nid:
                neuron_ids.append(nid)

        # de-dupe preserve order
        seen = set()
        unique = []
        for i in neuron_ids:
            if i not in seen:
                seen.add(i)
                unique.append(i)

        result: dict[str, Any] = {"ok": True, "nodes": len(unique), "strengthened": 0}
        if len(unique) >= 2:
            s = neurons.strengthen(unique)
            result["strengthened"] = s.get("updated_edges", 0)
            result["strengthen"] = s
        elif len(unique) == 1:
            # still mark activation via activate with node_ids
            pass

        if query or unique:
            act = neurons.activate(query=query or "", node_ids=unique[:10], top_k=min(10, max(3, len(unique))))
            result["activate_mode"] = act.get("mode")
            result["activate_count"] = act.get("result_count")

        return result
    except Exception as e:
        logger.warning("on_memory_injected failed: %s", e)
        return {"ok": False, "message": str(e), "strengthened": 0}


def on_memory_created(entry: dict) -> Optional[str]:
    """Call when a new memory fact is stored (extract or manual add)."""
    if not entry:
        return None
    return ensure_memory_node(
        entry.get("id") or "",
        entry.get("text") or "",
        category=entry.get("category") or "fact",
        pinned=bool(entry.get("pinned")),
    )


def on_project_selected(project_id: str, label: str = "", text: str = "") -> dict[str, Any]:
    """Project HQ selection → ensure project node + light activate."""
    neurons = _safe_import()
    if not neurons or not project_id:
        return {"ok": False}
    try:
        nid = ensure_project_node(project_id, label=label, text=text)
        if not nid:
            return {"ok": False}
        act = neurons.activate(query=label or project_id, node_ids=[nid], top_k=5)
        return {"ok": True, "node_id": nid, "activate_count": act.get("result_count")}
    except Exception as e:
        logger.warning("on_project_selected failed: %s", e)
        return {"ok": False, "message": str(e)}
