"""Tool resolution, result append, metrics, verifier, fallbacks."""

import asyncio
import collections
import json
import os
import re
import time
import logging
from typing import AsyncGenerator, List, Dict, Optional, Set
from urllib.parse import urlparse

from src.llm_core import (
    stream_llm,
    stream_llm_with_fallback,
    _is_ollama_native_url,
)
from src.model_context import estimate_tokens
from src.settings import get_setting
from src.prompt_security import untrusted_context_message
from src.tool_security import blocked_tools_for_owner, plan_mode_disabled_tools
from src.tool_policy import GUIDE_ONLY_DIRECTIVE, WEB_TOOL_NAMES, ToolPolicy
from src.tool_utils import _truncate, get_mcp_manager
from src.agent_tools import (
    parse_tool_blocks,
    strip_tool_blocks,
    execute_tool_block,
    format_tool_result,
    set_active_document,
    set_active_model,
    function_call_to_tool_block,
    FUNCTION_TOOL_SCHEMAS,
    TOOL_TAGS,
    ToolBlock,
    MAX_AGENT_ROUNDS,
)

from src.agent_loop.prompts import (
    TOOL_SECTIONS,
    get_builtin_overrides,
    _assemble_prompt,
    AGENT_SYSTEM_PROMPT,
    _API_HOSTS,
    _MCP_KEYWORDS,
    _ADMIN_SCHEMA_NAMES,
    _DOMAIN_TOOL_MAP,
    _domain_rules_for_tools,
)

from src.agent_loop.helpers import (
    _is_ollama_openai_compat_url,
    _is_local_openai_compat_url,
    _endpoint_lookup_keys,
    _detect_admin_intent,
    _extract_last_user_message,
    _user_turn_count,
    _insert_before_latest_user,
    _uploaded_files_context_message,
    _strip_think_blocks,
    _is_explicit_continuation,
    _is_casual_low_signal,
    _is_contextual_retry_continuation,
    _assistant_requested_followup,
    _classify_agent_request,
    _turn_targets_active_document,
    _is_email_document_obj,
    _minimal_saved_memory_message,
    _compact_email_draft_context,
    _minimal_odysseus_doc_messages,
    _looks_like_notes_turn,
    _minimal_odysseus_notes_messages,
    _looks_like_memory_identity_turn,
    _minimal_odysseus_general_messages,
    _strip_doc_model_artifacts,
    _normalize_truncated_document_tool_fences,
    _normalize_stream_document_fences,
    _recent_context_for_retrieval,
    _build_system_prompt,
    _ADMIN_TOOLS,
    _CASUAL_OPENING_RE,
    _CASUAL_BLOCKLIST_RE,
    _EXPLICIT_CONTINUATION_RE,
)

logger = logging.getLogger(__name__)

