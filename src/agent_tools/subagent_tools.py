"""subagent_tools.py — agent tool for spawning isolated sub-agents.

Supports two modes (ECC-compatible):
1. Single task: goal + context + optional toolsets
2. Batch (parallel): tasks array (up to 3) run concurrently

Each sub-agent gets its own conversation, terminal session, and scoped toolset.
Results are collected and merged.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Set

from src.agent_tools.agent_definitions import get_agent_registry

logger = logging.getLogger(__name__)

# ── constants ──

SUBAGENT_TIMEOUT = 120  # wall-clock seconds per subagent


async def _synthesize_results(
    goal: str,
    results: List[Dict[str, Any]],
    owner: Optional[str] = None,
) -> str:
    """Call a light LLM to aggregate sub-agent findings into a single answer."""
    # TODO: Implement LLM-based synthesis using the same endpoint as subagents.
    # For now, return a simple text merge.
    parts = []
    for r in results:
        g = r.get("goal", "Unknown")
        resp = r.get("response", "")
        err = r.get("error")
        if err:
            parts.append(f"⚠️ **{g}** (error): {err}")
        else:
            parts.append(f"✅ **{g}**: {resp[:500]}")
    return f"## Synthesis for: {goal}\n\n" + "\n\n---\n\n".join(parts)


# Tool scopes — maps readable names to sets of tool names to disable.
TOOL_SCOPES = {
    "read": {"write_file", "edit_file", "ask_user", "manage_skills",
             "manage_tasks", "manage_session", "manage_endpoints",
             "manage_mcp", "manage_webhooks", "manage_tokens",
             "chat_with_model", "ask_teacher", "generate_image",
             "ui_control", "update_plan", "create_session",
             "list_sessions", "send_to_session", "manage_documents"},
    "read-terminal": {"ask_user", "manage_skills", "manage_tasks",
                      "manage_session", "manage_endpoints",
                      "manage_mcp", "manage_webhooks", "manage_tokens",
                      "chat_with_model", "ask_teacher", "generate_image",
                      "ui_control", "update_plan", "create_session",
                      "list_sessions", "send_to_session", "manage_documents"},
    "web-only": {"write_file", "edit_file", "bash", "python",
                 "ask_user", "manage_skills", "manage_tasks",
                 "manage_session", "manage_session", "manage_endpoints",
                 "chat_with_model", "ask_teacher", "manage_documents"},
    "full": set(),  # all tools available
}


def _resolve_toolsets(toolsets: Optional[List[str]]) -> set:
    """Resolve tool scope names to a set of disabled_tools."""
    combined = set()
    if not toolsets:
        # default: read-only (safe)
        return TOOL_SCOPES["read"]
    for ts in toolsets:
        ts = ts.strip().lower()
        if ts in TOOL_SCOPES:
            combined.update(TOOL_SCOPES[ts])
    return combined


async def _run_single_subagent(
    goal: str,
    context: str = "",
    toolsets: Optional[List[str]] = None,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    temperature: float = 0.3,
    max_rounds: int = 10,
    max_tokens: int = 4096,
    role: str = "leaf",
    agent_type: Optional[str] = None,
    timeout: int = 120,  # wall-clock seconds (FR-4.4)
) -> Dict[str, Any]:
    """Run a single sub-agent task and return its result dict."""
    from src.agent_loop.loop import stream_agent_loop
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.database import SessionLocal, ModelEndpoint
    from src.endpoint_resolver import resolve_endpoint_runtime, build_headers
    from src.auth_helpers import owner_filter
    from src.agent_tools.agent_definitions import get_agent_registry

    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if owner:
            query = owner_filter(query, ModelEndpoint, owner)
        endpoint = query.order_by(ModelEndpoint.id).first()
        if not endpoint:
            return {"goal": goal[:80], "error": "No enabled endpoint for sub-agent"}
        base_url, api_key = resolve_endpoint_runtime(endpoint, owner=owner)
        model_name = endpoint.default_model or "auto"
        headers = build_headers(api_key, base_url)
    finally:
        db.close()

    # System prompt
    system_tools = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    from src.agent_loop.prompts import _assemble_prompt
    system_prompt = _assemble_prompt(tool_names=system_tools, compact=True, owner=owner)

    if role == "orchestrator":
        system_prompt += "\n\nYou ARE an orchestrator — you can spawn your own sub-agents."

    # Inject agent instructions from AgentRegistry (ECC ~/agents/<type>.md)
    if agent_type:
        try:
            _agent_reg = get_agent_registry()
            if not _agent_reg.loaded:
                _agent_reg.reload()
            _agent_def = _agent_reg.get_agent(agent_type)
            if _agent_def:
                system_prompt += f"\n\n## You are: {_agent_def.name}\n{_agent_def.instructions}"
        except Exception as _agent_e:
            logger.debug("Agent type '%s' not loaded: %s", agent_type, _agent_e)

    sub_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"## Task\n{goal}\n\n## Context\n{context}\n\nWork autonomously and report your findings."},
    ]

    disabled_tools = _resolve_toolsets(toolsets)
    # leaf agents cannot delegate further
    if role != "orchestrator":
        disabled_tools.add("delegate_task")

    sub_id = session_id + "_sub_" + uuid.uuid4().hex[:6] if session_id else None

    sub_output: List[str] = []
    sub_tool_events: List[Dict] = []
    sub_metrics: Optional[Dict] = None  # NFR-4.2 token tracking

    try:
        _sub_loop = stream_agent_loop(
            endpoint_url=base_url,
            model=model_name,
            messages=sub_messages,
            headers=headers,
            temperature=temperature,
            max_tokens=max_tokens,
            max_rounds=max_rounds,
            owner=owner,
            session_id=sub_id,
            disabled_tools=disabled_tools,
            workload="background",
        )

        async for event in asyncio.wait_for(_sub_loop, timeout=timeout):
            if event.startswith("data: "):
                payload = event[6:]
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                    evt_type = data.get("type")
                    if evt_type == "metrics":
                        sub_metrics = data.get("data", {})
                    elif "delta" in data:
                        sub_output.append(data["delta"])
                    elif evt_type == "tool_event":
                        sub_tool_events.append(data)
                except (json.JSONDecodeError, TypeError):
                    pass

        final = "".join(sub_output)
        if not final.strip():
            final = f"Task completed with {len(sub_tool_events)} tool call(s)."

        result: Dict[str, Any] = {
            "goal": goal[:200],
            "response": final.strip()[:8000],
            "tool_calls": len(sub_tool_events),
        }

        # Token accounting (NFR-4.2)
        if sub_metrics:
            result["input_tokens"] = sub_metrics.get("input_tokens", 0)
            result["output_tokens"] = sub_metrics.get("output_tokens", 0)

        return result
    except asyncio.TimeoutError:
        logger.warning("Sub-agent '%s' timed out after %ss", goal[:40], timeout)
        return {"goal": goal[:200], "error": f"Timed out after {timeout}s"}
    except Exception as e:
        logger.error("Sub-agent '%s' failed: %s", goal[:60], e)
        return {"goal": goal[:200], "error": str(e)[:500]}


async def delegate_task(
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Spawn one or more isolated sub-agents.

    Content (single task mode):
      Line 1: goal
      Line 2+: context

    Content (batch mode):
      Full JSON object: {"tasks": [...], "toolsets": [...]}
    """
    if not content or not content.strip():
        return {"error": "delegate_task needs a goal or tasks"}

    stripped = content.strip()

    # Detect batch mode (starts with {)
    if stripped.startswith("{"):
        try:
            args = json.loads(stripped)
        except json.JSONDecodeError:
            return {"error": "Batch mode requires valid JSON: {\"tasks\": [{\"goal\": \"...\"}]}"}

        tasks_spec = args.get("tasks", [])
        toolsets = args.get("toolsets") or []
        if not tasks_spec or not isinstance(tasks_spec, list):
            return {"error": "Batch mode expects tasks array with at least one item"}

        if len(tasks_spec) > 3:
            tasks_spec = tasks_spec[:3]

        results = await asyncio.gather(*[
            _run_single_subagent(
                goal=t.get("goal", "Untitled"),
                context=t.get("context", ""),
                toolsets=toolsets,
                session_id=session_id,
                owner=owner,
                role=t.get("role", "leaf"),
                agent_type=t.get("agent") or t.get("type"),
                timeout=t.get("timeout", SUBAGENT_TIMEOUT),
            )
            for t in tasks_spec
        ], return_exceptions=True)

        # Replace exceptions with error dicts
        final_results = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning(f"Sub-agent task {i} failed with exception: {r}")
                final_results.append({
                    "goal": tasks_spec[i].get("goal", "Untitled"),
                    "output": str(r),
                    "error": True,
                })
            else:
                final_results.append(r)

        # Synthesize combined answer (FR-4.5)
        combined_answer = await _synthesize_results(
            goal=tasks_spec[0].get("goal", "Batch task"),
            results=final_results,
            owner=owner,
        )

        return {
            "sub_agent": True,
            "batch": True,
            "count": len(final_results),
            "results": final_results,
            "synthesis": combined_answer,
        }

    # Single task mode
    lines = stripped.split("\n", 1)
    goal = lines[0].strip()[:200] if lines else ""
    context = lines[1].strip() if len(lines) > 1 else ""

    if not goal:
        return {"error": "Delegate needs a goal on line 1"}

    # Also allow JSON single task with agent field
    agent_type = None
    if stripped.startswith("{"):
        try:
            single_args = json.loads(stripped)
            goal = single_args.get("goal", goal)
            context = single_args.get("context", context)
            agent_type = single_args.get("agent") or single_args.get("type")
        except json.JSONDecodeError:
            pass

    result = await _run_single_subagent(
        goal=goal,
        context=context,
        session_id=session_id,
        owner=owner,
        agent_type=agent_type,
    )

    return {
        "sub_agent": True,
        "goal": result.get("goal", goal),
        "response": result.get("response", ""),
        "error": result.get("error"),
    }
