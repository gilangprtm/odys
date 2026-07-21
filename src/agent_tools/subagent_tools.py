"""subagent_tools.py — agent tool for spawning isolated sub-agents.

Sub-agents run a full stream_agent_loop with their own messages, terminal
session, and toolset. The main agent continues while the sub-agent works;
results are delivered back as tool output.
"""

import asyncio
import json
import logging
import uuid
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


async def delegate_task(
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
) -> Dict:
    """Spawn an isolated sub-agent to work on a task.

    Content:
      Line 1: goal (brief description)
      Line 2+: context (background, constraints, file paths)

    The sub-agent runs with a clean conversation, isolated terminal, and
    a restricted toolset (read-only + web search). Returns the sub-agent's
    final response or an error message.
    """
    lines = content.strip().split("\n", 1)
    goal = lines[0].strip()[:200] if lines else ""
    context = lines[1].strip() if len(lines) > 1 else ""

    if not goal:
        return {"error": "Delegate needs a goal on line 1"}

    try:
        from src.agent_loop.loop import stream_agent_loop
        from src.agent_loop.helpers import _extract_last_user_message
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
        from src.model_context import estimate_tokens
        from src.database import SessionLocal, ModelEndpoint
        from src.endpoint_resolver import resolve_endpoint_runtime, build_headers
        from src.auth_helpers import owner_filter

        # Determine the best available endpoint/model
        db = SessionLocal()
        try:
            query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
            if owner:
                query = owner_filter(query, ModelEndpoint, owner)
            endpoint = query.order_by(ModelEndpoint.position).first()
            if not endpoint:
                return {"error": "No enabled endpoint for sub-agent"}
            base_url, api_key = resolve_endpoint_runtime(endpoint, owner=owner)
            model_name = endpoint.default_model or "auto"
            headers = build_headers(api_key, base_url)
        finally:
            db.close()

        # Build messages for the sub-agent
        system_tools = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
        from src.agent_loop.prompts import _assemble_prompt
        system_prompt = _assemble_prompt(
            tool_names=system_tools,
            disabled_tools=set(),
            compact=True,
            owner=owner,
        )

        sub_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"## Task\n{goal}\n\n## Context\n{context}\n\nWork autonomously and report your findings."},
        ]

        # Run the sub-agent in a new event context
        sub_output: List[str] = []
        sub_tool_events: List[Dict] = []

        async for event in stream_agent_loop(
            endpoint_url=base_url,
            model=model_name,
            messages=sub_messages,
            headers=headers,
            temperature=0.3,
            max_tokens=4096,
            max_rounds=10,
            owner=owner,
            session_id=session_id + "_sub" if session_id else None,
            disabled_tools={"ask_user", "search_chats", "manage_memory", "manage_tasks", "chat_with_model",
                           "ask_teacher", "create_session", "list_sessions", "send_to_session", "manage_session"},
            workload="background",
        ):
            # Parse SSE events
            if event.startswith("data: "):
                payload = event[6:]
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                    if data.get("type") == "metrics":
                        pass  # capture metrics later
                    elif "delta" in data:
                        sub_output.append(data["delta"])
                    elif data.get("type") == "tool_event":
                        sub_tool_events.append(data)
                except (json.JSONDecodeError, TypeError):
                    pass

        final = "".join(sub_output)
        if not final.strip():
            final = f"Task completed with {len(sub_tool_events)} tool call(s)."

        return {"sub_agent": True, "goal": goal, "response": final.strip()[:8000]}

    except Exception as e:
        logger.error(f"delegate_task failed: {e}")
        return {"error": f"Sub-agent failed: {e}", "goal": goal}