def _build_base_prompt(
    disabled_tools,
    mcp_mgr,
    needs_admin,
    relevant_tools=None,
    mcp_disabled_map=None,
    compact: bool = False,
    owner: Optional[str] = None,
    suppress_local_context: bool = False,
    suppress_skills: bool = False,
):
    """Build the agent prompt with only relevant tools included.

    If relevant_tools is provided (from RAG retrieval), only those tools
    are shown with full descriptions. Otherwise falls back to full prompt.
    """
    from src.tool_index import ALWAYS_AVAILABLE

    disabled = set(disabled_tools or [])
    if not get_setting("image_gen_enabled", False):
        disabled.add("generate_image")

    if relevant_tools is not None:
        # RAG mode: trust the relevant_tools set as already-composed.
        # get_tools_for_query starts from ALWAYS_AVAILABLE and may
        # *discard* tools that conflict with the query's intent (e.g.
        # drop manage_memory for clear contact-save patterns). Unioning
        # ALWAYS_AVAILABLE back in here used to silently undo those
        # drops. Only force-include the irreducible loop primitives
        # (ask_user, update_plan) as belt-and-suspenders.
        tool_names = set(relevant_tools) | {"ask_user", "update_plan"}
        if needs_admin:
            tool_names |= _ADMIN_TOOLS
        agent_prompt = _assemble_prompt(tool_names, disabled, compact=compact, owner=owner)
    else:
        # Fallback: full prompt (RAG unavailable)
        agent_prompt = AGENT_SYSTEM_PROMPT
        if not needs_admin:
            # At least strip the management section
            mgmt_tools = set(TOOL_SECTIONS.keys()) - set(ALWAYS_AVAILABLE) - {
                "generate_image", "suggest_document",
                "chat_with_model", "ask_teacher", "list_models",
            }
            agent_prompt = _assemble_prompt(
                set(TOOL_SECTIONS.keys()) - mgmt_tools, disabled, compact=compact, owner=owner
            )
        elif compact:
            agent_prompt = _assemble_prompt(set(TOOL_SECTIONS.keys()), disabled, compact=True, owner=owner)

    # Inject the Level-0 skill index - one line per skill so the agent
    # knows what canonical procedures exist. Includes published skills
    # plus teacher-escalation drafts (auto-written when the student
    # fails a task; appear here on the very next turn so the student
    # can apply them immediately). Full SKILL.md fetched on demand via
    # `manage_skills view name=...`. Gating mirrors index_for: platform
    # + requires_toolsets + fallback_for_toolsets.
    #
    # SECURITY: skill `name` and `description` are user-editable, so the
    # index block is returned SEPARATELY (not appended to agent_prompt).
    # The caller wraps it in untrusted_context_message and ships it as a
    # user-role message - same treatment as the matched-skills block.
    skill_index_block = ""
    if not suppress_local_context and not suppress_skills:
        try:
            from services.memory.skills import SkillsManager
            from src.constants import DATA_DIR
            _sm = SkillsManager(DATA_DIR)
            active_tools = list(set(TOOL_SECTIONS.keys()) - set(disabled or []))
            skill_idx = _sm.index_for(owner=owner, active_toolsets=active_tools) or []
            lines = ["## Available skills",
                     "Procedures the assistant should consult before doing domain work. "
                     "Fetch skills with `manage_skills` action=view name=<name> "
                     "when one looks relevant. Entries tagged `(draft)` were written by the "
                     "teacher-escalation loop after a prior failure - treat them as authoritative "
                     "guidance; if you follow one and it works, that's a good signal the procedure "
                     "is correct. Built-in skills ship with Odys (read-only); user skills live "
                     "under data/skills/ and can be created via auto-learn or manage_skills."]

            by_cat: dict[str, list] = {}
            for s in skill_idx:
                by_cat.setdefault(s["category"], []).append(s)
            for cat in sorted(by_cat):
                lines.append(f"\n**{cat}**")
                for s in by_cat[cat]:
                    badge = " *(draft)*" if s.get("status") == "draft" else ""
                    lines.append(f"- `{s['name']}` - {s['description']}{badge}")
            # Only emit block if we have more than the header lines
            if len(lines) > 2:
                skill_index_block = "\n\n" + "\n".join(lines)
        except Exception as _e:
            # Skill index is a soft enhancement - never fail prompt assembly on it.
            logger.debug(f"Skill-index injection skipped: {_e}")

    return agent_prompt, skill_index_block



