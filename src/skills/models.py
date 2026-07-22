"""models.py — Skill schema and dataclass for ECC-compatible skills."""

import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


def _parse_origin_version(origin: str) -> tuple:
    """Parse 'ecc@^2.0' into (package, version_spec).
    Returns ('ecc', '^2.0') or ('', '')."""
    if "@" in origin:
        pkg, spec = origin.split("@", 1)
        return pkg.strip(), spec.strip()
    return origin.strip(), ""


def _version_satisfies(version: str, spec: str) -> bool:
    """Simple semver check: '2.0.1' satisfies '^2.0' (major match).
    Handles ^ (caret), ~ (tilde), exact, and empty spec."""
    if not spec or not version:
        return True  # no constraint
    # strip leading ^ or ~
    clean_spec = spec.lstrip("^~")
    parts_v = [int(p) for p in version.split(".") if p.isdigit()]
    parts_s = [int(p) for p in clean_spec.split(".") if p.isdigit()]
    if not parts_v or not parts_s:
        return True  # can't compare, allow
    if spec.startswith("^"):
        # major must match
        return parts_v[0] == parts_s[0]
    elif spec.startswith("~"):
        # major+minor must match
        return parts_v[:2] == parts_s[:2]
    else:
        # exact match
        return parts_v[:len(parts_s)] == parts_s


# Known compatible version
_ODYS_ECC_VERSION = "2.0"


@dataclass
class Skill:
    """Represents an ECC-compatible skill loaded from a directory containing SKILL.md."""
    name: str
    description: str
    origin: str = "ecc@^2.0"
    version: str = ""           # resolved version
    tools: List[str] = field(default_factory=list)
    when: str = ""
    content: str = ""           # The full markdown body of SKILL.md
    raw_frontmatter: Dict[str, Any] = field(default_factory=dict)
    path: Optional[str] = None  # File path to SKILL.md
    requires: List[str] = field(default_factory=list)  # FR-1.5: dependent skill names
    version_ok: bool = True     # FR-1.6: passed version check

    def __post_init__(self):
        """FR-1.6: Validate origin version on load."""
        if self.origin:
            pkg, spec = _parse_origin_version(self.origin)
            if pkg == "ecc":
                self.version_ok = _version_satisfies(_ODYS_ECC_VERSION, spec)

    def to_tool_schema(self) -> Dict[str, Any]:
        """Convert this skill into an OpenAI-compatible function schema."""
        safe_name = f"skill_{self.name.lower().replace('-', '_').replace(' ', '_')}"
        desc = f"[SKILL] {self.description}"
        if not self.version_ok:
            desc += f" [WARN: version mismatch for origin={self.origin}]"
        return {
            "type": "function",
            "function": {
                "name": safe_name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "instruction": {
                            "type": "string",
                            "description": "Specific instruction or task details to execute within this skill context"
                        }
                    },
                    "required": ["instruction"]
                }
            }
        }
