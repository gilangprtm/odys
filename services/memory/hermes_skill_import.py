"""One-shot import of Hermes skills into Odysseus `data/skills/` (SAO SoT).

Hermes skills live under a host directory (e.g. %LOCALAPPDATA%/hermes/skills
or Docker mount /hermes-skills). After import, Odysseus owns the copies under
`data/skills/` and does not need Hermes process or live sync.

Non-goal: continuous two-way sync with Hermes.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

logger = logging.getLogger(__name__)

# Text-ish files we copy alongside SKILL.md (references, scripts, templates).
_ALLOWED_SUFFIXES = (
    ".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".toml",
    ".js", ".ts", ".css", ".html", ".xml", ".csv", ".ps1",
)
_TEXT_NAMES = frozenset({"skill.md", "license", "license.md", "readme.md", "agents.md"})
_SKIP_DIR_NAMES = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv"})


def default_hermes_skills_dir() -> str:
    """Resolve Hermes skills root (Docker mount first, then host paths)."""
    env = (os.environ.get("HERMES_SKILLS_DIR") or "").strip()
    if env and os.path.isdir(env):
        return env
    for candidate in (
        "/hermes-skills",
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "hermes", "skills"),
        os.path.join(os.path.expanduser("~"), ".hermes", "skills"),
    ):
        if os.path.isdir(candidate):
            return candidate
    return env or "/hermes-skills"


def _is_text_file(name: str) -> bool:
    low = name.lower()
    if low in _TEXT_NAMES:
        return True
    return any(low.endswith(s) for s in _ALLOWED_SUFFIXES)


def _rel_posix(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


@dataclass
class HermesSkillSource:
    """One skill directory under the Hermes tree."""
    name: str
    category: str
    skill_md_path: str
    skill_dir: str


@dataclass
class ImportResult:
    imported: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[Dict[str, str]] = field(default_factory=list)
    hermes_dir: str = ""
    dry_run: bool = False

    def as_dict(self) -> Dict:
        return {
            "hermes_dir": self.hermes_dir,
            "dry_run": self.dry_run,
            "imported": self.imported,
            "skipped": self.skipped,
            "failed": self.failed,
            "counts": {
                "imported": len(self.imported),
                "skipped": len(self.skipped),
                "failed": len(self.failed),
            },
        }


def discover_hermes_skills(hermes_dir: str) -> List[HermesSkillSource]:
    """Walk Hermes skills tree; each SKILL.md is one skill."""
    root = os.path.abspath(hermes_dir)
    if not os.path.isdir(root):
        return []

    found: List[HermesSkillSource] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # prune noisy dirs in-place
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        if "SKILL.md" not in filenames:
            continue
        skill_md = os.path.join(dirpath, "SKILL.md")
        skill_dir = dirpath
        rel = os.path.relpath(skill_dir, root)
        parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
        if not parts:
            # SKILL.md directly under root
            name = "root-skill"
            category = "imported"
        elif len(parts) == 1:
            # category-less: skills/foo/SKILL.md
            name = parts[0]
            category = "imported"
        else:
            # skills/<category>/<name>/... or deeper nest → last dir = name, first = category
            category = parts[0]
            name = parts[-1]
        found.append(
            HermesSkillSource(
                name=name,
                category=category,
                skill_md_path=skill_md,
                skill_dir=skill_dir,
            )
        )
    found.sort(key=lambda s: (s.category, s.name))
    return found


def _collect_skill_files(skill_dir: str, max_files: int = 64, max_total: int = 2_000_000) -> Dict[str, str]:
    """Relative path → text content for one Hermes skill directory."""
    files: Dict[str, str] = {}
    total = 0
    skill_dir = os.path.abspath(skill_dir)
    for dirpath, dirnames, filenames in os.walk(skill_dir, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        for fn in filenames:
            if not _is_text_file(fn):
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > 400_000:
                continue
            if total + size > max_total:
                break
            if len(files) >= max_files:
                break
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                logger.warning("skip unreadable %s: %s", full, e)
                continue
            rel = _rel_posix(os.path.relpath(full, skill_dir))
            files[rel] = text
            total += size
        if len(files) >= max_files or total >= max_total:
            break
    if "SKILL.md" not in files and "skill.md" not in {k.lower() for k in files}:
        # force include SKILL.md if walk skipped it
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if os.path.isfile(skill_md):
            with open(skill_md, encoding="utf-8", errors="replace") as f:
                files["SKILL.md"] = f.read()
    return files


def import_hermes_skills(
    skills_manager,
    *,
    hermes_dir: Optional[str] = None,
    names: Optional[Sequence[str]] = None,
    categories: Optional[Sequence[str]] = None,
    all_skills: bool = False,
    owner: Optional[str] = None,
    status: str = "published",
    overwrite: bool = False,
    dry_run: bool = False,
    max_skills: int = 200,
) -> ImportResult:
    """Copy Hermes skills into Odysseus SkillsManager disk store.

    Args:
        skills_manager: SkillsManager instance (data/skills SoT).
        hermes_dir: Hermes skills root; default auto-detect.
        names: optional name filter (slug match, case-insensitive).
        categories: optional category filter (top-level Hermes folder).
        all_skills: import every discovered skill (respects max_skills).
        owner: stamp owner frontmatter on import.
        status: draft | published (default published so index sees them).
        overwrite: if True, delete existing same-name skill then re-import.
        dry_run: discover + plan only, no writes.
        max_skills: hard cap.
    """
    root = hermes_dir or default_hermes_skills_dir()
    result = ImportResult(hermes_dir=root, dry_run=dry_run)

    if not os.path.isdir(root):
        result.failed.append({"name": "*", "error": f"Hermes skills dir not found: {root}"})
        return result

    sources = discover_hermes_skills(root)
    if not sources:
        result.failed.append({"name": "*", "error": f"No SKILL.md under {root}"})
        return result

    name_filter: Optional[Set[str]] = None
    if names:
        name_filter = {n.strip().lower() for n in names if n and str(n).strip()}
    cat_filter: Optional[Set[str]] = None
    if categories:
        cat_filter = {c.strip().lower() for c in categories if c and str(c).strip()}

    # Default: if neither names nor categories nor all_skills → import SAO-relevant cats
    if not name_filter and not cat_filter and not all_skills:
        cat_filter = {
            "autonomous-ai-agents",
            "github",
            "software-development",
            "note-taking",
            "devops",
            "deployment",
            "architecture",
            "data",
            "nextjs",
            "uiux",
            "productivity",
            "research",
        }

    selected: List[HermesSkillSource] = []
    for src in sources:
        if name_filter and src.name.lower() not in name_filter:
            continue
        if cat_filter and src.category.lower() not in cat_filter:
            continue
        selected.append(src)
        if len(selected) >= max_skills:
            break

    if not selected:
        result.failed.append({
            "name": "*",
            "error": (
                f"No skills matched filters (names={names}, categories={categories}, "
                f"all={all_skills}). Discovered={len(sources)} under {root}."
            ),
        })
        return result

    existing_names = {s.get("name") for s in skills_manager.load_all()}

    for src in selected:
        key = src.name
        if key in existing_names and not overwrite:
            result.skipped.append(f"{src.category}/{key}")
            continue
        if dry_run:
            result.imported.append(f"{src.category}/{key}")
            continue
        try:
            if key in existing_names and overwrite:
                try:
                    skills_manager.delete_skill(key, owner=owner)
                except Exception:
                    # try ownerless
                    skills_manager.delete_skill(key, owner=None)
            files = _collect_skill_files(src.skill_dir)
            if "SKILL.md" not in files:
                # case variants
                for k in list(files.keys()):
                    if k.lower() == "skill.md":
                        files["SKILL.md"] = files.pop(k)
                        break
            if "SKILL.md" not in files:
                result.failed.append({"name": key, "error": "missing SKILL.md content"})
                continue
            entry = skills_manager.import_bundle_from_files(
                files,
                owner=owner,
                source_url=f"hermes-local:{src.category}/{src.name}",
                category=src.category or "imported",
            )
            # Force published so Level-0 index injects them
            if status and entry.get("status") != status:
                skills_manager.update_skill(
                    entry["name"],
                    {"status": status, "source": "imported"},
                    owner=owner or entry.get("owner"),
                )
            result.imported.append(f"{src.category}/{entry.get('name', key)}")
            existing_names.add(entry.get("name", key))
        except Exception as e:
            logger.exception("import hermes skill failed: %s", key)
            result.failed.append({"name": key, "error": str(e)})

    return result


def list_hermes_skill_index(hermes_dir: Optional[str] = None) -> List[Dict[str, str]]:
    """Lightweight index for admin / manage_skills list_hermes action."""
    root = hermes_dir or default_hermes_skills_dir()
    out: List[Dict[str, str]] = []
    for src in discover_hermes_skills(root):
        desc = ""
        try:
            with open(src.skill_md_path, encoding="utf-8", errors="replace") as f:
                head = f.read(800)
            # crude description from frontmatter
            for line in head.splitlines():
                if line.lower().startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip("\"'")
                    break
        except OSError:
            pass
        out.append({
            "name": src.name,
            "category": src.category,
            "description": desc,
            "path": src.skill_md_path,
        })
    return out
