"""loader.py — Parse ECC SKILL.md files (YAML frontmatter + markdown body)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import yaml

from src.skills.models import Skill

logger = logging.getLogger(__name__)

# Match YAML frontmatter between --- fences at the start of a file.
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z",
    re.DOTALL,
)


def parse_skill_md(text: str, path: Optional[str] = None) -> Optional[Skill]:
    """Parse a SKILL.md string into a Skill dataclass.

    Expected format (ECC-compatible)::

        ---
        name: tdd-workflow
        description: Test-Driven Development cycle
        origin: ecc@^2.0
        tools: [bash, read_file, write_file]
        when: "User asks to implement a feature with tests first"
        ---
        # TDD Workflow
        ...
    """
    if not text or not text.strip():
        logger.warning("Empty skill content at %s", path)
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        logger.warning("No YAML frontmatter found in %s", path)
        return None

    raw_yaml, body = match.group(1), match.group(2)
    try:
        meta = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as e:
        logger.warning("Invalid YAML frontmatter in %s: %s", path, e)
        return None

    if not isinstance(meta, dict):
        logger.warning("Frontmatter is not a mapping in %s", path)
        return None

    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        logger.warning("Skill missing name or description in %s", path)
        return None

    tools_raw = meta.get("tools") or []
    if isinstance(tools_raw, str):
        tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
    elif isinstance(tools_raw, list):
        tools = [str(t).strip() for t in tools_raw if str(t).strip()]
    else:
        tools = []

    return Skill(
        name=name,
        description=description,
        origin=str(meta.get("origin") or "ecc@^2.0"),
        tools=tools,
        when=str(meta.get("when") or ""),
        content=body.strip(),
        raw_frontmatter=meta,
        path=path,
    )


def load_skill_file(path: Path) -> Optional[Skill]:
    """Load and parse a single SKILL.md file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Cannot read skill file %s: %s", path, e)
        return None
    return parse_skill_md(text, path=str(path))


def discover_skills(search_dirs: list[Path]) -> list[Skill]:
    """Discover all SKILL.md files under the given directories.

    Supports two layouts:
    - ``dir/my-skill/SKILL.md``  (ECC standard: skill in subdirectory)
    - ``dir/my-skill.md``        (flat: single markdown file)
    """
    skills: list[Skill] = []
    seen_names: set[str] = set()

    for base in search_dirs:
        if not base.is_dir():
            continue

        # Layout 1: subdirectories with SKILL.md
        for skill_md in base.glob("*/SKILL.md"):
            skill = load_skill_file(skill_md)
            if skill and skill.name not in seen_names:
                skills.append(skill)
                seen_names.add(skill.name)
                logger.info("Loaded skill '%s' from %s", skill.name, skill_md)

        # Layout 2: flat .md files (skip SKILL.md already handled)
        for md_file in base.glob("*.md"):
            if md_file.name.upper() == "SKILL.MD":
                continue
            skill = load_skill_file(md_file)
            if skill and skill.name not in seen_names:
                skills.append(skill)
                seen_names.add(skill.name)
                logger.info("Loaded skill '%s' from %s", skill.name, md_file)

    return skills
