"""agent_definitions.py — Load ECC agent persona definitions from markdown files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentDefinition:
    def __init__(self, name: str, instructions: str, path: Path):
        self.name = name
        self.instructions = instructions
        self.path = path


class AgentRegistry:
    def __init__(self, search_dirs: Optional[List[Path]] = None):
        self._agents: Dict[str, AgentDefinition] = {}
        self._loaded = False

        if search_dirs is None:
            home = Path.home()
            _this_dir = Path(__file__).resolve().parent
            _repo_root = _this_dir.parent.parent  # src/agent_tools/ -> src/ -> repo root
            self._search_dirs = [
                home / ".odys" / "agents",
                Path(".odys") / "agents",
                _repo_root / "ecc-skills" / "agents",
            ]
        else:
            self._search_dirs = search_dirs

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def agents(self) -> Dict[str, AgentDefinition]:
        return dict(self._agents)

    def reload(self) -> int:
        """Scan directories and load markdown agent persona definitions."""
        self._agents.clear()
        total = 0

        for base in self._search_dirs:
            if not base.is_dir():
                continue

            for agent_file in base.glob("*.md"):
                name = agent_file.stem
                if name in self._agents:
                    continue  # precedence

                try:
                    content = agent_file.read_text(encoding="utf-8")
                    self._agents[name] = AgentDefinition(
                        name=name,
                        instructions=content,
                        path=agent_file,
                    )
                    total += 1
                except Exception as e:
                    logger.warning("Failed to load agent file %s: %s", agent_file, e)

        self._loaded = True
        logger.info("Loaded %s agent definitions", total)
        return total

    def get_agent(self, name: str) -> Optional[AgentDefinition]:
        # Support slug formats (e.g. 'code-reviewer' or 'code_reviewer')
        normalized = name.lower().replace("_", "-")
        return self._agents.get(normalized)


# Singleton
_registry: Optional[AgentRegistry] = None


def get_agent_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
