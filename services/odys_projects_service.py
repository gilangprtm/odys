"""Odys Projects Service — scan repo, git log, index.

Berjalan native (bukan Docker mount). Scan folder proyek langusng dari
filesystem Windows.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Konfigurasi ─────────────────────────────────────────

def ensure_projects_root(root: Path | None = None) -> dict[str, Any]:
    """Create projects root directory and index if missing. Idempotent.
    Returns dict: {ok, path, created: bool, message}
    """
    root = (root or get_projects_root()).expanduser().resolve()
    created = False
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)
        created = True
    # Ensure index file exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PROJECTS_INDEX_FILE.exists():
        _save_index({})
    message = f"Projects root ready at {root}"
    if created:
        message += " (newly created)"
    return {
        "ok": True,
        "path": str(root),
        "created": created,
        "message": message,
    }


def get_projects_root() -> Path:
    env = os.environ.get("ODY_PROJECTS_PATH") or os.environ.get("ODYS_PROJECTS_PATH")
    if env:
        return Path(env).expanduser()
    # Cross-platform fallback
    import sys
    if sys.platform == "win32":
        return Path("D:/Project")
    return Path.home() / "projects"

DEFAULT_PROJECTS_ROOT = get_projects_root()
DATA_DIR = Path("data/odys_projects")
PROJECTS_INDEX_FILE = DATA_DIR / "projects_index.json"

# Stack detection by file presence
STACK_PATTERNS: dict[str, list[str]] = {
    "Next.js": ["next.config", "next.config.ts", "next.config.mjs"],
    "React": ["vite.config.ts", "vite.config.js", "src/App.tsx", "src/App.js"],
    "FastAPI": ["main.py", "app.py", "requirements.txt", "pyproject.toml"],
    "Python": ["setup.py", "setup.cfg", "Pipfile", "pyproject.toml"],
    "TypeScript": ["tsconfig.json"],
    "Node.js": ["package.json"],
    "Rust": ["Cargo.toml"],
    "Go": ["go.mod"],
}


@dataclass
class ProjectInfo:
    id: str
    name: str
    path: str
    detected_type: str = ""
    detected_stack: list[str] = field(default_factory=list)
    file_count: int = 0
    last_indexed_at: str | None = None
    last_activity_at: str | None = None
    recent_changes: dict | None = None
    potential_score: int | None = None
    pinned: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "detected_type": self.detected_type,
            "detected_stack": self.detected_stack,
            "file_count": self.file_count,
            "last_indexed_at": self.last_indexed_at,
            "last_activity_at": self.last_activity_at,
            "recent_changes": self.recent_changes,
            "potential_score": self.potential_score,
            "pinned": self.pinned,
        }


def _detect_stack(path: Path) -> tuple[str, list[str]]:
    """Detect project type + stack from files in root."""
    stacks: list[str] = []
    for stack_name, patterns in STACK_PATTERNS.items():
        for pat in patterns:
            if (path / pat).exists() or (path / pat.lower()).exists():
                stacks.append(stack_name)
                break

    # Dedupe
    seen: set[str] = set()
    unique: list[str] = []
    for s in stacks:
        if s not in seen:
            seen.add(s)
            unique.append(s)

    # Determine primary type
    ptype = "Project"
    if "Next.js" in unique:
        ptype = "Web App (Next.js)"
    elif "React" in unique:
        ptype = "Web App (React)"
    elif "FastAPI" in unique:
        ptype = "Backend (FastAPI)"
    elif "Rust" in unique:
        ptype = "CLI / Library (Rust)"

    return ptype, unique


def _git_log(path: Path, count: int = 20) -> list[dict]:
    """Recent git log entries."""
    try:
        out = subprocess.run(
            ["git", "log", f"-{count}", "--format=%H|%ai|%s"],
            cwd=path, capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return []
        entries = []
        for line in out.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                entries.append({
                    "hash": parts[0][:8],
                    "date": parts[1],
                    "message": parts[2],
                })
        return entries
    except Exception:
        return []


def _git_changes_since(path: Path, since: str | None = None) -> dict:
    """Count new/modified/deleted files since last index."""
    if not since:
        return {"new_count": 0, "modified_count": 0, "deleted_count": 0, "recent_files": []}
    try:
        # Use git diff --name-status since the indexed commit
        out = subprocess.run(
            ["git", "log", "--oneline", "-1", "--format=%H"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        head = out.stdout.strip()
        if not head:
            return {"new_count": 0, "modified_count": 0, "deleted_count": 0, "recent_files": []}

        # Compare head to indexed ref (we use the timestamp as rough proxy)
        diff = subprocess.run(
            ["git", "diff", "--name-status", f"@{{2026-01-01}}..HEAD"],  # rough
            cwd=path, capture_output=True, text=True, timeout=10,
        )
        if diff.returncode != 0:
            return {"new_count": 0, "modified_count": 0, "deleted_count": 0, "recent_files": []}

        new = modified = deleted = 0
        recent_files = []
        for line in diff.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            status, fpath = parts
            if status.startswith("A"):
                new += 1
                recent_files.append(f"+ {fpath}")
            elif status.startswith("M"):
                modified += 1
                recent_files.append(f"~ {fpath}")
            elif status.startswith("D"):
                deleted += 1
                recent_files.append(f"- {fpath}")

        return {
            "new_count": new,
            "modified_count": modified,
            "deleted_count": deleted,
            "recent_files": recent_files[:20],
        }
    except Exception:
        return {"new_count": 0, "modified_count": 0, "deleted_count": 0, "recent_files": []}


def _count_files(path: Path) -> int:
    """Count source files (not node_modules, .git, etc.)."""
    total = 0
    try:
        for root, dirs, files in os.walk(str(path)):
            # Skip common dirs
            dirs[:] = [d for d in dirs if d not in (
                "node_modules", ".git", "__pycache__", ".next",
                ".venv", "venv", ".data", "dist", "build", ".hermes",
            )]
            total += len([f for f in files if not f.startswith(".")])
    except Exception:
        pass
    return total


def _scan_root(root: Path) -> list[ProjectInfo]:
    """Scan a directory for project folders (have .git or known config)."""
    projects: list[ProjectInfo] = []
    if not root.is_dir():
        return projects

    for entry in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        # Must have .git or package.json/pyproject.toml etc
        has_git = (entry / ".git").is_dir()
        has_config = any(
            (entry / pat).exists()
            for patterns in STACK_PATTERNS.values()
            for pat in patterns
        )
        if not has_git and not has_config:
            continue

        ptype, stack = _detect_stack(entry)
        projects.append(ProjectInfo(
            id=entry.name.lower().replace(" ", "-"),
            name=entry.name,
            path=str(entry.resolve()),
            detected_type=ptype,
            detected_stack=stack,
            file_count=_count_files(entry),
        ))
    return projects


def _load_index() -> dict:
    """Load persisted project index."""
    if PROJECTS_INDEX_FILE.exists():
        try:
            return json.loads(PROJECTS_INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_index(index: dict):
    """Save project index."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_INDEX_FILE.write_text(
        json.dumps(index, indent=2, default=str), encoding="utf-8"
    )


