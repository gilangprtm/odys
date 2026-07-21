"""matcher.py — Simple hook matcher for tool name + file path pattern."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class MatchResult:
    """Result of matching a hook condition against a tool call."""
    matched: bool
    reason: str = ""


def match_hook(matcher_expr: str, tool_name: str, tool_args: str = "") -> MatchResult:
    """Evaluate a hook matcher expression against a tool call.

    Supported syntax (ECC-compatible subset):
      tool == "Write"       → exact match on tool_name
      tool == "Bash" && tool_input.command matches "npm"
      tool_input.path ends_with ".py"
      tool_input.path starts_with "/tmp/"
      *                     → wildcard, matches everything

    Returns MatchResult with matched=True/False and a reason string.
    """
    if not matcher_expr or matcher_expr.strip() == "*":
        return MatchResult(matched=True, reason="wildcard")

    expr = matcher_expr.strip()

    # Split on && (AND clauses)
    clauses = [c.strip() for c in expr.split("&&")]

    for clause in clauses:
        if not clause:
            continue

        # Pattern 1: tool == "Name"
        m = re.match(r'tool\s*==\s*"(.+)"', clause)
        if m:
            expected = m.group(1)
            if tool_name != expected:
                return MatchResult(matched=False, reason=f"tool != {expected}")

        # Pattern 2: tool matches "regex"
        m = re.match(r'tool\s+matches\s+"(.+)"', clause)
        if m:
            pattern = m.group(1)
            if not re.search(pattern, tool_name):
                return MatchResult(matched=False, reason=f"tool !~ {pattern}")

        # Pattern 3: tool_input.path ends_with "suffix"
        m = re.match(r'tool_input\.path\s+ends_with\s+"(.+)"', clause)
        if m:
            suffix = m.group(1)
            if not tool_args.endswith(suffix):
                return MatchResult(matched=False, reason=f"path !ends_with {suffix}")

        # Pattern 4: tool_input.path starts_with "prefix"
        m = re.match(r'tool_input\.path\s+starts_with\s+"(.+)"', clause)
        if m:
            prefix = m.group(1)
            if not tool_args.startswith(prefix):
                return MatchResult(matched=False, reason=f"path !starts_with {prefix}")

        # Pattern 5: tool_input.path matches "regex"
        m = re.match(r'tool_input\.path\s+matches\s+"(.+)"', clause)
        if m:
            pattern = m.group(1)
            if not re.search(pattern, tool_args):
                return MatchResult(matched=False, reason=f"path !~ {pattern}")

        # Pattern 6: tool_input.command matches "regex"
        m = re.match(r'tool_input\.(?:command|content)\s+matches\s+"(.+)"', clause)
        if m:
            pattern = m.group(1)
            if not re.search(pattern, tool_args):
                return MatchResult(matched=False, reason=f"args !~ {pattern}")

    return MatchResult(matched=True, reason=f"all conditions met")
