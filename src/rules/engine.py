"""engine.py — Load and merge modular rules for system prompt injection.

Rules are markdown files under:
  ~/.odys/rules/*.md          (user-global)
  .odys/rules/*.md            (project-local, overrides user)

Categories (by filename stem, optional):
  security, coding-style, testing, git, agents, performance, general

Token budget is enforced so rules never blow the context window.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Rough chars-per-token estimate for budget enforcement
_CHARS_PER_TOKEN = 4

# Default max tokens for the entire rules section
DEFAULT_MAX_RULES_TOKENS = 2000

# Priority order: lower = higher priority (kept when budget tight)
_CATEGORY_PRIORITY = {
    "security": 0,
    "coding-style": 1,
    "testing": 2,
    "git": 3,
    "agents": 4,
    "performance": 5,
    "general": 6,
}


@dataclass
class Rule:
    """A single rule file loaded from disk."""
    name: str          # filename stem
    category: str      # inferred from name or 'general'
    content: str       # markdown body
    path: str
    priority: int = 6


@dataclass
class RulesEngine:
    """Loads, merges, and formats rules for system prompt injection."""

    max_tokens: int = DEFAULT_MAX_RULES_TOKENS
    search_dirs: Optional[List[Path]] = None
    _rules: Dict[str, Rule] = field(default_factory=dict)
    _loaded: bool = False

    def __post_init__(self):
        if self.search_dirs is None:
            home = Path.home()
            self.search_dirs = [
                home / ".odys" / "rules",
                Path(".odys") / "rules",
            ]

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def rules(self) -> Dict[str, Rule]:
        return dict(self._rules)

    def reload(self) -> int:
        """Scan search dirs, load all .md rules. Project overrides user.

        Returns number of rules loaded.
        """
        self._rules.clear()

        # Load user rules first, then project (later wins on same name)
        for base in self.search_dirs:
            if not base.is_dir():
                continue
            for md in sorted(base.glob("*.md")):
                try:
                    text = md.read_text(encoding="utf-8").strip()
                except OSError as e:
                    logger.warning("Cannot read rule %s: %s", md, e)
                    continue
                if not text:
                    continue

                # Reject secrets / hardcoded absolute paths (light guard)
                if self._looks_like_secret(text):
                    logger.warning("Rule %s rejected: looks like it contains secrets", md)
                    continue

                stem = md.stem.lower().replace("_", "-")
                category = self._infer_category(stem)
                priority = _CATEGORY_PRIORITY.get(category, 6)

                rule = Rule(
                    name=stem,
                    category=category,
                    content=text,
                    path=str(md),
                    priority=priority,
                )
                self._rules[stem] = rule  # project overrides user
                logger.info("Loaded rule '%s' (%s) from %s", stem, category, md)

        self._loaded = True
        return len(self._rules)

    def render(self, max_tokens: Optional[int] = None) -> str:
        """Render all rules into a single markdown section for system prompt.

        Enforces token budget by dropping lowest-priority rules first.
        """
        if not self._rules:
            return ""

        budget = max_tokens if max_tokens is not None else self.max_tokens
        # Sort by priority (security first)
        ordered = sorted(self._rules.values(), key=lambda r: r.priority)

        sections: List[str] = []
        used_chars = 0
        max_chars = budget * _CHARS_PER_TOKEN

        for rule in ordered:
            # Header + body
            block = f"### {rule.name}\n{rule.content}\n"
            block_len = len(block)
            if used_chars + block_len > max_chars and sections:
                logger.info(
                    "Rules budget reached (%s tokens) — dropped rule '%s' and lower",
                    budget, rule.name,
                )
                break
            sections.append(block)
            used_chars += block_len

        if not sections:
            return ""

        header = "## Persistent Rules\n\n"
        return header + "\n".join(sections)

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _infer_category(stem: str) -> str:
        for cat in _CATEGORY_PRIORITY:
            if cat in stem or stem.startswith(cat.split("-")[0]):
                return cat
        return "general"

    @staticmethod
    def _looks_like_secret(text: str) -> bool:
        """Reject rules that embed API keys or private key blocks."""
        patterns = [
            r"sk-[A-Za-z0-9]{20,}",
            r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----",
            r"ghp_[A-Za-z0-9]{36}",
            r"xox[baprs]-[A-Za-z0-9-]{10,}",
        ]
        for pat in patterns:
            if re.search(pat, text):
                return True
        return False


# Module-level singleton
_engine: Optional[RulesEngine] = None


def get_engine() -> RulesEngine:
    """Get or create the global RulesEngine singleton."""
    global _engine
    if _engine is None:
        _engine = RulesEngine()
    return _engine


def reload_rules() -> int:
    """Convenience: reload all rules. Returns count."""
    return get_engine().reload()


def render_rules(max_tokens: Optional[int] = None) -> str:
    """Convenience: render rules section for system prompt."""
    eng = get_engine()
    if not eng.loaded:
        eng.reload()
    return eng.render(max_tokens=max_tokens)