def list_projects() -> list[dict]:
    """Return all discovered projects with cached info."""
    index = _load_index()
    return list(index.values())


def scan_projects() -> dict:
    """Scan D:/Project for new/modified projects."""
    projects = _scan_root(DEFAULT_PROJECTS_ROOT)
    index = _load_index()

    for p in projects:
        key = p.id
        existing = index.get(key, {})
        p.pinned = existing.get("pinned", False)

        # Preserve last_indexed_at
        if existing.get("last_indexed_at"):
            p.last_indexed_at = existing["last_indexed_at"]

        # Show recent git activity
        git_log = _git_log(Path(p.path), count=5)
        if git_log:
            p.last_activity_at = git_log[0]["date"]
            p.recent_changes = {
                "new_count": 0,
                "modified_count": 0,
                "deleted_count": 0,
                "recent_files": [e["message"][:60] for e in git_log[:5]],
            }

        index[key] = p.to_dict()

    _save_index(index)
    # Natural: seed project neurons from scan (fail-soft)
    try:
        from services.odys_neuron_hooks import seed_projects_from_index
        seed_projects_from_index(list(index.values()))
    except Exception:
        pass
    return {
        "ok": True,
        "projects": list(index.values()),
        "message": f"Scan complete — {len(projects)} project(s) found",
    }


def index_project(project_id: str) -> dict:
    """Index a single project: git diff + file count."""
    index = _load_index()
    entry = index.get(project_id)
    if not entry:
        return {"ok": False, "message": f"Project '{project_id}' not found"}

    path = Path(entry["path"])
    if not path.is_dir():
        return {"ok": False, "message": f"Path not found: {entry['path']}"}

    entry["file_count"] = _count_files(path)
    entry["last_indexed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    git_log = _git_log(path, count=10)
    if git_log:
        entry["last_activity_at"] = git_log[0]["date"]
        entry["recent_changes"] = {
            "new_count": 0,
            "modified_count": 0,
            "deleted_count": 0,
            "recent_files": [e["message"][:60] for e in git_log[:5]],
        }

    # Update type/stack
    ptype, stack = _detect_stack(path)
    entry["detected_type"] = ptype
    entry["detected_stack"] = stack

    index[project_id] = entry
    _save_index(index)

    return {"ok": True, "project": entry, "message": f"Indexed: {entry['name']}"}


def get_project_detail(project_id: str) -> dict:
    """Get detailed info for a single project."""
    index = _load_index()
    entry = index.get(project_id)
    if not entry:
        return {"ok": False, "message": "Not found"}

    path = Path(entry["path"])
    entry["file_count"] = _count_files(path) if path.is_dir() else entry.get("file_count", 0)

    # Git log for recent changes
    if path.is_dir():
        git_log = _git_log(path, count=20)
        changes = {
            "new_count": 0,
            "modified_count": 0,
            "deleted_count": 0,
            "recent_files": [],
            "last_modified": entry.get("last_activity_at"),
        }
        if git_log:
            changes["last_modified"] = git_log[0]["date"]
            changes["recent_files"] = [e["message"][:60] for e in git_log[:8]]
            changes["new_count"] = sum(1 for e in git_log if "add" in e["message"].lower())
            changes["modified_count"] = sum(1 for e in git_log if "fix" in e["message"].lower() or "update" in e["message"].lower())
        entry["recent_changes"] = changes

    return {
        "ok": True,
        "project": entry,
        "score": {"overall": entry.get("potential_score", 50)},
        "stage": {"current": entry.get("detected_type", "Project")},
        "recent_changes": entry.get("recent_changes", {}),
        "recommendation": {"do_this_next": f"Review {entry['name']} — {entry.get('detected_stack', [''])[0] if entry.get('detected_stack') else 'unknown stack'}"},
    }


def pin_project(project_id: str) -> dict:
    """Toggle pin status."""
    index = _load_index()
    entry = index.get(project_id)
    if not entry:
        return {"ok": False, "message": "Not found"}
    entry["pinned"] = not entry.get("pinned", False)
    index[project_id] = entry
    _save_index(index)
    return {"ok": True, "pinned": entry["pinned"]}