def _resolve_tool_blocks(
    round_response: str,
    native_tool_calls: list,
    round_num: int,
    is_api_model: bool = False,
    allow_fenced_for_api: bool = False,
):
    """Choose native function calls or fenced code block parsing. Returns (tool_blocks, used_native)."""
    used_native = False
    converted_calls = []  # native calls that converted, ALIGNED with tool_blocks
    if native_tool_calls:
        tool_blocks = []
        for tc in native_tool_calls:
            tc_name = tc.get("name", "")
            tc_args = tc.get("arguments", "{}")
            block = function_call_to_tool_block(tc_name, tc_args)
            if block:
                tool_blocks.append(block)
                converted_calls.append(tc)
                logger.info(f"  -> converted: {tc_name} -> {block.tool_type}")
            else:
                logger.warning(f"  -> FAILED to convert native call: {tc_name} args={tc_args[:200]}")
        if tool_blocks:
            used_native = True
    if not used_native:
        # Native function-calling models (GPT/Claude/Grok/Qwen3/DeepSeek-V, etc.)
        # have a reliable structured channel for real tool invocations. When such
        # a model emits no native tool_calls, any ```bash/```python/```json fence
        # in its prose is virtually always an illustrative example for the user
        # (e.g. "here's the command you'd run"), not an attempted tool call -
        # executing it causes accidental runs and clarification loops (#3222).
        #
        # Gate ONLY that fenced-block pattern for native models, not the whole
        # parser: explicit [TOOL_CALL]/<invoke>/<tool_code>/DSML markup that
        # leaks into content as text is never illustrative - it's a real call
        # the model couldn't emit on its structured channel (e.g. DeepSeek-V
        # falling back to DSML). Dropping the whole parser would silently lose
        # those too. Non-native / textual-only models keep every pattern,
        # fenced blocks included, since that's their *only* tool channel.
        tool_blocks = parse_tool_blocks(round_response, skip_fenced=(is_api_model and not allow_fenced_for_api))
        if tool_blocks:
            logger.info(f"Agent round {round_num}: {len(tool_blocks)} fenced tool block(s) detected")

    resp_preview = round_response[:200].replace('\n', '\\n') if round_response else "(empty)"
    logger.info(f"Agent round {round_num} summary: {len(round_response)} chars, "
                f"{len(native_tool_calls)} native calls, "
                f"{len(tool_blocks)} tool blocks. Preview: {resp_preview}")

    return tool_blocks, used_native, converted_calls


def _append_tool_results(
    messages: List[Dict],
    round_response: str,
    native_tool_calls: list,
    tool_results: list,
    tool_result_texts: list,
    used_native: bool,
    round_num: int,
    round_reasoning: str = "",
):
    """Append tool execution results back into the message history for the next LLM round.

    `round_reasoning` (DeepSeek / vLLM reasoning-parser deltas) is echoed
    back via `reasoning_content` on the assistant message - DeepSeek's API
    rejects follow-up requests in thinking mode that don't include the
    prior reasoning.

    NOTE: it is NOT universally ignored. Nemotron's chat template re-injects
    EVERY prior `reasoning_content` as a <think> block, and this agent loop is
    trimmed only once (before the loop), so across rounds the reasoning piles
    up unbounded - bloating context and feeding the model its own prior
    reasoning, which reinforces repetition/looping. So keep reasoning_content
    on the MOST RECENT assistant turn only: enough for DeepSeek continuity,
    without the per-round accumulation.
    """
    # Strip reasoning_content from earlier assistant turns; only the newest keeps it.
    for _m in messages:
        if _m.get("role") == "assistant":
            _m.pop("reasoning_content", None)
    if used_native and native_tool_calls:
        assistant_msg = {"role": "assistant"}
        # When the model emitted ONLY tool calls (no prose), content must be
        # null, NOT an empty string. Google Gemini's OpenAI-compatible endpoint
        # and Ollama both reject an assistant message that carries tool_calls
        # alongside empty-string content with HTTP 400 ("contents is not
        # specified" / a JSON parse error), which aborts every tool-using turn
        # at the follow-up round. null (i.e. omitted text) is the spec-correct
        # form the OpenAI SDK itself emits, and OpenAI/Anthropic accept it too.
        assistant_msg["content"] = round_response if round_response.strip() else None
        if round_reasoning:
            assistant_msg["reasoning_content"] = round_reasoning
        assistant_msg["tool_calls"] = [
            {
                "id": tc.get("id", f"call_{round_num}_{j}"),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": tc.get("arguments", "{}"),
                },
                # Gemini 3 requires the opaque thought_signature it returned with
                # each function call to be echoed back on the follow-up turn, or
                # the next request 400s. Replay it when present; other providers
                # never emit it (their payload builders just ignore the field).
                **({"extra_content": tc["extra_content"]} if tc.get("extra_content") else {}),
            }
            for j, tc in enumerate(native_tool_calls)
        ]
        messages.append(assistant_msg)
        for j, tc in enumerate(native_tool_calls):
            result_text = tool_result_texts[j] if j < len(tool_result_texts) else ""
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{round_num}_{j}"),
                "content": result_text,
            })
    else:
        tool_output_text = "\n\n".join(tool_results)
        msg = {"role": "assistant", "content": round_response}
        if round_reasoning:
            msg["reasoning_content"] = round_reasoning
        messages.append(msg)
        # Tool output (shell/python stdout, file reads, fetched pages, email
        # bodies, MCP results) is sourced from outside the server. Wrap it as
        # untrusted data so prompt-injection inside a tool result is treated as
        # data, not instructions - same hardening as skills (#788) and the
        # web/RAG context. THREAT_MODEL.md lists tool output as a surface that
        # must go through untrusted_context_message.
        messages.append(
            untrusted_context_message("tool execution results", tool_output_text)
        )


