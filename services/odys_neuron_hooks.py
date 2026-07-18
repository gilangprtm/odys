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

VAULT_SCAN_GLOBS = ("**/*.md",)
VAULT_SKIP_PARTS = {"node_modules", ".git"}


def _safe_import():
    try:
        from services import odys_neuron_service as neurons
        return neurons
    except Exception as e:
        logger.debug("neuron service unavailable: %s", e)
        return None


def _get_graph_instance():
    """Get the NeuronGraph instance directly (not the module-level API wrapper)."""
    try:
        from services import odys_neuron_service as neurons
        return neurons.get_graph()
    except Exception:
        return None

def ensure_memory_node(
    memory_id: str, text: str, *, category: str = "fact", pinned: bool = False
) -> Optional[str]:
    """Ensure a memory node exists in the graph, then return its ID."""
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


def import_all_memories_to_neurons(owner: str = "") -> dict[str, Any]:
    """Bulk-import existing memory.json entries into neuron nodes. Idempotent."""
    try:
        from src.memory import MemoryManager as _MM
        _sn = _safe_import()
        if not _sn:
            return {"ok": False, "message": "neuron service unavailable"}

        mm = _MM("")
        all_mem = mm.load(owner=owner if owner else None)
        if not all_mem:
            return {"ok": True, "imported": 0, "total": 0, "message": "no memories found"}

        # Get existing neuron memory nodes to avoid re-import
        existing = _sn.list_nodes()
        existing_refs = {n.ref for n in existing if n.type == "memory"}

        imported = 0
        skipped = 0
        for m in all_mem:
            mid = m.get("id") or ""
            if not mid or not m.get("text"):
                continue
            if mid in existing_refs:
                skipped += 1
                continue
            base = 0.9 if m.get("pinned") or m.get("category") == "identity" else 0.5
            try:
                _sn.add_node(
                    type="memory",
                    label=(m["text"][:60]),
                    ref=str(mid),
                    base_weight=base,
                    text=m["text"],
                )
                imported += 1
            except Exception:
                pass

        # Auto-strengthen co-occurring memories (from same chat session)
        all_ids = [n.id for n in _sn.list_nodes() if n.type == "memory" and n.id in {r.id for r in _sn.list_nodes()}]
        if len(all_ids) >= 2:
            # Strengthen the first batch of imported memories as a cluster
            new_ids = [n.id for n in _sn.list_nodes() if n.type == "memory" and n.id not in existing_refs]
            if len(new_ids) >= 2:
                _sn.strengthen(new_ids[:10])

        st = _sn.status()
        return {
            "ok": True,
            "imported": imported,
            "skipped": skipped,
            "total": len(all_mem),
            "graph": st.get("stats", {}),
        }
    except Exception as e:
        logger.warning("import_all_memories_to_neurons failed: %s", e)
        return {"ok": False, "message": str(e)}


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



# ── Natural maintenance (silent, automatic) ───────────────

_LAST_SYNC_KEY = "last_vault_sync_at"
_LAST_DECAY_KEY = "last_decay_at"


def _cfg_get() -> dict:
    try:
        from services.odys_vault import load_config
        return load_config() or {}
    except Exception:
        return {}


def _cfg_set(updates: dict) -> None:
    try:
        from services.odys_vault import load_config, save_config
        cfg = load_config() or {}
        cfg.update(updates)
        save_config(cfg)
    except Exception as e:
        logger.debug("neuron cfg_set failed: %s", e)


