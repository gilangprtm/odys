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
            "UserPromptSubmit": [],
            "PreCompact": [],
            "Notification": [],
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

            for event_type in ("PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit", "PreCompact", "Notification"):
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

    async def run_user_prompt_submit(self, user_message: str, session_id: Optional[str] = None) -> List[str]:
        """Run UserPromptSubmit hooks before the prompt enters the LLM.

        ECC use: validation, pre-load checks, guardrails before model sees input.
        Returns list of error messages; if any returned, the prompt should be blocked.
        """
        errors: List[str] = []
        for hook in self._hooks.get("UserPromptSubmit", []):
            result = match_hook(hook.matcher, "UserPromptSubmit", user_message)
            if not result.matched:
                continue
            for action in hook.actions:
                if action.type == "block":
                    logger.info("UserPromptSubmit hook blocked: %s", action.message)
                    errors.append(action.message)
                elif action.type == "command":
                    try:
                        proc = await asyncio.create_subprocess_shell(
                            action.command.format(message=user_message),
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=action.timeout)
                        if proc.returncode != 0 and stderr:
                            err = stderr.decode()[:200]
                            logger.warning("UserPromptSubmit hook error: %s", err)
                    except asyncio.TimeoutError:
                        logger.warning("UserPromptSubmit hook timed out")
                    except Exception as e:
                        logger.warning("UserPromptSubmit hook failed: %s", e)
        return errors

    async def run_stop(self, summary: Optional[str] = None) -> List[HookResult]:
        """Run Stop hooks when the LLM finishes responding.

        ECC use: auto-save, log summary, trigger downstream workflows.
        """
        results: List[HookResult] = []
        for hook in self._hooks.get("Stop", []):
            result = match_hook(hook.matcher, "Stop", summary or "")
            if not result.matched:
                continue
            hook_result = HookResult(hook_name=hook.matcher)
            for action in hook.actions:
                if action.type == "command":
                    try:
                        proc = await asyncio.create_subprocess_shell(
                            action.command.format(summary=summary or ""),
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=action.timeout)
                        if proc.returncode != 0 and stderr:
                            hook_result.errors.append(stderr.decode()[:200])
                    except asyncio.TimeoutError:
                        hook_result.errors.append("timeout")
                    except Exception as e:
                        hook_result.errors.append(str(e))
            results.append(hook_result)
        return results

    async def run_pre_compact(self, context_stats: Optional[dict] = None) -> List[str]:
        """Run PreCompact hooks before context window compaction.

        ECC use: persist important state, save checkpoints, trim non-essential data.
        Returns list of actions to take (e.g. "save_session", "summarize").
        """
        actions_taken: List[str] = []
        raw = json.dumps(context_stats or {})
        for hook in self._hooks.get("PreCompact", []):
            result = match_hook(hook.matcher, "PreCompact", raw)
            if not result.matched:
                continue
            for action in hook.actions:
                if action.type == "block":
                    actions_taken.append(f"PreCompact blocked: {action.message}")
                elif action.type == "command":
                    try:
                        subprocess.run(
                            action.command.format(tokens=str(context_stats or {})),
                            shell=True,
                            timeout=action.timeout,
                            capture_output=True,
                            text=True,
                        )
                        actions_taken.append(f"precompact: {action.command[:60]}")
                    except subprocess.TimeoutExpired:
                        logger.warning("PreCompact hook timed out")
                    except Exception as e:
                        logger.warning("PreCompact hook failed: %s", e)
        return actions_taken

    async def run_notification(self, notification_type: str, payload: Any = None) -> List[str]:
        """Run Notification hooks for permission requests, approvals, etc.

        ECC use: user-facing permission requests (tool approval, data access).
        Returns list of approval strings ("approved", "denied") or errors.
        """
        results: List[str] = []
        raw = json.dumps({"type": notification_type, "payload": payload}) if payload else notification_type
        for hook in self._hooks.get("Notification", []):
            result = match_hook(hook.matcher, notification_type, str(payload or ""))
            if not result.matched:
                continue
            for action in hook.actions:
                if action.type == "block":
                    results.append(f"blocked: {action.message}")
                elif action.type == "command":
                    try:
                        proc = await asyncio.create_subprocess_shell(
                            action.command.format(payload=raw),
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=action.timeout)
                        if proc.returncode != 0 and stderr:
                            results.append(f"error: {stderr.decode()[:200]}")
                        else:
                            results.append("approved")
                    except asyncio.TimeoutError:
                        results.append("timeout")
                    except Exception as e:
                        results.append(f"error: {e}")
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