def _compute_final_metrics(
    messages: List[Dict],
    full_response: str,
    total_duration: float,
    time_to_first_token,
    context_length: int,
    real_input_tokens: int,
    real_output_tokens: int,
    has_real_usage: bool,
    tool_events: list,
    round_texts: list,
    model: str = "",
    last_round_input_tokens: int = 0,
    prep_timings: Optional[Dict[str, float]] = None,
    backend_gen_tps: float = 0,
    backend_prefill_tps: float = 0,
) -> dict:
    """Compute token counts, TPS, and build the final metrics dict."""
    if has_real_usage:
        input_tokens = real_input_tokens
        output_tokens = real_output_tokens
    else:
        input_content = ""
        for msg in messages:
            if isinstance(msg.get("content"), str):
                input_content += msg["content"] + "\n"
        input_tokens = len(input_content) // 4
        output_tokens = len(full_response) // 4
    # Prefer the backend's true generation speed (llama.cpp
    # timings.predicted_per_second) - pure decode, no prefill/tool/network time.
    # Fall back to tokens/wall-clock only when the backend didn't report it
    # (e.g. cloud APIs without timings); that figure reads low because
    # total_duration includes prefill + agent overhead.
    if backend_gen_tps and backend_gen_tps > 0:
        tps = backend_gen_tps
    else:
        tps = output_tokens / total_duration if total_duration > 0 else 0
    # Use last round's input tokens for context % (peak usage) when available
    ctx_tokens = last_round_input_tokens if last_round_input_tokens > 0 else input_tokens
    ctx_pct = min(round((ctx_tokens / context_length) * 100, 1), 100.0) if context_length else 0

    metrics = {
        "response_time": round(total_duration, 2),
        "time_to_first_token": round(time_to_first_token, 2) if time_to_first_token else 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_per_second": round(tps, 2),
        # True decode speed when the backend reported it; "computed" = the
        # tokens/wall-clock fallback (reads low - includes prefill/overhead).
        "tps_source": "backend" if (backend_gen_tps and backend_gen_tps > 0) else "computed",
        "total_tokens": input_tokens + output_tokens,
        "context_length": context_length,
        "context_percent": ctx_pct,
        "usage_source": "real" if has_real_usage else "estimated",
        "model": model,
    }
    if backend_prefill_tps and backend_prefill_tps > 0:
        metrics["prefill_tps"] = round(backend_prefill_tps, 2)
    if prep_timings:
        prep_total = round(sum(prep_timings.values()), 3)
        metrics["agent_prep_time"] = prep_total
        metrics["agent_model_wait_time"] = round(max((time_to_first_token or 0) - prep_total, 0), 3)
        metrics["agent_prep_breakdown"] = {
            key: round(value, 3) for key, value in prep_timings.items()
        }
    if tool_events:
        metrics["tool_events"] = tool_events
        metrics["round_texts"] = round_texts
    return metrics


# ── Completion verifier ──
# Tools whose effects produce a checkable artifact. A turn that used one of
# these is "effectful" and worth an independent completion check; pure
# read-only / Q&A turns are not.
_VERIFIER_EFFECTFUL_TOOLS = {
    "create_document", "update_document", "edit_document",
    "bash", "python", "write_file",
}
_VERIFIER_MAX_ROUNDS = 2  # cap re-verify cycles per turn - never loop forever


