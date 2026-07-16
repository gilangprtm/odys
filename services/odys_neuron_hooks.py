"""Odys Neuron hooks — Phase 2/3.

Bridge chat memory retrieve + memory extract + vault notes → neuron graph.
Fail-soft: never raise into chat path.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

VAULT_SCAN_GLOBS = ("*.md", "wiki/**/*.md", "Philosophy/**/*.md", "Sessions/**/*.md")
VAULT_SKIP_PARTS = {"graphify-out", "ingested", "raw", "_templates", "node_modules", ".git"}


def _safe_import():
    try:
        from services import odys_neuron_service as neurons
        return neurons
    except Exception as e:
        logger.debug("neuron service unavailable: %s", e)
        return None


def ensure_memory_node(
    memory_id: str, text: str, *, category: str = "fact", pinned: bool = False
) -> Optional[str]:
    neurons = _safe_import()
    if not neurons or not memory_id or not (text or "").strip():
        return None
    try:
        base = 0.9 if pinned or category == "identity" else 0.5
        label = (text or "").strip()[:60]
        r = neurons.add_node(type="memory", label=label, ref=str(memory_id), base_weight=base, text=text)
        return (r.get("node") or {}).get("id")
    except Exception as e:
        logger.warning("neuron ensure_memory_node failed: %s", e)
        return None


def ensure_project_node(project_id: str, label: str = "", text: str = "") -> Optional[str]:
    neurons = _safe_import()
    if not neurons or not project_id:
        return None
    try:
        r = neurons.add_node(type="project", label=label or project_id, ref=str(project_id), base_weight=0.7, text=text or label or project_id)
        return (r.get("node") or {}).get("id")
    except Exception as e:
        logger.warning("neuron ensure_project_node failed: %s", e)
        return None


def ensure_vault_node(rel_path: str, title: str, text: str = "", *, base_weight: float = 0.6) -> Optional[str]:
    neurons = _safe_import()
    if not neurons or not rel_path:
        return None
    try:
        label = (title or Path(rel_path).stem)[:80]
        body = text or title or rel_path
        r = neurons.add_node(type="vault_note", label=label, ref=rel_path.replace("\\", "/"), base_weight=base_weight, text=body)
        return (r.get("node") or {}).get("id")
    except Exception as e:
        logger.warning("neuron ensure_vault_node failed: %s", e)
        return None


def on_memory_injected(memory_entries: list[dict], *, query: str = "") -> dict[str, Any]:
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
            nid = ensure_memory_node(mid, text, category=m.get("category") or "fact", pinned=bool(m.get("pinned")))
            if nid:
                neuron_ids.append(nid)
        seen = set()
        unique = [i for i in neuron_ids if not (i in seen or seen.add(i))]
        result: dict[str, Any] = {"ok": True, "nodes": len(unique), "strengthened": 0}
        if len(unique) >= 2:
            s = neurons.strengthen(unique)
            result["strengthened"] = s.get("updated_edges", 0)
            result["strengthen"] = s
        if query or unique:
            act = neurons.activate(query=query or "", node_ids=unique[:10], top_k=min(10, max(3, len(unique))))
            result["activate_mode"] = act.get("mode")
            result["activate_count"] = act.get("result_count")
        return result
    except Exception as e:
        logger.warning("on_memory_injected failed: %s", e)
        return {"ok": False, "message": str(e), "strengthened": 0}


def on_memory_created(entry: dict) -> Optional[str]:
    if not entry:
        return None
    return ensure_memory_node(entry.get("id") or "", entry.get("text") or "", category=entry.get("category") or "fact", pinned=bool(entry.get("pinned")))


def on_project_selected(project_id: str, label: str = "", text: str = "") -> dict[str, Any]:
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


# ── Phase 3: Vault bridge ────────────────────────────────


def _parse_md(path: Path, max_excerpt: int = 400) -> tuple[str, str, list[str]]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path.stem, path.stem, []
    title = path.stem
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]; body = parts[2]
            m = re.search(r"(?m)^title:\s*[\"']?(.+?)[\"']?\s*$", fm)
            if m:
                title = m.group(1).strip().strip("\"'")
    lines = []
    for s in body.splitlines():
        s = s.strip()
        if not s or s.startswith("#") or s.startswith("|") or s.startswith("```"):
            continue
        lines.append(s)
        if sum(len(x) for x in lines) > max_excerpt:
            break
    excerpt = " ".join(lines)[:max_excerpt]
    wikilinks = re.findall(r"\[\[([^\]|#]+?)(?:[|#][^\]]*)?\]\]", body)
    wikilinks = [w.strip().replace("\\", "/") for w in wikilinks if w.strip()]
    return title, f"{title} {excerpt} {' '.join(wikilinks)}", wikilinks


def sync_vault_notes(vault_path: Path | str | None = None, *, link_wikilinks: bool = True) -> dict[str, Any]:
    neurons = _safe_import()
    if not neurons:
        return {"ok": False, "message": "neuron unavailable"}
    try:
        from services.odys_vault import get_vault_path
    except ImportError:
        get_vault_path = None

    vault = Path(vault_path) if vault_path else (get_vault_path() if get_vault_path else None)
    if not vault or not vault.is_dir():
        return {"ok": False, "message": f"vault missing: {vault}", "upserted": 0}
    vault = vault.resolve()

    md_files: list[Path] = []
    for pat in VAULT_SCAN_GLOBS:
        for f in vault.glob(pat):
            r = f.resolve()
            if not r.is_file() or r.suffix.lower() != ".md":
                continue
            try:
                rel = r.relative_to(vault)
                if set(rel.parts) & VAULT_SKIP_PARTS:
                    continue
            except ValueError:
                continue
            if r.name.startswith("."):
                continue
            md_files.append(r)
    md_files = list(dict.fromkeys(md_files))  # unique preserve order

    ref_to_nid: dict[str, str] = {}
    title_to_nid: dict[str, str] = {}
    pending_links: list[tuple[str, list[str]]] = []
    upserted = 0

    for p in sorted(md_files, key=lambda x: str(x).lower()):
        rel = str(p.relative_to(vault)).replace("\\", "/")
        title, text, links = _parse_md(p)
        base = 0.75 if p.name in ("AGENTS.md", "SCHEMA.md") or "prd" in p.name.lower() else 0.6
        nid = ensure_vault_node(rel, title, text, base_weight=base)
        if not nid:
            continue
        upserted += 1
        ref_to_nid[rel] = nid
        ref_to_nid[p.stem] = nid
        title_to_nid[title.lower()] = nid
        title_to_nid[p.stem.lower()] = nid
        if link_wikilinks and links:
            pending_links.append((nid, links))

    linked = 0
    if link_wikilinks and pending_links:
        for src_nid, links in pending_links:
            targets = set()
            for link in links:
                tid = ref_to_nid.get(link) or ref_to_nid.get(f"{link}.md") or title_to_nid.get(link.lower())
                if not tid:
                    base = Path(link).stem.lower()
                    tid = title_to_nid.get(base)
                if tid and tid != src_nid:
                    targets.add(tid)
            for tid in list(targets)[:12]:
                s = neurons.strengthen([src_nid, tid])
                if s.get("ok"):
                    linked += s.get("updated_edges", 0)

    st = (neurons.status().get("stats") or {})
    return {
        "ok": True,
        "vault": str(vault),
        "files_scanned": len(md_files),
        "upserted": upserted,
        "wikilink_edges": linked,
        "stats": st,
    }
