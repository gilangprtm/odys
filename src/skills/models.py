"""models.py — Skill schema and dataclass for ECC-compatible skills."""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

@dataclass
class Skill:
    """Represents an ECC-compatible skill loaded from a directory containing SKILL.md."""
    name: str
    description: str
    origin: str = "ecc@^2.0"
    tools: List[str] = field(default_factory=list)
    when: str = ""
    content: str = ""  # The full markdown body of SKILL.md
    raw_frontmatter: Dict[str, Any] = field(default_factory=dict)
    path: Optional[str] = None  # File path to SKILL.md

    def to_tool_schema(self) -> Dict[str, Any]:
        """Convert this skill into an OpenAI-compatible function schema."""
        # Clean name for schema (only alphanumeric and underscores)
        safe_name = f"skill_{self.name.lower().replace('-', '_').replace(' ', '_')}"
        return {
            "type": "function",
            "function": {
                "name": safe_name,
                "description": f"[SKILL] {self.description}. Trigger when: {self.when}",
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