def _build_actions_snapshot(tool_events: list, limit: int = 8000) -> str:
    """Compact record of what the agent actually did this turn, for the
    verifier to judge against. One block per tool execution: the command and
    a head of its output."""
    parts = []
    for ev in tool_events:
        tool = ev.get("tool", "?")
        cmd = (ev.get("command") or "").strip()
        out = (ev.get("output") or "").strip()
        rc = ev.get("exit_code")
        head = f"[{tool}] {cmd}" if cmd else f"[{tool}]"
        rc_s = f" (exit {rc})" if rc not in (None, 0) else ""
        body = (out[:1200] + " …") if len(out) > 1200 else (out or "(no output)")
        parts.append(f"{head}{rc_s}\n-> {body}")
    snap = "\n\n".join(parts)
    return snap[:limit] if len(snap) > limit else snap


async def _run_verifier_subagent(
    instruction: str, actions_snapshot: str,
    *, endpoint_url: str, model: str, headers: dict,
) -> list:
    """Fresh-context completion verifier. A second model instance with NO
    shared history reads the user's request + a record of what the agent did
    and judges whether the task is genuinely complete. The independent context
    is the whole point: a model checking its own work rationalizes; one that
    didn't do the work reads it cold. Returns a list of failure reasons
    (empty = pass, or silently empty on any error so it can't block a valid
    completion)."""
    from src.llm_core import llm_call_async
    prompt = (
        "You are an independent verifier. Another assistant just claimed the "
        "following task is complete. Using ONLY the request and the record of "
        "what it actually did, decide whether that claim is correct. Be strict: "
        "only say SUCCESS if the work genuinely satisfies the request.\n\n"
        f"<user_request>\n{(instruction or '')[:4000]}\n</user_request>\n\n"
        f"<actions_taken>\n{actions_snapshot[:8000]}\n</actions_taken>\n\n"
        "<checklist>\n"
        "1. Every concrete deliverable the request asked for was actually produced\n"
        "2. Outputs/edits match what was asked - nothing missing, no extra or unrequested changes\n"
        "3. Tool results show success, not errors or empty output that got ignored\n"
        "4. Anything the request said to leave alone was left unchanged\n"
        "</checklist>\n\n"
        "Reason briefly (2-3 sentences max). Then output EXACTLY one of:\n"
        "  VERIFICATION: SUCCESS\n"
        "  VERIFICATION: FAIL: <one short sentence per issue, semicolon-separated>\n"
        "Output nothing after the VERIFICATION line."
    )
    try:
        raw = await llm_call_async(
            url=endpoint_url, model=model,
            messages=[{"role": "user", "content": prompt}],
            headers=headers, temperature=0.0, max_tokens=600, timeout=60,
        )
    except Exception as e:
        logger.warning(f"[agent] verifier subagent failed: {e}")
        return []
    raw = _strip_think_blocks(raw or "")
    last_v = None
    for line in raw.splitlines():
        if "VERIFICATION:" in line:
            last_v = line.strip()
    if not last_v or "VERIFICATION: FAIL:" not in last_v:
        return []
    reasons = last_v.split("VERIFICATION: FAIL:", 1)[1].strip()
    return [r.strip() for r in reasons.split(";") if r.strip()]


