"""SQLite-backed neuron graph storage (Phase 2 — replaces JSON file).

Migrates graph.json → data/odys_neurons/graph.db on first access.
JSON file kept as backup. Provides atomic writes via WAL mode.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DATA_DIR = Path("data/odys_neurons")
DB_FILE = DATA_DIR / "graph.db"
GRAPH_JSON = DATA_DIR / "graph.json"

_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@contextmanager
def _connect():
    """WAL-mode connection with auto-commit."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if not exist. Runs once per process."""
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL DEFAULT 'memory',
                label TEXT NOT NULL DEFAULT '',
                ref TEXT NOT NULL DEFAULT '',
                base_weight REAL NOT NULL DEFAULT 0.5,
                tokens TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                last_activated_at TEXT,
                archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS edges (
                key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 0.1,
                count INTEGER NOT NULL DEFAULT 1,
                last_seen TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
            CREATE INDEX IF NOT EXISTS idx_nodes_archived ON nodes(archived);
            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
            CREATE INDEX IF NOT EXISTS idx_edges_weight ON edges(weight);

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)


def migrate_from_json() -> dict[str, Any]:
    """One-time migration from graph.json → graph.db. Returns migration stats."""
    if not GRAPH_JSON.exists():
        return {"ok": True, "migrated": 0, "message": "no JSON file"}

    try:
        raw = json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "message": f"JSON read error: {e}"}

    nodes_raw = raw.get("nodes") or {}
    edges_raw = raw.get("edges") or {}

    with _connect() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        if existing > 0:
            return {"ok": True, "migrated": 0, "message": f"DB already has {existing} nodes"}

        for nid, nd in nodes_raw.items():
            conn.execute(
                """INSERT OR REPLACE INTO nodes
                   (id, type, label, ref, base_weight, tokens, created_at, updated_at, last_activated_at, archived)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (nid, nd.get("type", "memory"), nd.get("label", ""), nd.get("ref", ""),
                 float(nd.get("base_weight", 0.5)), json.dumps(nd.get("tokens") or []),
                 nd.get("created_at", ""), nd.get("updated_at", ""),
                 nd.get("last_activated_at"), 1 if nd.get("archived") else 0),
            )

        for ek, ed in edges_raw.items():
            conn.execute(
                """INSERT OR REPLACE INTO edges (key, source, target, weight, count, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ek, ed["source"], ed["target"], float(ed.get("weight", 0.1)),
                 int(ed.get("count", 1)), ed.get("last_seen", "")),
            )

        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', '2')")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('migrated_from', 'json')")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('migrated_at', ?)", (_now(),))

    # Backup JSON
    bak = GRAPH_JSON.with_suffix(".json.migrated")
    try:
        if bak.exists():
            bak.unlink()
        GRAPH_JSON.rename(bak)
    except OSError:
        pass

    return {"ok": True, "migrated_nodes": len(nodes_raw), "migrated_edges": len(edges_raw)}


def load_all() -> tuple[dict[str, dict], dict[str, dict]]:
    """Load all nodes and edges. Returns (nodes_dict, edges_dict)."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM nodes").fetchall()
        nodes = {}
        for r in rows:
            nodes[r["id"]] = {
                "id": r["id"], "type": r["type"], "label": r["label"],
                "ref": r["ref"], "base_weight": r["base_weight"],
                "tokens": json.loads(r["tokens"]),
                "created_at": r["created_at"], "updated_at": r["updated_at"],
                "last_activated_at": r["last_activated_at"],
                "archived": bool(r["archived"]),
            }

        erows = conn.execute("SELECT * FROM edges").fetchall()
        edges = {}
        for r in erows:
            edges[r["key"]] = {
                "source": r["source"], "target": r["target"],
                "weight": r["weight"], "count": r["count"],
                "last_seen": r["last_seen"],
            }

    return nodes, edges


def save_all(nodes: dict[str, dict], edges: dict[str, dict]) -> None:
    """Atomic save of all nodes + edges."""
    with _lock:
        with _connect() as conn:
            conn.execute("DELETE FROM nodes")
            conn.execute("DELETE FROM edges")
            conn.executemany(
                """INSERT INTO nodes
                   (id, type, label, ref, base_weight, tokens, created_at, updated_at, last_activated_at, archived)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(nid, n["type"], n["label"], n["ref"], n["base_weight"],
                  json.dumps(n.get("tokens") or []), n.get("created_at", ""),
                  n.get("updated_at", ""), n.get("last_activated_at"),
                  1 if n.get("archived") else 0)
                 for nid, n in nodes.items()],
            )
            conn.executemany(
                """INSERT INTO edges (key, source, target, weight, count, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(ek, e["source"], e["target"], e["weight"], e["count"], e.get("last_seen", ""))
                 for ek, e in edges.items()],
            )


def stats() -> dict[str, Any]:
    """Quick stats without loading all data."""
    with _connect() as conn:
        node_count = conn.execute("SELECT COUNT(*) FROM nodes WHERE archived=0").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM nodes WHERE archived=1").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        types = conn.execute(
            "SELECT type, COUNT(*) as cnt FROM nodes WHERE archived=0 GROUP BY type"
        ).fetchall()
    return {
        "node_count": node_count,
        "archived_count": archived,
        "edge_count": edge_count,
        "by_type": {r["type"]: r["cnt"] for r in types},
    }


def node_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM nodes WHERE archived=0").fetchone()[0]
