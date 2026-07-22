"""registry.py — Load, validate, and register skills as agent tools."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Set

from src.skills.models import Skill
from src.skills.loader import discover_skills

logger = logging.getLogger(__name__)

# Canonical set of Odys tools that skills can declare.
# Includes ECC-capitalised aliases for compatibility.
_ODYS_TOOLS = frozenset({
    "bash", "python", "read_file", "write_file", "edit_file",
    "grep", "glob", "ls", "web_search", "web_fetch",
    "create_document", "update_document", "edit_document",
    "manage_documents", "get_workspace", "ask_user", "update_plan",
    "manage_bg_jobs", "manage_tasks", "manage_skills",
    "manage_memory", "manage_endpoints", "search_chats",
    "delegate_task", "chat_with_model", "ask_teacher", "list_models",
    # ECC capitalized aliases
    "Read", "Write", "Edit", "Bash", "Grep", "Glob", "Task",
    "Search", "Fetch", "List", "Execute",
})


class SkillRegistry:
    """Loads, caches, and provides access to ECC-compatible skills."""

    def __init__(self, search_dirs: Optional[List[Path]] = None):
        self._skills: Dict[str, Skill] = {}         # name → Skill
        self._tool_map: Dict[str, Skill] = {}        # skill_<name> → Skill
        self._loaded = False
        self._mtime_cache: dict = {}  # NFR-1.3

        # Default search paths
        if search_dirs is None:
            home = Path.home()
            # Bundled ECC skills shipped with Odys (git submodule)
            _this_dir = Path(__file__).resolve().parent
            _repo_root = _this_dir.parent.parent  # src/skills/ -> src/ -> repo root
            _bundled = _repo_root / "ecc-skills" / "skills"
            search_dirs = [
                home / ".odys" / "skills",
                Path(".odys") / "skills",
                _bundled,
            ]

        self._search_dirs = search_dirs

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def skills(self) -> Dict[str, Skill]:
        return dict(self._skills)

    @property
    def tool_names(self) -> Set[str]:
        return set(self._tool_map.keys())

    def reload(self) -> int:
        """Scan directories, parse skills, validate tools. Returns count."""
        discovered = discover_skills(self._search_dirs)

        self._skills.clear()
        self._tool_map.clear()
        self._mtime_cache.clear()  # NFR-1.3

        for skill in discovered:
            # Validate declared tools
            for tool in skill.tools:
                if tool not in _ODYS_TOOLS:
                    logger.warning(
                        "Skill '%s' declares unknown tool '%s' — may fail at runtime",
                        skill.name, tool,
                    )

            self._skills[skill.name] = skill
            safe_name = f"skill_{skill.name.lower().replace('-', '_').replace(' ', '_')}"
            self._tool_map[safe_name] = skill
            logger.info("Registered skill '%s' as tool '%s'", skill.name, safe_name)

        self._loaded = True
        return len(discovered)

    def hot_reload_if_changed(self) -> int:
        """NFR-1.3: Check mtime of all skill files, reload if any changed. Returns 0 if no change, >0 if reloaded."""
        from src.skills.loader import load_skill_file
        dirty = False
        for base in self._search_dirs:
            if not base.is_dir():
                continue
            for skill_md in base.glob("*/SKILL.md"):
                mtime = skill_md.stat().st_mtime
                key = str(skill_md.resolve())
                if self._mtime_cache.get(key) != mtime:
                    self._mtime_cache[key] = mtime
                    dirty = True
            for md_file in base.glob("*.md"):
                if md_file.name.upper() == "SKILL.MD":
                    continue
                mtime = md_file.stat().st_mtime
                key = str(md_file.resolve())
                if self._mtime_cache.get(key) != mtime:
                    self._mtime_cache[key] = mtime
                    dirty = True
        if dirty:
            n = self.reload()
            return n
        return 0

    def get_skill_by_name(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def get_skill_by_tool_name(self, tool_name: str) -> Optional[Skill]:
        return self._tool_map.get(tool_name)

    def get_all_schemas(self) -> List[dict]:
        """Return list of OpenAI-compatible function schemas for all skills."""
        return [skill.to_tool_schema() for skill in self._skills.values()]

    async def execute_skill(self, skill_tool_name: str, content: str) -> dict:
            """Execute a skill — run its instruction as a user prompt in context.

            For now, this returns the skill content + instruction as a structured
            result that the agent loop injects into messages. This is a V1 approach;
            future versions will use a dedicated sub-agent or skill loop.

            FR-1.5: If the skill declares 'requires' dependencies, prepend those
            skill contents as context.
            """
            skill = self._tool_map.get(skill_tool_name)
            if not skill:
                return {"error": f"Skill '{skill_tool_name}' not loaded", "exit_code": 1}

            # FR-1.5: Compose required skills
            composed_parts = []
            for req_name in skill.requires:
                req_skill = self._skills.get(req_name)
                if req_skill:
                    composed_parts.append(f"## Required Skill: {req_skill.name}\n{req_skill.content}")

            # Format the skill body with the user instruction
            body = skill.content or ""
            instruction = content or ""

            lines = [
                f"## Skill: {skill.name}",
                "",
            ]
            if composed_parts:
                lines.extend(["\n---\n".join(composed_parts), "", "---", ""])
            lines.append(body)
            if instruction:
                lines.extend([
                    "",
                    f"**User instruction:** {instruction}",
                ])

            return {
                "skill": True,
                "skill_name": skill.name,
                "output": "\n".join(lines),
                "exit_code": 0,
            }


# Module-level singleton (lazy init)
_registry: Optional[SkillRegistry] = None


def get_registry() -> SkillRegistry:
    """Get or create the global SkillRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry


def reload_skills() -> int:
    """Convenience: reload all skills from search dirs. Returns count."""
    reg = get_registry()
    return reg.reload()