async def _empty_response_fallback(
    full_response: str,
    round_reasoning: str,
    tool_events: list,
    messages: list = None,
    endpoint_url: str = None,
    model: str = None,
    headers: dict = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> tuple:
    """Return (final_response, sse_chunk_or_none) for the end-of-loop empty-response guard."""

    if full_response.strip():
        return full_response, None
    if round_reasoning.strip() and not tool_events:
        return round_reasoning, None

    # Attempt synthesis if we have tool events (data gathered) but no text answer
    if tool_events and messages and endpoint_url:
        try:
            from src.llm_core import llm_call_async
            import asyncio
            _synth_messages = list(messages) + [{
                "role": "user",
                "content": (
                    "Using ONLY the information already gathered above, write "
                    "the final answer for the user now. Do NOT call any tools, "
                    "do NOT explain your reasoning - output the finished response "
                    "directly. If some data couldn't be fetched, just work with "
                    "what you have and note what's missing in one short line."
                ),
            }]

            # Use await directly since this function is now async
            _synth_raw = await llm_call_async(
                url=endpoint_url, model=model, messages=_synth_messages,
                headers=headers, temperature=temperature, max_tokens=max_tokens, timeout=45,
            )

            from src.agent_loop.loop import strip_tool_blocks, _strip_think_blocks
            _synth = _strip_think_blocks(strip_tool_blocks(_synth_raw or "")).strip()
            if _synth:
                return _synth, f'data: {json.dumps({"delta": _synth})}\n\n'
        except Exception as _e:
            import logging
            logging.getLogger("agent").warning(f"[agent] fallback grace synthesis failed: {_e}")

    # Fallback to last reasoning block if available as a last resort
    if round_reasoning.strip():
        return round_reasoning, None

    _error_msg = "The model returned an empty response. Please try again or switch to a different model."
    logger.warning(f"[agent] empty response fallback triggered: full_response empty, round_reasoning empty, tool_events empty")
    return _error_msg, f'data: {json.dumps({"delta": _error_msg})}\n\n'


PLAN_MODE_DIRECTIVE = (
    "## PLAN MODE - OVERRIDES EVERYTHING ELSE BELOW\n"
    "You are in PLAN MODE. Your ONLY job this turn is to PROPOSE a plan. You have "
    "NOT done anything yet. Do NOT claim you created, wrote, ran, sent, or changed "
    "anything - that would be a lie.\n"
    "\n"
    "ABSOLUTE RULE - DO NOT MUTATE ANYTHING. Every write/state-changing tool, "
    "including the shell (`bash`/`python`), is disabled this turn and will be "
    "rejected - only read-only tools remain available. Use the read-only tools "
    "listed below (read files, search code, browse the project, web lookups) to "
    "ground the plan. If the task is 'write a file', your plan is to DESCRIBE "
    "writing it - you do NOT write it now.\n"
    "\n"
    "OUTPUT: present the plan as a GitHub-style checklist, one concrete step per line:\n"
    "- [ ] first action you will take once approved\n"
    "- [ ] next action\n"
    "Each item = one concrete action (file to create/edit, command to run, side "
    "effect). Do not execute. Do not end with 'Done' or anything implying the work "
    "is finished. End your turn with the checklist."
)


def build_active_plan_note(approved_plan: str) -> str:
    """System note that pins an approved plan during execution.

    Sent back by the frontend each turn so a long plan on a weak model survives
    history truncation - the agent can always re-read it. Returns "" for empty
    input.
    """
    if not approved_plan or not approved_plan.strip():
        return ""
    return (
        "## ACTIVE PLAN (approved - execute this)\n"
        "You are executing a plan the user already approved. THE FULL PLAN IS "
        "BELOW - it is always provided here every turn. Do NOT say you lost it, "
        "and do NOT look for it in tasks, notes, memory, files, or the API; just "
        "read it below. Work through it IN ORDER. After finishing each step, call "
        "the `update_plan` tool with the full checklist and that step marked "
        "`- [x]` so progress stays visible in the user's plan window. If the user "
        "asks to change the plan, call `update_plan` with the revised checklist. "
        "Do the next unchecked item until all are done. Do not skip, reorder, or "
        "invent steps; if a step is genuinely impossible, say so and stop.\n\n"
        "Current plan:\n"
        + approved_plan.strip()
    )


def _detect_runaway_call(call_freq, threshold=5):
    """Tool name of a call signature repeated >= ``threshold`` times - a real
    runaway loop. Counts IDENTICAL repeated calls (same tool AND args), so a
    legitimate batch of distinct calls to one tool (e.g. creating 18 calendar
    events at once) is NOT flagged. Returns ``None`` when nothing is runaway.

    ``call_freq`` is a Counter keyed by ``"{tool_type}:{content[:120]}"``.
    Agent discipline: threshold lowered from 15 -> 5 so loops die earlier.
    """
    sig = next((s for s, n in call_freq.items() if n >= threshold), None)
    return sig.split(":", 1)[0] if sig else None

