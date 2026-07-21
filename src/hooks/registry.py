"""registry.py — Hook registry: load hooks.json, match, execute.

Supports two hook types (ECC-compatible):
  - PreToolUse: sync, runs before tool dispatch, can block with message
  - PostToolUse: async, runs after tool completes, fire-and-forget

Config location:
  ~/.odys/hooks/hooks.json
  .odys/hooks/hooks.json (project override)
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.hooks.matcher import match_hook, MatchResult

logger = logging.getLogger(__name__)

HOOK_TIMEOUT = 10  # seconds


@dataclass
class HookAction:
    """A single action inside a hook."""
    type: str  # "command" | "python" | "block"
    command: str = ""
    code: str = ""
    message: str = ""
    timeout: int = HOOK_TIMEOUT


@dataclass
class Hook:
    """A single configured hook with event type, matcher, and actions."""
    event: str        # "PreToolUse" | "PostToolUse" | "Stop"
    matcher: str      # matcher expression
    actions: List[HookAction] = field(default_factory=list)
    _match_cache: dict = field(default_factory=dict)


@dataclass
class HookResult:
    hook_name: str
    blocked: bool = False
    block_message: str = ""
    errors: List[str] = field(default_factory=list)


class HookRegistry:
    """Loads, matches, and executes hooks from hook configuration files."""

    def __init__(self, config_dirs: Optional[List[Path]] = None):
        self._hooks: Dict[str, List[Hook]] = {
            "PreToolUse": [],
            "PostToolUse": [],
            "Stop": [],
        }
        self._loaded = False

        if config_dirs is None:
            self._config_dirs = [
                Path.home() / ".odys" / "hooks",
                Path(".odys") / "hooks",
            ]
        else:
            self._config_dirs = config_dirs

    @property
    def loaded(self) -> bool:
        return self._loaded

    def reload(self) -> int:
        """Load hooks from config files. Returns total hook count."""
        self._hooks = {k: [] for k in self._hooks}
        total = 0

        # Scan config locations (user first, project overrides)
        for cfg_dir in self._config_dirs:
            cfg_file = cfg_dir / "hooks.json"
            if not cfg_file.is_file():
                continue
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Cannot load hooks from %s: %s", cfg_file, e)
                continue

            for event_type in ("PreToolUse", "PostToolUse", "Stop"):
                entries = data.get(event_type, [])
                for entry in entries:
                    matcher_expr = entry.get("matcher", "*")
                    raw_actions = entry.get("hooks", [])
                    actions = []

                    for act in raw_actions:
                        if isinstance(act, str):
                            actions.append(HookAction(type="command", command=act))
                        elif isinstance(act, dict):
                            act_type = act.get("type", "command")
                            if act_type == "command":
                                actions.append(HookAction(
                                    type="command",
                                    command=act.get("command", ""),
                                    timeout=act.get("timeout", HOOK_TIMEOUT),
                                ))
                            elif act_type == "block":
                                actions.append(HookAction(
                                    type="block",
                                    message=act.get("message", "Blocked by hook"),
                                ))

                    if actions:
                        hook = Hook(event=event_type, matcher=matcher_expr, actions=actions)
                        self._hooks[event_type].append(hook)
                        total += 1
                        logger.debug("Hook %s: %s", event_type, matcher_expr)

        self._loaded = True
        logger.info("Loaded %s hooks", total)
        return total

    def run_pre_tool(self, tool_name: str, tool_args: str = "") -> Optional[HookResult]:
        """Run PreToolUse hooks. Returns HookResult if any hook blocks the tool."""
        for hook in self._hooks.get("PreToolUse", []):
            result = match_hook(hook.matcher, tool_name, tool_args)
            if not result.matched:
                continue

            for action in hook.actions:
                if action.type == "block":
                    logger.info("Hook blocked %s: %s", tool_name, action.message)
                    return HookResult(
                        hook_name=hook.matcher,
                        blocked=True,
                        block_message=action.message,
                    )
                elif action.type == "command":
                    try:
                        subprocess.run(
                            action.command,
                            shell=True,
                            timeout=action.timeout,
                            capture_output=True,
                            text=True,
                        )
                    except subprocess.TimeoutExpired:
                        logger.warning("Hook command timed out: %s", action.command)
                    except Exception as e:
                        logger.warning("Hook command failed: %s", e)

        return None

    async def run_post_tool(self, tool_name: str, tool_args: str = "", tool_result: Any = None) -> List[HookResult]:
        """Run PostToolUse hooks asynchronously."""
        results: List[HookResult] = []
        for hook in self._hooks.get("PostToolUse", []):
            result = match_hook(hook.matcher, tool_name, tool_args)
            if not result.matched:
                continue

            hook_result = HookResult(hook_name=hook.matcher)
            for action in hook.actions:
                if action.type == "command":
                    try:
                        proc = await asyncio.create_subprocess_shell(
                            action.command,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        try:
                            _, stderr = await asyncio.wait_for(
                                proc.communicate(), timeout=action.timeout
                            )
                            if proc.returncode != 0 and stderr:
                                hook_result.errors.append(
                                    f"exit={proc.returncode}: {stderr.decode()[:200]}"
                                )
                        except asyncio.TimeoutError:
                            proc.kill()
                            hook_result.errors.append("timeout")
                    except Exception as e:
                        hook_result.errors.append(str(e))
            results.append(hook_result)
        return results


# Module-level singleton
_registry: Optional[HookRegistry] = None


def get_registry() -> HookRegistry:
    global _registry
    if _registry is None:
        _registry = HookRegistry()
    return _registry


def reload_hooks() -> int:
    return get_registry().reload()
