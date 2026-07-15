"""Odys Home Service — briefing, status, activity summary.

Lightweight home dashboard data. Reuses odys_projects_service.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from services.odys_projects_service import list_projects


def _greeting() -> str:
    hour = datetime.now().hour
    if hour < 11:
        return "Selamat pagi"
    if hour < 15:
        return "Selamat siang"
    if hour < 18:
        return "Selamat sore"
    return "Selamat malam"


def _bridge_status() -> dict:
    """Check Desktop Bridge health (localhost:8765)."""
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8765/health",
            method="GET",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return {"ok": True, "status": "online", "detail": data[:200]}
    except Exception as e:
        return {"ok": False, "status": "offline", "detail": str(e)[:120]}


def _server_status() -> dict:
    """Server is us — always online if this runs."""
    return {"ok": True, "status": "online", "detail": "Odys server running"}


def get_briefing() -> dict[str, Any]:
    """Build home briefing: greeting + project summary + status."""
    projects = list_projects()
    pinned = [p for p in projects if p.get("pinned")]
    recent = sorted(
        projects,
        key=lambda p: p.get("last_activity_at") or p.get("last_indexed_at") or "",
        reverse=True,
    )[:5]

    # Build priorities from recent activity
    priorities = []
    for p in recent[:4]:
        priorities.append({
            "name": p.get("name"),
            "id": p.get("id"),
            "stack": (p.get("detected_stack") or [p.get("detected_type") or "Project"])[0],
            "score": p.get("potential_score"),
            "files": p.get("file_count", 0),
            "activity": p.get("last_activity_at"),
        })

    # Recommendation
    unindexed = [p for p in projects if not p.get("last_indexed_at")]
    if unindexed:
        rec = f"Index {len(unindexed)} project(s) belum di-index — buka Odys Projects → Index All"
    elif not projects:
        rec = "Scan workspace dulu — buka Odys Projects → Scan"
    else:
        top = recent[0] if recent else None
        rec = f"Lanjut kerja di {top['name']}" if top else "Semua proyek ter-index. Siap kerja."

    greeting = _greeting()
    headline = f"{greeting}, Tuan. {len(projects)} project aktif"
    if pinned:
        headline += f" · {len(pinned)} pinned"

    spoken = f"{greeting}. Ada {len(projects)} project. {rec}"

    bridge = _bridge_status()
    server = _server_status()

    return {
        "ok": True,
        "greeting": greeting,
        "headline": headline,
        "spoken": spoken,
        "recommendation": rec,
        "priorities": priorities,
        "project_count": len(projects),
        "pinned_count": len(pinned),
        "status": {
            "server": server,
            "bridge": bridge,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "projects": recent,
    }