def _hours_since(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        import time as _t
        t0 = _t.mktime(_t.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
        return (_t.time() - t0) / 3600.0
    except Exception:
        return 1e9


def sync_sessions_to_neurons(owner: str = "") -> dict[str, Any]:
    """Sync active chat sessions from DB to neuron nodes (Phase 2 enhancement)."""
    neurons = _safe_import()
    if not neurons:
        return {"ok": False, "message": "neuron unavailable"}
    try:
        from core.database import SessionLocal, Session as DBSession
        db = SessionLocal()
        try:
            q = db.query(DBSession).filter(
                DBSession.archived == False,
                DBSession.message_count > 0,
            )
            if owner:
                q = q.filter((DBSession.owner == owner) | (DBSession.owner.is_(None)))
            sessions = q.all()
        finally:
            db.close()

        if not sessions:
            return {"ok": True, "synced": 0, "message": "no active sessions"}

        synced = 0
        for s in sessions:
            # Build node text from session name + metadata
            parts = [s.name or "unnamed"]
            if s.mode:
                parts.append(f"mode:{s.mode}")
            if s.message_count:
                parts.append(f"{s.message_count} messages")
            if s.is_important:
                parts.append("important")
            text = " | ".join(parts)

            # Base weight: important sessions get higher weight
            base = 0.7 if s.is_important else 0.4
            if s.message_count and s.message_count > 10:
                base += 0.1  # active conversations

            # Upsert as session node
            nid = neurons.add_node(
                type="session",
                label=s.name or "unnamed session",
                ref=s.id,
                base_weight=min(base, 1.0),
                text=text,
            )
            synced += 1

        # Strengthen co-occurring sessions from same owner
        try:
            graph_instance = _get_graph_instance()
            if graph_instance:
                session_nodes = [n for n in graph_instance.list_nodes() if n.type == "session"]
                if len(session_nodes) >= 2:
                    graph_instance.strengthen([n.id for n in session_nodes[:10]])
        except Exception:
            pass

        return {"ok": True, "synced": synced, "total_sessions": len(sessions)}
    except Exception as e:
        logger.warning("sync_sessions_to_neurons failed: %s", e)
        return {"ok": False, "message": str(e)}


def seed_projects_from_index(projects: list[dict] | None = None) -> dict[str, Any]:
    """Upsert project nodes from Project HQ index (silent)."""
    try:
        if projects is None:
            from services.odys_projects_service import list_projects
            projects = list_projects()
    except Exception as e:
        return {"ok": False, "message": str(e), "seeded": 0}

    seeded = 0
    for p in projects or []:
        pid = p.get("id") or p.get("name")
        if not pid:
            continue
        label = p.get("name") or pid
        text = " ".join(
            str(x) for x in (
                label,
                p.get("detected_type"),
                " ".join(p.get("detected_stack") or []),
                p.get("path"),
            ) if x
        )
        if ensure_project_node(str(pid), label=label, text=text):
            seeded += 1
    return {"ok": True, "seeded": seeded}


def natural_boot(*, force_sync: bool = False, force_decay: bool = False) -> dict[str, Any]:
    """Run on odys start — silent vault sync + light decay + project seed.

    Rules (natural, not noisy):
    - vault sync if never / older than 6h
    - decay if never / older than 24h
    - always try seed projects from existing index (cheap)
    """
    import time as _t
    out: dict[str, Any] = {"ok": True, "actions": []}
    cfg = _cfg_get()
    now = _t.strftime("%Y-%m-%dT%H:%M:%S")

    # 1) vault sync
    last_sync = cfg.get(_LAST_SYNC_KEY)
    if force_sync or _hours_since(last_sync) >= 6:
        # Ensure vault exists before syncing (creates structure if missing, cross-platform)
        try:
            from services.odys_vault import ensure_odys_vault, get_vault_path
            ensure_odys_vault(get_vault_path())
        except Exception as ev:
            logger.error(f"Failed to ensure odys vault at boot: {ev}")

        r = sync_vault_notes()
        out["vault_sync"] = r
        out["actions"].append("vault_sync")
        if r.get("ok"):
            _cfg_set({_LAST_SYNC_KEY: now})
    else:
        out["vault_sync"] = {"ok": True, "skipped": True, "hours_since": round(_hours_since(last_sync), 2)}

    # 2) project seed
    try:
        # Ensure projects root exists (creates directory if missing)
        from services.odys_projects_service import ensure_projects_root
        ensure_projects_root()
        sp = seed_projects_from_index()
        out["project_seed"] = sp
        if sp.get("seeded"):
            out["actions"].append("project_seed")
    except Exception as e:
        out["project_seed"] = {"ok": False, "message": str(e)}

    # 3) memory → neuron import (if never done)
    try:
        from src.memory import MemoryManager as _MM
        _sn = _safe_import()
        all_mem = _MM(str("")).load_all() if _MM else []
        st = _sn.status() if _sn else {}
        existing_count = (st.get("stats") or {}).get("node_count", 0)
        mem_nodes = [m for m in all_mem if m.get("id") and m.get("text")]
        if mem_nodes and existing_count < 6:  # fresh graph
            imported = 0
            for m in mem_nodes[:200]:
                base = 0.9 if m.get("pinned") or m.get("category") == "identity" else 0.5
                try:
                    _sn.add_node(
                        type="memory",
                        label=(m["text"][:60]),
                        ref=str(m["id"]),
                        base_weight=base,
                        text=m["text"],
                    )
                    imported += 1
                except Exception:
                    pass
            out["memory_import"] = {"ok": True, "imported": imported, "total": len(mem_nodes)}
            out["actions"].append("memory_import")
    except Exception as e:
        out["memory_import"] = {"ok": False, "message": str(e)}

    # 4) light decay
    last_decay = cfg.get(_LAST_DECAY_KEY)
    if force_decay or _hours_since(last_decay) >= 24:
        neurons = _safe_import()
        if neurons:
            d = neurons.decay()
            out["decay"] = d
            out["actions"].append("decay")
            if d.get("ok"):
                _cfg_set({_LAST_DECAY_KEY: now})
        else:
            out["decay"] = {"ok": False, "message": "no service"}
    else:
        out["decay"] = {"ok": True, "skipped": True, "hours_since": round(_hours_since(last_decay), 2)}

    # 5) session sync → neurons
    try:
        ss = sync_sessions_to_neurons()
        out["session_sync"] = ss
        if ss.get("synced"):
            out["actions"].append("session_sync")
    except Exception as e:
        out["session_sync"] = {"ok": False, "message": str(e)}

    return out


def active_thoughts_for_chat(query: str = "", top_k: int = 5) -> list[dict[str, Any]]:
    """Return compact active thoughts for silent chat injection."""
    neurons = _safe_import()
    if not neurons:
        return []
    try:
        act = neurons.activate(query=query or "", top_k=top_k)
        rows = []
        for r in (act.get("results") or [])[:top_k]:
            # skip ultra-weak noise
            if float(r.get("score") or 0) < 0.25:
                continue
            rows.append({
                "type": r.get("type"),
                "label": r.get("label"),
                "ref": r.get("ref"),
                "score": r.get("score"),
                "why": r.get("why"),
            })
        return rows
    except Exception as e:
        logger.debug("active_thoughts_for_chat failed: %s", e)
        return []
