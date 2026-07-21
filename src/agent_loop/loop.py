"""stream_agent_loop — main multi-round streaming agent runtime."""

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
    _TOOL_SELECTION_TIMEOUT_SECONDS,
    _DOMAIN_TOOL_MAP,
    _domain_rules_for_tools,
    _looks_like_notes_list_request,
    _note_list_summary_from_tool_output,
    _load_mcp_disabled_map,
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
                            _normalize_truncated_document_tool_fences,
        _recent_context_for_retrieval,
    _build_system_prompt,
    _ADMIN_TOOLS,
    _CASUAL_OPENING_RE,
    _CASUAL_BLOCKLIST_RE,
    _EXPLICIT_CONTINUATION_RE,
)

from src.agent_loop.tool_runner import (
    _build_base_prompt,
    _resolve_tool_blocks,
    _append_tool_results,
    _compute_final_metrics,
    _build_actions_snapshot,
    _run_verifier_subagent,
    _empty_response_fallback,
    build_active_plan_note,
    _detect_runaway_call,
)

logger = logging.getLogger(__name__)



# --- Headroom compression support ---
_headroom_compress = None

def _init_headroom():
    global _headroom_compress
    if _headroom_compress is not None:
        return
    try:
        from headroom import compress as _compress
        def _compress_tool_output(text: str) -> str:
            """Compress tool output via Headroom compress() (role=tool so ContentRouter routes to SmartCrusher/text crunch)."""
            messages = [{"role": "tool", "content": text}]
            res = _compress(messages)
            if isinstance(res, list) and res:
                compressed = res[0].get("content", text)
                saved_pct = 100 - int(len(compressed) / max(len(text),1) * 100)
                if saved_pct > 0:
                    logger.info(f"[headroom] compress: {len(text)} -> {len(compressed)} chars ({saved_pct}% saved)")
                return compressed
            return text
        _headroom_compress = _compress_tool_output
        logger.info("[headroom] compressor loaded successfully")
    except ImportError:
        logger.info("[headroom] not installed, defaulting to _truncate")
    except Exception as e:
        logger.warning(f"[headroom] failed to initialize: {e}")

# Call init immediately (lazy setup check)
try:
    _init_headroom()
except Exception:
    pass



def _smart_truncate(raw_text: str) -> str:
    """Compress tool output intelligently using Headroom, falling back to simple char truncation."""
    from src.constants import COMPRESS_THRESHOLD
    from src.tool_utils import _truncate
    
    if not isinstance(raw_text, str):
        raw_text = str(raw_text) if raw_text is not None else ""
        
    if _headroom_compress and len(raw_text) >= COMPRESS_THRESHOLD:
        try:
            return _headroom_compress(raw_text)
        except Exception as e:
            logger.debug(f"[headroom] compression failed, falling back to _truncate: {e}")
            
    return _truncate(raw_text)


async def stream_agent_loop(
    endpoint_url: str,
    model: str,
    messages: List[Dict],
    headers: Optional[Dict] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    prompt_type: Optional[str] = None,
    max_rounds: int = MAX_AGENT_ROUNDS,
    max_tool_calls: int = 0,
    context_length: int = 0,
    active_document=None,
    active_email: Optional[Dict[str, str]] = None,
    session_id: Optional[str] = None,
    disabled_tools: Optional[Set[str]] = None,
    owner: Optional[str] = None,
    relevant_tools: Optional[Set[str]] = None,
    fallbacks: Optional[List[tuple]] = None,
    plan_mode: bool = False,
    approved_plan: Optional[str] = None,
    tool_policy: Optional[ToolPolicy] = None,
    workspace: Optional[str] = None,
    forced_tools: Optional[Set[str]] = None,
    uploaded_files: Optional[List[Dict]] = None,
    workload: str = "foreground",
    _is_teacher_run: bool = False,
) -> AsyncGenerator[str, None]:
    """Streaming agent loop generator.

    Yields SSE events:
      - data: {"delta": "text"}                             (text chunks)
      - data: {"type": "tool_start", "tool": "...", ...}    (before execution)
      - data: {"type": "tool_output", "tool": "...", ...}   (after execution)
      - data: {"type": "agent_step", "round": N}            (next round)
      - data: {"type": "metrics", "data": {...}}            (final metrics)
      - data: [DONE]                                        (end)
    """

    mcp_mgr = get_mcp_manager()
    prep_timings: Dict[str, float] = {}
    disabled_tools = set(disabled_tools or [])
    if tool_policy:
        disabled_tools.update(tool_policy.all_disabled_names())
        if tool_policy.disable_mcp:
            mcp_mgr = None
    guide_only = bool(tool_policy and tool_policy.mode == "guide_only")
    public_blocked_tools = blocked_tools_for_owner(owner)
    if public_blocked_tools:
        disabled_tools.update(public_blocked_tools)
        # MCP tools are namespaced dynamically, so hide all MCP schemas for
        # public/non-admin users rather than trying to enumerate every tool.
        mcp_mgr = None

    if plan_mode:
        # Plan mode: investigate read-only, propose a plan, don't execute. The
        # route also unions the read-only-disabled set, but enforce here too so
        # the loop is safe regardless of caller. MCP stays available but is
        # filtered to read-only tools below (after the disabled map is loaded).
        disabled_tools.update(plan_mode_disabled_tools())

    uploaded_files = uploaded_files or []
    _upload_msg = _uploaded_files_context_message(uploaded_files)
    if _upload_msg:
        messages = _insert_before_latest_user(messages, _upload_msg)

    _t0 = time.time()
    _needs_admin = _detect_admin_intent(messages)
    _last_user = _extract_last_user_message(messages)

    # ── UserPromptSubmit Hooks (ECC guardrails/validation) ──
    try:
        from src.hooks.registry import get_registry as _get_hook_reg
        _hr = _get_hook_reg()
        if _hr.loaded and _last_user:
            _errs = await _hr.run_user_prompt_submit(_last_user, session_id)
            if _errs:
                _msg = "Blocked by UserPromptSubmit hook:\n- " + "\n- ".join(_errs)
                yield f'data: {json.dumps({"delta": _msg})}\n\n'
                yield f"data: {json.dumps({'type': 'metrics', 'data': {'error': True}})}\n\n"
                yield "data: [DONE]\n\n"
                return
    except Exception as e:
        logger.debug("UserPromptSubmit hook check failed: %s", e)

    _intent = _classify_agent_request(messages, _last_user)
    _low_signal_turn = bool(_intent.get("low_signal"))
    _casual_low_signal_turn = _is_casual_low_signal(_last_user)
    _existing_conversation = _user_turn_count(messages) > 1
    _active_document_relevant = _turn_targets_active_document(_intent, _last_user, active_document)
    _active_email_draft_relevant = _active_document_relevant and _is_email_document_obj(active_document)
    if _active_email_draft_relevant:
        disabled_tools.update({
            "list_email_accounts", "list_emails", "read_email",
            "mcp__email__list_emails", "mcp__email__read_email",
        })
    _prompt_active_document = active_document if _active_document_relevant else None
    # Tool retrieval uses the latest message by default. It may inherit recent
    # user turns only for explicit continuations ("yes", "do it", "1").
    _retrieval_query = str(_intent.get("retrieval_query") or _last_user)
    logger.info(
        "[agent-intent] latest=%r continuation=%s low_signal=%s domains=%s active_doc_relevant=%s retrieval_query=%r",
        _last_user[:120],
        bool(_intent.get("continuation")),
        _low_signal_turn,
        sorted(_intent.get("domains") or []),
        _active_document_relevant,
        _retrieval_query[:200],
    )
    if _low_signal_turn and _existing_conversation:
        logger.info(
            "[agent] keeping contextual path for low-signal turn in existing conversation latest=%r",
            _last_user[:80],
        )
    _mcp_disabled_map = _load_mcp_disabled_map() if mcp_mgr else {}

    if plan_mode and mcp_mgr:
        # Allow read-only MCP tools to investigate, block write/unknown ones:
        # hide them from the schemas AND reject them at runtime by qualified name.
        _mcp_block_map, _mcp_block_q = mcp_mgr.plan_mode_blocked_mcp()
        for _sid, _names in _mcp_block_map.items():
            _mcp_disabled_map.setdefault(_sid, set()).update(_names)
        disabled_tools.update(_mcp_block_q)
    prep_timings["request_setup"] = time.time() - _t0

    # Initialize skill registry (ECC skills) once per session
    try:
        from src.skills.registry import get_registry
        _reg = get_registry()
        if not _reg.loaded:
            _n = _reg.reload()
            if _n > 0:
                logger.info("[skills] Loaded %s ECC skill(s)", _n)
    except Exception as e:
        logger.debug("[skills] Init skipped: %s", e)

    # Initialize rules engine once per session
    try:
        from src.rules.engine import get_engine
        _re = get_engine()
        if not _re.loaded:
            _n_rules = _re.reload()
            if _n_rules > 0:
                logger.info("[rules] Loaded %s rule(s)", _n_rules)
    except Exception as e:
        logger.debug("[rules] Init skipped: %s", e)

    # Initialize hooks registry once per session
    try:
        from src.hooks.registry import get_registry as _get_hooks
        _hr = _get_hooks()
        if not _hr.loaded:
            _n_hooks = _hr.reload()
            if _n_hooks > 0:
                logger.info("[hooks] Loaded %s hook(s)", _n_hooks)
    except Exception as e:
        logger.debug("[hooks] Init skipped: %s", e)

    # Tool Selection (Hermes style)
    _relevant_tools = relevant_tools
    _t1 = time.time()
    if _relevant_tools:
        logger.info(f"[tool-selection] Using caller-provided relevant_tools ({len(_relevant_tools)} tools)")
    else:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
        _relevant_tools = {
            s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS
            if "function" in s and "name" in s["function"]
        }
        logger.info(f"[tool-selection] All {len(_relevant_tools)} tools provided to agent")

    if _active_document_relevant:
        _relevant_tools.update({"edit_document", "update_document", "suggest_document"})
        if _active_email_draft_relevant:
            _email_fetch_tools = {
                "list_email_accounts", "list_emails", "read_email",
                "mcp__email__list_emails", "mcp__email__read_email",
            }
            _relevant_tools.difference_update(_email_fetch_tools)

    prep_timings["tool_selection"] = time.time() - _t1

    _t2 = time.time()
    # Hosted-API match by URL, OR the model name looks like a recent model
    # known to follow OpenAI-style function calling (DeepSeek, GPT*, Claude,
    # Gemini, Qwen3+, Mixtral, Llama 3.1+). Caught the DeepSeek-via-local-
    # vLLM case where endpoint_url doesn't include a vendor host.
    _model_lc = (model or "").lower()
    # Step 1: per-endpoint override (set at registration time from the
    # serve command — `--enable-auto-tool-choice` flips it on. UI can
    # also toggle per endpoint). NULL = unknown; for local Ollama /v1 we
    # default to fenced tools, otherwise fall through to keyword + host checks.
    _endpoint_supports: Optional[bool] = None
    try:
        from core.database import SessionLocal as _SL, ModelEndpoint as _ME
        _db = _SL()
        try:
            _ep = None
            for _key in _endpoint_lookup_keys(endpoint_url):
                _ep = _db.query(_ME).filter(_ME.base_url == _key).first()
                if _ep is not None:
                    break
            if _ep is not None:
                _endpoint_supports = _ep.supports_tools
        finally:
            _db.close()
    except Exception as _e:
        logger.debug(f"endpoint supports_tools lookup failed: {_e}")
    _model_supports_tools = any(kw in _model_lc for kw in (
        "gpt-4", "gpt-5", "gpt-o", "claude", "gemini", "gemma",
        "qwen3", "qwen2.5", "mixtral", "mistral", "llama-3.1", "llama-3.2",
        "llama-3.3", "llama-4", "llama3.1", "llama3.2", "llama3.3", "llama4",
        # Local-served models that follow OpenAI-style function calling
        # via vLLM's `--enable-auto-tool-choice`. Belt-and-suspenders
        # with the per-endpoint flag above.
        "minimax", "kimi", "yi-", "phi-3", "phi-4", "command-r",
        "glm-4", "internlm", "hermes",
        "fusion",  # SAO local gateway model
        # deepseek-v2/v3/chat support tools via the cloud API; deepseek-r1
        # (reasoning model) does not — handled by the blocklist below.
        "deepseek-v", "deepseek-chat",
    ))
    # Models known to reject tool schemas at the Ollama/local level even when
    # the endpoint URL would otherwise enable native function calling.
    # The per-endpoint supports_tools flag (True/False) always takes priority
    # and can override this list for users who know their setup.
    _model_no_tools = any(kw in _model_lc for kw in (
        "deepseek-r1",
        # Open-weight GPT-OSS models are commonly served through llama.cpp /
        # llama-cpp-python. Their names contain "gpt-o", but they do not use
        # OpenAI's native tool-call channel unless the endpoint opts in.
        "gpt-oss",
    ))
    # Native Ollama endpoints (/api/chat) handle tool schemas differently from
    # the OpenAI-compat path. Models like gemma4, qwen3.5, ministral respond to
    # tool schemas by emitting a single native tool_call token then stopping,
    # rather than writing a fenced block — the agent loop sees 1 token and no
    # recognised tool, so the round terminates immediately (issue #1567).
    # Unless the endpoint is explicitly marked supports_tools=True by the user
    # (via the endpoint settings toggle), treat Ollama-native as text-only so
    # the fenced-block path is used instead of native function calling.
    _is_ollama_native = _is_ollama_native_url(endpoint_url or "")
    _ollama_openai_compat = _is_ollama_openai_compat_url(endpoint_url or "")
    # Prefer native OpenAI-style tools when
    # settings say so (agent_prefer_native_tools). Endpoint.supports_tools still
    # wins when explicitly True/False. Fenced blocks remain fallback via
    # agent_allow_fenced_fallback (used later in _resolve_tool_blocks).
    _prefer_native = bool(get_setting("agent_prefer_native_tools", True))
    _allow_fenced_fallback = bool(get_setting("agent_allow_fenced_fallback", True))
    if _endpoint_supports is True:
        _is_api_model = True
    elif (
        _endpoint_supports is False
        or _model_no_tools
        or _is_ollama_native
        or (_ollama_openai_compat and not _prefer_native)
    ):
        _is_api_model = False
    elif _prefer_native and not _model_no_tools:
        # SAO: custom OpenAI-compat (e.g. fusion @ host.docker.internal:20128)
        # should send tool schemas even when host is not in _API_HOSTS.
        _is_api_model = True
    else:
        _is_api_model = any(h in endpoint_url for h in _API_HOSTS) or _model_supports_tools
    logger.info(
        "[agent-native] prefer_native=%s endpoint_supports=%s is_api_model=%s allow_fenced_fallback=%s model=%s",
        _prefer_native,
        _endpoint_supports,
        _is_api_model,
        _allow_fenced_fallback,
        model,
    )
    _compact_agent_prompt = _is_api_model or _is_ollama_native or _ollama_openai_compat
    messages, mcp_schemas = _build_system_prompt(
        messages, model, _prompt_active_document, mcp_mgr, disabled_tools,
        needs_admin=_needs_admin, relevant_tools=_relevant_tools,
        mcp_disabled_map=_mcp_disabled_map,
        compact=_compact_agent_prompt,
        owner=owner,
        suppress_local_context=guide_only,
        suppress_skills=_low_signal_turn,
        active_email=active_email,
    )
    if plan_mode and not guide_only:
        # Steer the model to investigate-then-propose. Hard tool gating handles
        # every write path except shell; this directive is what keeps the
        # intentionally-allowed bash/python read-only, so it must DOMINATE. Put
        # it at the very TOP of the system prompt (the base prompt is large and
        # action-oriented — appending buried it, and small models ignored it).
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = PLAN_MODE_DIRECTIVE + "\n\n" + (messages[0].get("content") or "")
        else:
            messages.insert(0, {"role": "system", "content": PLAN_MODE_DIRECTIVE})
    elif approved_plan and approved_plan.strip() and not guide_only:
        # EXECUTING an approved plan. Pin the checklist as a top-of-context
        # system note so a long plan on a weak model survives history
        # truncation — the agent can always re-read the plan instead of losing
        # the thread. (The first system message is kept by the context trimmer.)
        _plan_note = build_active_plan_note(approved_plan)
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = _plan_note + "\n\n" + (messages[0].get("content") or "")
        else:
            messages.insert(0, {"role": "system", "content": _plan_note})
        logger.info("[plan] pinned approved plan (%d chars) for execution turn", len(approved_plan))
    if guide_only:
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = GUIDE_ONLY_DIRECTIVE + "\n\n" + (messages[0].get("content") or "")
        else:
            messages.insert(0, {"role": "system", "content": GUIDE_ONLY_DIRECTIVE})
    try:
        from services import odys_neuron_service as _neurons
        _last_user_msg = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
        if _last_user_msg:
            _n_result = _neurons.activate(query=_last_user_msg, top_k=5)
            if _n_result.get("ok") and _n_result.get("results"):
                _n_texts = []
                for res in _n_result["results"]:
                    _lbl = res.get("label", "")
                    _ref = res.get("ref", "")
                    _typ = res.get("type", "")
                    if _lbl:
                        _line = f"- [{_typ}] {_lbl}"
                        if _ref:
                            _line += f" (id={_ref})"
                        _n_texts.append(_line)
                
                if _n_texts:
                    _mem_str = "## Relevant Background Memory (auto-recalled)\n" + "\n".join(_n_texts)
                    if messages and messages[0].get("role") == "system":
                        messages[0]["content"] = messages[0].get("content", "") + "\n\n" + _mem_str
                    else:
                        messages.insert(0, {"role": "system", "content": _mem_str})
                    logger.info(f"[neuron] Injected {len(_n_texts)} active memory nodes based on user query")
    except Exception as e:
        logger.warning(f"[neuron] Auto-injection skipped/failed: {e}")

    prep_timings["prompt_build"] = time.time() - _t2

    _t3 = time.time()
    try:
        from src.context_compactor import trim_for_context
        from src.context_budget import compute_input_token_budget, DEFAULT_HARD_MAX, DEFAULT_BUDGET, budget_is_explicit as _budget_is_explicit
        from src.model_context import budget_context_for_model

        soft_budget = int(get_setting("agent_input_token_budget", DEFAULT_BUDGET) or 0)
        if soft_budget > 0:
            before_trim_tokens = estimate_tokens(messages)
            reserve_tokens = min(max(max_tokens or 1024, 512), 2048)
            # Ceiling for the auto-derived budget (no effect on an explicit budget;
            # see #1230). Falls back to DEFAULT_HARD_MAX on missing/malformed values
            # so misconfig can't zero the budget.
            try:
                hard_max = int(get_setting("agent_input_token_hard_max", DEFAULT_HARD_MAX) or DEFAULT_HARD_MAX)
            except (TypeError, ValueError):
                hard_max = DEFAULT_HARD_MAX
            if hard_max <= 0:
                hard_max = DEFAULT_HARD_MAX
            # Default value = auto sentinel (scale to the window); any other value =
            # explicit cap. Value-based, not presence-based, because the save path
            # materializes defaults so a persisted default must still read as auto (#4121).
            budget_is_explicit = _budget_is_explicit(soft_budget)
            # Scale only off a window we actually discovered, bound to the value it
            # proves (else 0) — not the passed-in context_length, which can be stale
            # or unset for some callers (#4122 review).
            ctx_for_budget = budget_context_for_model(endpoint_url, model, fallback=context_length)
            effective_budget = compute_input_token_budget(
                soft_budget,
                ctx_for_budget,
                budget_is_explicit,
                hard_max=hard_max,
            )
            trimmed_messages = trim_for_context(
                messages,
                effective_budget,
                reserve_tokens=reserve_tokens,
            )
            after_trim_tokens = estimate_tokens(trimmed_messages)
            if after_trim_tokens < before_trim_tokens:
                logger.info(
                    "[agent] soft-trimmed context: %s -> %s tokens (budget=%s, reserve=%s)",
                    before_trim_tokens,
                    after_trim_tokens,
                    effective_budget,
                    reserve_tokens,
                )
                messages = trimmed_messages
    except Exception as e:
        logger.warning("[agent] Soft context trim skipped: %s", e)
    prep_timings["context_trim"] = time.time() - _t3

    # Strip internal metadata keys before sending to the LLM API
    messages = [{k: v for k, v in msg.items() if k != "_protected"} for msg in messages]

    agent_prompt_tokens = estimate_tokens(messages)
    logger.info(
        "[agent-timing] prep_done model=%s prompt_tokens=%s context_length=%s prep=%s",
        model,
        agent_prompt_tokens,
        context_length,
        {k: round(v, 3) for k, v in prep_timings.items()},
    )
    yield f"data: {json.dumps({'type': 'agent_prep', 'data': {k: round(v, 3) for k, v in prep_timings.items()}})}\n\n"

    full_response = ""
    total_start = time.time()
    time_to_first_token = None
    first_token_received = False
    tool_events = []   # Persist tool executions for history reload
    round_texts = []   # Cleaned text per round for history reload
    # Completion-verifier state (mechanism 3a). _effectful_used flips on when
    # a tool that produces a checkable artifact runs; the verifier only fires
    # on such turns.
    real_input_tokens = 0   # Accumulated real usage from API
    real_output_tokens = 0
    last_round_input_tokens = 0  # Last round's input tokens (for context % peak)
    has_real_usage = False
    backend_gen_tps = 0      # backend-reported true gen speed (llama.cpp timings)
    backend_prefill_tps = 0  # backend-reported prefill speed
    requested_model = model
    actual_model = model
    total_tool_calls = 0  # for budget enforcement
    # Session discipline tracking (totals across whole agent run)
    _session_tool_counts = {"web_fetch": 0, "web_search": 0, "bash": 0, "python": 0}

    # Loop-breaker state. Small models (e.g. deepseek-v4-flash) can get
    # stuck firing the same tool call over and over with no text — burns
    # all 20 rounds, looks like the chat "died". Track recent call
    # signatures + consecutive no-text tool rounds to bail early.
    _recent_call_sigs = collections.deque(maxlen=6)
    _stuck_rounds = 0
    # Retry holder for native tool call JSON parse failures
    _native_retries_holder = type('_nr', (), {'_n': 0})()
    # Track how many tool_events we've already checked for circuit breaker errors
    _events_processed_marker = type('_ep', (), {'_n': 0})()
    # Track which tool caps we've already informed the model about (once per type)
    _capped_tools_informed = {}
    # Frequency of each exact call signature (tool + args), for the runaway
    # backstop. Counting identical repeats — not distinct same-tool calls —
    # lets a legit batch (e.g. 18 calendar events at once) through.
    _call_freq: collections.Counter = collections.Counter()
    _force_answer = False  # set by loop-breaker → next round runs with NO tools
    _awaiting_user = False  # set by ask_user → end the turn and wait for a choice
    # Supervisor: how many times we've nudged the model after it announced
    # an action without emitting the tool call. Capped to prevent a model
    # that *can't* call the tool from looping forever.    # "I said I would, then didn't" detector. The pattern that breaks debug
    # loops on weak models (deepseek-v4-flash mid-2026): the model writes
    # "Let me tail the output to see the error" and then ends the turn with
    # no tool_calls. The intent is sincere but the function call gets dropped.
    # Match the common phrasings + an action verb that maps to an available
    # tool, so we don't nudge on harmless transitional text like "let me
    # know what you think".    _awaiting_user = False  # set by ask_user → end the turn and wait for a choice

    # ── Circuit breaker state (error normalization + stagnation detector) ──
    # Track tool error signatures: normalize volatile parts (line numbers,
    # ports, temp paths) to detect "same error, different details" patterns.
    _error_signatures: collections.Counter = collections.Counter()
    _consecutive_error_rounds = 0  # rounds where ALL tools failed with errors
    _no_progress_rounds = 0        # rounds with zero useful output (no text, only errors)
    _CB_MAX_SAME_ERROR = 3         # same normalized error N times → escalate
    _CB_MAX_ERROR_ROUNDS = 3       # N consecutive rounds with only errors → escalate
    _CB_MAX_NO_PROGRESS = 4        # N rounds with zero useful output → escalate

    # Document streaming state (persists across rounds)
    _doc_acc = ""          # accumulated tool-call JSON arguments
    _doc_opened = False    # whether doc_stream_open was sent
    _doc_last_len = 0      # last content length sent
    # Set when the loop runs out of rounds while the agent was still actively
    # using tools — i.e. it was cut off, not finished. Drives a "Continue" event
    # so the user can resume instead of the turn silently stalling.
    _exhausted_rounds = False

    for round_num in range(1, max_rounds + 1):
        round_response = ""
        round_reasoning = ""  # reasoning_content deltas (DeepSeek-thinking, vLLM --reasoning-parser)
        native_tool_calls = []  # populated if model uses function calling
        # Reset doc streaming state per round
        _doc_acc = ""
        _doc_opened = False
        _doc_last_len = 0
        _doc_fence_offset = 0  # offset into round_response for text-fence content
        # Cursor for the multi-block scanner — when a `create_document`
        # fenced block closes we advance this so the next iteration can
        # detect a SUBSEQUENT block in the same round.
        _doc_scan_from = 0

        # Merge native tool schemas with MCP tool schemas, filtering out
        # Only send function schemas for API models (OpenAI, Anthropic, etc.).
        # Local models use fenced code blocks or <tool_code> — schemas add overhead.
        if _force_answer:
            # Loop-breaker decided the model has enough info but keeps
            # calling tools. Send NO tools this round so it's forced to
            # write the answer instead of flailing further.
            all_tool_schemas = []
        elif _is_api_model:
            # Filter schemas by RAG-selected tools (if available)
            if _relevant_tools:
                # _build_base_prompt unions _ADMIN_TOOLS into the prompt
                # sections when admin intent fires — the schema list must
                # offer the same names, or the model reads prose describing
                # tools it cannot call and substitutes the nearest schema
                # it does have (e.g. manage_memory for manage_skills).
                _schema_names = set(_relevant_tools)
                if _needs_admin:
                    _schema_names |= _ADMIN_TOOLS
                base_schemas = [
                    s for s in FUNCTION_TOOL_SCHEMAS
                    if s.get("function", {}).get("name") in _schema_names
                ]
                _mcp_filtered = [
                    s for s in mcp_schemas
                    if s.get("function", {}).get("name") in _relevant_tools
                ]
                all_tool_schemas = base_schemas + _mcp_filtered
            else:
                base_schemas = FUNCTION_TOOL_SCHEMAS if _needs_admin else [
                    s for s in FUNCTION_TOOL_SCHEMAS
                    if s.get("function", {}).get("name") not in _ADMIN_SCHEMA_NAMES
                ]
                all_tool_schemas = base_schemas + mcp_schemas
            if disabled_tools:
                    all_tool_schemas = [
                        t for t in all_tool_schemas
                        if t.get("function", {}).get("name") not in disabled_tools
                        and t.get("name") not in disabled_tools
                    ]

            # Append skill schemas (dynamic ECC skills)
            try:
                from src.skills.registry import get_registry
                _reg = get_registry()
                if _reg.loaded and _reg.tool_names:
                    _skill_schemas = _reg.get_all_schemas()
                    if disabled_tools:
                        _skill_schemas = [
                            s for s in _skill_schemas
                            if s.get("function", {}).get("name") not in disabled_tools
                        ]
                    all_tool_schemas.extend(_skill_schemas)
            except Exception:
                pass
        else:
            # Local: only MCP schemas when message suggests MCP tool usage
            _last_content = _last_user.lower()
            _wants_mcp = any(kw in _last_content for kw in _MCP_KEYWORDS)
            all_tool_schemas = mcp_schemas if (_wants_mcp and mcp_schemas) else []
        agent_stream_timeout = int(get_setting("agent_stream_timeout_seconds", 300) or 300)

        _tool_names_sent = [t.get("function", {}).get("name") for t in (all_tool_schemas or []) if t.get("function")]
        logger.info(f"[agent-debug] round={round_num} model={model} _is_api_model={_is_api_model} tools_sent={len(_tool_names_sent)} tool_names={_tool_names_sent[:15]} relevant_tools={sorted(_relevant_tools)[:15] if _relevant_tools else 'ALL'}")

        # Primary target + any configured fallback models. stream_llm_with_fallback
        # only switches on a pre-content failure, so streamed output is never
        # duplicated; the dead-host cooldown keeps repeat primary attempts cheap.
        _candidates = [(endpoint_url, model, headers)] + list(fallbacks or [])
        # stream_llm enforces a per-read INACTIVITY timeout (httpx read=timeout),
        # which kills a wedged/silent endpoint. This wall-clock deadline is the
        # complementary cap for the rare stream that trickles bytes forever and
        # so never trips the inactivity timeout. Generous — only catches runaway.
        _round_deadline = time.time() + max(agent_stream_timeout * 4, 1200)
        _round_start = time.time()
        _round_first_event_logged = False
        _round_first_token_logged = False
        logger.info(
            "[agent-timing] round_start round=%s model=%s endpoint=%s prompt_tokens=%s tools=%s native_tools=%s timeout=%s",
            round_num,
            model,
            endpoint_url,
            estimate_tokens(messages),
            len(_tool_names_sent),
            bool(all_tool_schemas),
            agent_stream_timeout,
        )
        async for chunk in stream_llm_with_fallback(
            _candidates,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_type=prompt_type if round_num == 1 else None,
            tools=all_tool_schemas if all_tool_schemas else None,
            timeout=agent_stream_timeout,
            session_id=session_id,
            workload=workload,
        ):
            if not _round_first_event_logged:
                _round_first_event_logged = True
                logger.info(
                    "[agent-timing] first_event round=%s elapsed=%.3fs kind=%s",
                    round_num,
                    time.time() - _round_start,
                    "error" if chunk.startswith("event: error") else "data",
                )
            if time.time() > _round_deadline:
                logger.warning(
                    "[agent-timing] round_deadline round=%s elapsed=%.3fs deadline_s=%s",
                    round_num,
                    time.time() - _round_start,
                    max(agent_stream_timeout * 4, 1200),
                )
                break
            # Forward error events from stream_llm to the frontend
            if chunk.startswith("event: error"):
                logger.warning(
                    "[agent-timing] stream_error round=%s elapsed=%.3fs chunk=%r",
                    round_num,
                    time.time() - _round_start,
                    chunk[:500],
                )
                yield chunk
                continue
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                try:
                    data = json.loads(chunk[6:])
                    # IMPORTANT: check type-based events BEFORE "delta" key,
                    # because tool_call_delta also has an "arg_delta" field.
                    if data.get("type") == "tool_call_delta":
                        if tool_policy and tool_policy.blocks(data.get("name")):
                            continue
                        # Stream document content to frontend as AI generates it
                        logger.debug(f"tool_call_delta: name={data.get('name')}, len(arg_delta)={len(data.get('arg_delta', ''))}")
                        _doc_acc += data.get("arg_delta", "")
                        if not _doc_opened:
                            tm = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', _doc_acc)
                            if tm:
                                _doc_opened = True
                                try:
                                    title = json.loads('"' + tm.group(1) + '"')
                                except Exception:
                                    title = tm.group(1)
                                lm = re.search(r'"language"\s*:\s*"((?:[^"\\]|\\.)*)"', _doc_acc)
                                lang = ""
                                if lm:
                                    try:
                                        lang = json.loads('"' + lm.group(1) + '"')
                                    except Exception:
                                        lang = lm.group(1)
                                logger.info(f"Doc streaming: open title={title!r} lang={lang!r}")
                                yield f'data: {json.dumps({"type": "doc_stream_open", "title": title, "language": lang})}\n\n'
                        if _doc_opened:
                            cm = re.search(r'"content"\s*:\s*"', _doc_acc)
                            if cm:
                                raw = _doc_acc[cm.end():]
                                raw = re.sub(r'"\s*\}\s*$', '', raw)
                                try:
                                    decoded = json.loads('"' + raw + '"')
                                except Exception:
                                    try:
                                        decoded = json.loads('"' + raw.rstrip('\\') + '"')
                                    except Exception:
                                        decoded = raw.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
                                if len(decoded) > _doc_last_len:
                                    _doc_last_len = len(decoded)
                                    yield f'data: {json.dumps({"type": "doc_stream_delta", "content": decoded})}\n\n'
                    elif data.get("type") == "tool_calls":
                        native_tool_calls = data.get("calls", [])
                        logger.info(f"Agent round {round_num}: received {len(native_tool_calls)} native tool call(s)")
                    elif data.get("type") == "usage":
                        u = data.get("data", {})
                        actual_model = u.get("model") or actual_model
                        round_input = u.get("input_tokens", 0)
                        real_input_tokens += round_input
                        real_output_tokens += u.get("output_tokens", 0)
                        last_round_input_tokens = round_input
                        has_real_usage = True
                        # Backend-reported TRUE generation speed (llama.cpp
                        # timings.predicted_per_second) — pure decode, excludes
                        # prefill/network. Preferred over tokens/wall-clock, which
                        # reads low. Keep the last round's value (the gen phase).
                        if u.get("gen_tps"):
                            backend_gen_tps = u["gen_tps"]
                        if u.get("prefill_tps"):
                            backend_prefill_tps = u["prefill_tps"]
                    elif data.get("type") == "fallback":
                        # The selected model failed and another answered; surface
                        # the notice so a misconfigured provider isn't masked.
                        actual_model = data.get("answered_by") or actual_model
                        logger.warning(f"[agent] round {round_num} fell back: "
                                       f"{data.get('selected_model')} -> {data.get('answered_by')}")
                        yield chunk
                    elif data.get("type") == "model_actual":
                        actual_model = data.get("model") or actual_model
                        data["requested_model"] = requested_model
                        yield f"data: {json.dumps(data)}\n\n"
                    elif "delta" in data:
                        if not first_token_received:
                            time_to_first_token = time.time() - total_start
                            first_token_received = True
                        if not _round_first_token_logged:
                            _round_first_token_logged = True
                            logger.info(
                                "[agent-timing] first_visible_token round=%s elapsed=%.3fs total_elapsed=%.3fs thinking=%s",
                                round_num,
                                time.time() - _round_start,
                                time.time() - total_start,
                                bool(data.get("thinking")),
                            )
                        # Keep reasoning deltas in a separate accumulator so
                        # we can echo them back via `reasoning_content` on the
                        # next request (DeepSeek requires this; harmless for
                        # other vendors). Regular content still flows into
                        # round_response unchanged.
                        if data.get("thinking"):
                            round_reasoning += data["delta"]
                        else:
                            round_response += data["delta"]
                            full_response += data["delta"]
                            yield f"data: {json.dumps(data)}\n\n"
                        # Detect text-fence doc streaming. Normal agent prompts
                        # use ```create_document; the doc LoRA streaming path
                        # uses neutral ```document to avoid triggering learned
                        # hidden native tool-call output.
                        if (
                            round_num > 1
                            and not _doc_acc
                            and not (tool_policy and tool_policy.blocks("create_document"))
                        ):
                            _fence_markers = ('```create_document\n',)
                            _fence_marker = None
                            for _mk in _fence_markers:
                                _candidate = _mk[0] if isinstance(_mk, tuple) else _mk
                                if _candidate in round_response[_doc_scan_from:]:
                                    _fence_marker = _candidate
                                    break
                            # Open a new block if we're not currently inside one
                            # and there's an unstreamed marker in the response.
                            # The marker search starts at the byte after the
                            # last block's closing fence so the SECOND
                            # `create_document` block in the same round gets
                            # detected (previously only the first one was
                            # streamed and the rest were silently dropped).
                            if not _doc_opened and _fence_marker:
                                _fi = round_response.index(_fence_marker, _doc_scan_from)
                                _fa = round_response[_fi + len(_fence_marker):]
                                _fl = _fa.split('\n')
                                if _fl and _fl[0].strip():
                                    _doc_opened = True
                                    _ft = _fl[0].strip()
                                    _kl = {'python','py','javascript','js','typescript','ts','html','css','json','yaml','bash','sql','rust','go','java','c','cpp','markdown','text'}
                                    _flang = _fl[1].strip() if len(_fl) > 1 and _fl[1].strip().lower() in _kl else ''
                                    _doc_fence_offset = _fi + len(_fence_marker) + len(_fl[0]) + 1
                                    if _flang:
                                        _doc_fence_offset += len(_fl[1]) + 1
                                    _doc_last_len = 0
                                    yield f'data: {json.dumps({"type": "doc_stream_open", "title": _ft, "language": _flang})}\n\n'
                            if _doc_opened:
                                _rc = round_response[_doc_fence_offset:]
                                _ci = _rc.find('\n```')
                                if _ci >= 0:
                                    _rc = _rc[:_ci]
                                if len(_rc) > _doc_last_len:
                                    _doc_last_len = len(_rc)
                                    yield f'data: {json.dumps({"type": "doc_stream_delta", "content": _rc})}\n\n'
                                # If the closing fence has arrived, finalise
                                # this block and arm detection of the NEXT
                                # one. The model can emit multiple
                                # `create_document` blocks in a single round.
                                if _ci >= 0:
                                    _doc_opened = False
                                    _doc_scan_from = _doc_fence_offset + _ci + len('\n```')
                                    _doc_fence_offset = 0
                                    _doc_last_len = 0
                    elif data.get("error"):
                        err_msg = data.get("error", "unknown")
                        logger.error(f"Agent round {round_num}: stream error: {err_msg}")
                        yield f'data: {json.dumps({"delta": chr(10) + chr(10) + "*[Stream error: " + str(err_msg) + "]*"})}\n\n'
                except json.JSONDecodeError:
                    if round_num == 1:
                        yield chunk
            elif chunk.startswith("event: "):
                # Forward error events to frontend as visible text
                yield chunk
            # Intercept [DONE] — don't forward until all rounds finish

        logger.info(
            "[agent-timing] round_stream_done round=%s elapsed=%.3fs text_chars=%s tool_calls=%s first_event=%s first_token=%s",
            round_num,
            time.time() - _round_start,
            len(round_response),
            len(native_tool_calls),
            _round_first_event_logged,
            _round_first_token_logged,
        )
        _normalized_doc_round = round_response
        tool_blocks, used_native, converted_calls = _resolve_tool_blocks(
            _normalized_doc_round,
            native_tool_calls,
            round_num,
            is_api_model=(_is_api_model and not guide_only),
            # SAO: prefer native, but still accept fenced blocks if model falls back
            # (or document LoRA path which is fence-only).
            allow_fenced_for_api=_allow_fenced_fallback,
        )

        # --- NATIVE TOOL CALL RETRY ---
        # If model emitted native tool calls but ALL failed conversion (even after auto-repair),
        # inject correction and retry. Track retry count to prevent infinite loops.
        if native_tool_calls and not tool_blocks and not _force_answer and round_num < max_rounds:
            _native_retries = getattr(_native_retries_holder, "_n", 0) + 1
            object.__setattr__(_native_retries_holder, "_n", _native_retries)
            if _native_retries <= 2:
                failed_count = len(native_tool_calls)
                logger.warning(
                    f"[agent] round {round_num}: all {failed_count} native tool call(s) failed to parse — "
                    f"retry {_native_retries}/2"
                )
                # Save the assistant's (malformed) response so model sees what it tried
                if round_response.strip():
                    messages.append({"role": "assistant", "content": round_response})
                # Clear round_response so we don't output empty text
                round_response = ""
                # Inject correction
                correction = (
                    "Your previous response contained tool calls, but they could not be parsed due to "
                    "malformed JSON arguments. Focus ONLY on fixing the JSON format and retry. "
                    "Example: bash -> {\"command\": \"ls -la\"}"
                )
                messages.append({"role": "user", "content": correction})
                continue  # re-enter loop (costs 1 round budget)
            else:
                logger.warning(f"[agent] round {round_num}: tool call parse retry exhausted (3/3), proceeding without tools")

        # Force-answer round: we told the model to STOP calling tools and
        # answer. If it ignored that and emitted a (possibly DSML) tool
        # call anyway, discard it — don't execute, don't re-loop. Keep
        # only the prose; if there's none, emit a graceful fallback.
        if _force_answer:
            if tool_blocks:
                logger.info(f"[agent] force-answer round {round_num}: discarding {len(tool_blocks)} ignored tool call(s)")
            tool_blocks = []
            if not _strip_think_blocks(strip_tool_blocks(round_response)).strip():
                # The model burned its budget gathering data but never wrote a
                # final answer (common with weaker models on multi-source
                # briefings). Salvage it: one blunt non-streaming synthesis call
                # over the full conversation (which already holds every tool
                # result) before falling back to the canned apology.
                _synth = ""
                try:
                    from src.llm_core import llm_call_async
                    _synth_messages = list(messages) + [{
                        "role": "user",
                        "content": (
                            "Using ONLY the information already gathered above, write "
                            "the final answer for the user now. Do NOT call any tools, "
                            "do NOT explain your reasoning — output the finished response "
                            "directly. If some data couldn't be fetched, just work with "
                            "what you have and note what's missing in one short line."
                        ),
                    }]
                    _raw = await llm_call_async(
                        url=endpoint_url, model=model, messages=_synth_messages,
                        headers=headers, temperature=0.3, max_tokens=max_tokens, timeout=60,
                    )
                    _synth = _strip_think_blocks(strip_tool_blocks(_raw or "")).strip()
                except Exception as _e:
                    logger.warning(f"[agent] grace synthesis failed: {_e}")
                if _synth:
                    yield f'data: {json.dumps({"delta": _synth})}\n\n'
                    full_response += _synth
                else:
                    _fb = ("I gathered some search results but couldn't pull a clean "
                           "answer together. Want me to try a more specific question, "
                           "or summarize what I did find?")
                    yield f'data: {json.dumps({"delta": _fb})}\n\n'
                    full_response += _fb

        # ── Fallback: auto-create document if model dumped large code in chat ──
        # If no create_document tool was used, check for big code blocks in text
        has_doc_tool = any(
            b.tool_type in ("create_document", "update_document")
            for b in tool_blocks
        ) or any(
            tc.get("name") in ("create_document", "update_document")
            for tc in native_tool_calls
        )
        if not has_doc_tool and session_id and "create_document" not in (disabled_tools or set()):
            _code_block_re = re.compile(r'```(\w*)\n([\s\S]*?)```')
            for m in _code_block_re.finditer(round_response):
                lang_tag = m.group(1).lower()
                code_body = m.group(2).strip()
                # Skip small blocks and known tool tags
                if code_body.count('\n') < 30:
                    continue
                if lang_tag in TOOL_TAGS:
                    continue  # already handled as a tool execution
                # Auto-create a document from this code block
                lang_map = {"py": "python", "js": "javascript", "ts": "typescript", "": "text"}
                doc_lang = lang_map.get(lang_tag, lang_tag or "text")
                doc_title = f"Code ({doc_lang})"
                tb = ToolBlock("create_document", f"{doc_title}\n{doc_lang}\n{code_body}")
                tool_blocks.append(tb)
                # Stream the document open event
                yield f'data: {json.dumps({"type": "doc_stream_open", "title": doc_title, "language": doc_lang})}\n\n'
                yield f'data: {json.dumps({"type": "doc_stream_delta", "content": code_body})}\n\n'
                logger.info(f"Auto-created document from {lang_tag} code block ({code_body.count(chr(10))+1} lines)")
                break  # only auto-create one document per round

        # Save cleaned round text for history persistence
        # Keep <think> blocks so they render in the thinking section on reload
        # Mirror the same fenced-pattern gate used to resolve tool_blocks above:
        # an illustrative fence that wasn't executed (because this is a native
        # model with no real native_tool_calls) must not be stripped from the
        # persisted text either — otherwise it streams once and then disappears
        # on reload (#3222 follow-up).
        cleaned_round = strip_tool_blocks(round_response, skip_fenced=(_is_api_model and not used_native and not guide_only)).strip()
        round_texts.append(cleaned_round)

        if not tool_blocks:
            break  # no tools — done
        # ── Circuit breaker (error normalization + duplicate call safety) ──
        _sig = "|".join(sorted(f"{b.tool_type}:{(b.content or '').strip()[:120]}" for b in tool_blocks))
        _is_repeat = _sig in _recent_call_sigs
        _recent_call_sigs.append(_sig)
        _real_text = _strip_think_blocks(cleaned_round).strip()

        # Analyze error patterns from this round's tool results
        # Process ONLY newly added events (not entire history) to avoid
        # double-counting the same event across multiple rounds.
        # Also safely handle non-int exit_code values (e.g. "n/a").
        _round_has_error = False
        _round_all_errors = bool(tool_blocks)
        _new_event_count = len(tool_events) - getattr(_events_processed_marker, "_n", 0)
        if _new_event_count > 0:
            for _ev in tool_events[-_new_event_count:]:
                _ev_exit = _ev.get("exit_code")
                _ev_err = _ev.get("error")
                _is_error_ev = bool(_ev_err) or (isinstance(_ev_exit, (int, float)) and _ev_exit != 0)
                if _is_error_ev:
                    _round_has_error = True
                    # Normalize error signature: strip volatile parts
                    _err_text = (_ev_err or _ev.get("output") or "")[:300]
                    _err_norm = re.sub(r"line \d+", "line N", _err_text)
                    _err_norm = re.sub(r"port \d+", "port N", _err_norm)
                    _err_norm = re.sub(r"pid \d+", "pid N", _err_norm)
                    _err_norm = re.sub(r"/tmp/[^ ]+", "/tmp/...", _err_norm)
                    _err_norm = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "x.x.x.x", _err_norm)
                    _error_signatures[_err_norm[:120]] += 1
                else:
                    _round_all_errors = False
        else:
            _round_all_errors = False

        if not _real_text and not _round_has_error:
            _round_all_errors = False  # no tool errors = not "all errors"

        # Track stagnation metrics
        if _is_repeat and not _real_text:
            _stuck_rounds += 1
        else:
            _stuck_rounds = 0

        if _round_all_errors and _round_has_error:
            _consecutive_error_rounds += 1
        else:
            _consecutive_error_rounds = 0

        if not _real_text and (not tool_events or _round_all_errors):
            _no_progress_rounds += 1
        else:
            _no_progress_rounds = 0

        # ── Circuit breaker triggers ──
        _cb_tripped = False
        _cb_reason = ""

        # Update events-processed marker for next round
        object.__setattr__(_events_processed_marker, "_n", len(tool_events))

        # 1. Duplicate call loop (same as before)
        if _stuck_rounds >= 3:
            _cb_tripped = True
            _cb_reason = f"repeated same tool call {_stuck_rounds}x without new text"

        # 2. Same error flooding
        elif _error_signatures:
            _most_common_err, _count = _error_signatures.most_common(1)[0]
            if _count >= _CB_MAX_SAME_ERROR:
                _cb_tripped = True
                _cb_reason = f"same error occurred {_count}x: {_most_common_err[:100]}"

        # 3. Consecutive all-error rounds
        elif _consecutive_error_rounds >= _CB_MAX_ERROR_ROUNDS:
            _cb_tripped = True
            _cb_reason = f"{_consecutive_error_rounds} consecutive rounds with only errors"

        # 4. No progress at all
        elif _no_progress_rounds >= _CB_MAX_NO_PROGRESS:
            _cb_tripped = True
            _cb_reason = f"{_no_progress_rounds} rounds with zero useful output"

        if _cb_tripped:
            logger.warning(f"[agent] circuit breaker tripped on round {round_num}: {_cb_reason}")
            _force_answer = True
            _escalation_msg = (
                f"The agent loop is stuck ({_cb_reason}). "
                "STOP calling tools immediately. Write your best final answer NOW "
                "from the information already gathered. If you have nothing useful, "
                "tell the user exactly what went wrong and what they should try next."
            )
            messages.append({"role": "system", "content": _escalation_msg})
            yield f'data: {json.dumps({"type": "circuit_breaker", "round": round_num, "reason": _cb_reason})}\n\n'
            continue


        # Pre-stream document content for fenced tool blocks (non-native path)
        # Native path already streamed via tool_call_delta above
        # For round 1 fenced blocks, frontend fence detection already handled streaming
        if not _doc_opened and round_num == 1:
            for block in tool_blocks:
                if tool_policy and tool_policy.blocks(block.tool_type):
                    continue
                if block.tool_type == "create_document":
                    _doc_opened = True
                    break

        if not _doc_opened:
            for block in tool_blocks:
                if tool_policy and tool_policy.blocks(block.tool_type):
                    continue
                if block.tool_type == "create_document":
                    lines = block.content.strip().split("\n")
                    title = lines[0].strip() if lines else "Untitled"
                    lang = ""
                    content_start = 1
                    if len(lines) > 1 and len(lines[1].strip()) < 20 and lines[1].strip().isalpha():
                        lang = lines[1].strip()
                        content_start = 2
                    content = "\n".join(lines[content_start:]) if len(lines) > content_start else ""
                    yield f'data: {json.dumps({"type": "doc_stream_open", "title": title, "language": lang})}\n\n'
                    if content:
                        yield f'data: {json.dumps({"type": "doc_stream_delta", "content": content})}\n\n'
                    break
                elif block.tool_type == "update_document":
                    # Pre-stream the full replacement content so user sees it immediately
                    content = block.content.strip()
                    yield f'data: {json.dumps({"type": "doc_stream_open", "title": "", "language": ""})}\n\n'
                    yield f'data: {json.dumps({"type": "doc_stream_delta", "content": content})}\n\n'
                    break

        # Execute each tool block (discipline + early loop detection)
        from src.constants import MAX_TOOLS_PER_ROUND, MAX_WEB_FETCH_PER_SESSION, MAX_WEB_SEARCH_PER_SESSION, MAX_BASH_PER_SESSION
        tool_results = []
        tool_result_texts = []  # plain text for native tool role messages
        budget_hit = False
        _round_tool_calls = 0
        _seen_signatures = set()
        
        # Session counters live on stream_agent_loop scope (_session_tool_counts)
        for i, block in enumerate(tool_blocks):
            # --- Round-level tool cap ---
            if _round_tool_calls >= MAX_TOOLS_PER_ROUND:
                logger.warning(
                    f"[agent] round {round_num}: capped at {MAX_TOOLS_PER_ROUND} tool calls, "
                    f"dropping {len(tool_blocks) - i} remaining blocks"
                )
                break
            # --- Session-level tool type caps ---
            t_type = block.tool_type
            if t_type == "web_fetch" and _session_tool_counts.get("web_fetch", 0) >= MAX_WEB_FETCH_PER_SESSION:
                logger.warning(f"[agent] round {round_num}: max web_fetch ({MAX_WEB_FETCH_PER_SESSION}) reached, skipping")
                # Inform model so it adapts — append to messages, not just log
                if _capped_tools_informed.get("web_fetch", 0) < 1:
                    _capped_tools_informed["web_fetch"] = _capped_tools_informed.get("web_fetch", 0) + 1
                    _cap_note = f"[SYSTEM] web_fetch calls exhausted ({MAX_WEB_FETCH_PER_SESSION}/{MAX_WEB_FETCH_PER_SESSION}). Use bash+git clone (with user confirmation) or read_file to inspect local files instead."
                    messages.append({"role": "system", "content": _cap_note})
                continue
            if t_type == "web_search" and _session_tool_counts.get("web_search", 0) >= MAX_WEB_SEARCH_PER_SESSION:
                logger.warning(f"[agent] round {round_num}: max web_search ({MAX_WEB_SEARCH_PER_SESSION}) reached, skipping")
                if _capped_tools_informed.get("web_search", 0) < 1:
                    _capped_tools_informed["web_search"] = _capped_tools_informed.get("web_search", 0) + 1
                    _cap_note = f"[SYSTEM] web_search calls exhausted ({MAX_WEB_SEARCH_PER_SESSION}/{MAX_WEB_SEARCH_PER_SESSION}). All results already gathered. Synthesize answer from what you have."
                    messages.append({"role": "system", "content": _cap_note})
                continue
            if t_type in ("bash", "python") and _session_tool_counts.get(t_type, 0) >= MAX_BASH_PER_SESSION:
                logger.warning(f"[agent] round {round_num}: max {t_type} ({MAX_BASH_PER_SESSION}) reached, skipping")
                if _capped_tools_informed.get(t_type, 0) < 1:
                    _capped_tools_informed[t_type] = _capped_tools_informed.get(t_type, 0) + 1
                    _cap_note = f"[SYSTEM] {t_type} calls exhausted ({MAX_BASH_PER_SESSION}/{MAX_BASH_PER_SESSION}). Use read-only tools or synthesize answer from gathered data."
                    messages.append({"role": "system", "content": _cap_note})
                continue

            # --- Early duplicate detection ---
            sig = f"{block.tool_type}:{(block.content or '').strip()[:120]}"
            if sig in _seen_signatures:
                logger.warning(
                    f"[agent] round {round_num}: duplicate tool call ({sig[:60]}), early break"
                )
                break
            _seen_signatures.add(sig)
            _round_tool_calls += 1
            _session_tool_counts[t_type] = _session_tool_counts.get(t_type, 0) + 1
            # --- Global tool budget check ---
            if max_tool_calls > 0 and total_tool_calls >= max_tool_calls:
                yield f'data: {json.dumps({"type": "budget_exceeded", "limit": max_tool_calls, "used": total_tool_calls})}\n\n'
                budget_hit = True
                break

            total_tool_calls += 1
            # Build a short display string for the frontend tool bubble.
            # Document tools show a brief summary instead of dumping full content.
            is_doc_tool = block.tool_type in ("create_document", "update_document", "edit_document", "suggest_document")
            full_command = block.content.strip()
            if is_doc_tool:
                cmd_display = block.content.split("\n")[0].strip()[:80]
            else:
                cmd_display = full_command

            if tool_policy and tool_policy.blocks(block.tool_type):
                desc = f"{block.tool_type}: BLOCKED"
                result = {
                    "error": tool_policy.reason_for(block.tool_type),
                    "exit_code": 1,
                    "blocked": True,
                }
                logger.info("Tool blocked before start by policy: %s", block.tool_type)
            else:
                yield (
                    f'data: {json.dumps({"type": "tool_start", "tool": block.tool_type, "command": cmd_display, "full_command": full_command, "round": round_num})}\n\n'
                )

                # Streaming progress for long-running tools (bash, python).
                # The bash/python branches inside _direct_fallback emit
                # periodic {elapsed_s, tail} payloads via this callback;
                # we forward each one as a `tool_progress` SSE event so
                # the UI can render live elapsed-time + tail-of-output.
                _progress_q: asyncio.Queue = asyncio.Queue()
                async def _push_progress(payload):
                    await _progress_q.put(payload)

                async def _run_tool():
                    try:
                        return await execute_tool_block(
                            block,
                            session_id=session_id,
                            disabled_tools=disabled_tools,
                            tool_policy=tool_policy,
                            owner=owner,
                            progress_cb=_push_progress,
                            workspace=workspace,
                        )
                    finally:
                        # Sentinel so the drainer knows to stop.
                        await _progress_q.put(None)

                _tool_task = asyncio.create_task(_run_tool())
                try:
                    # Drain progress events as they arrive — block until the
                    # next event OR the tool finishes (sentinel = None).
                    while True:
                        evt = await _progress_q.get()
                        if evt is None:
                            break
                        yield (
                            f'data: {json.dumps({"type": "tool_progress", "tool": block.tool_type, "round": round_num, **evt})}\n\n'
                        )
                    desc, result = await _tool_task

                    # ── PostToolUse Hooks (Async fire-and-forget) ──
                    try:
                        from src.hooks.registry import get_registry as _get_hook_reg
                        _hr = _get_hook_reg()
                        if _hr.loaded:
                            _tool_name = getattr(block, 'tool_type', '')
                            _tool_args = getattr(block, 'content', '')
                            _ = _hr.run_post_tool(_tool_name, _tool_args, desc, result)
                    except Exception as _hook_e:
                        logger.debug("PostToolUse hook failed: %s", _hook_e)

                finally:
                    # If the SSE client disconnects (or this generator is
                    # otherwise closed) while we're awaiting a progress event
                    # above, GeneratorExit is thrown in right here and the
                    # `await _tool_task` on the line above never runs — the
                    # task (and any subprocess execute_tool_block spawned for
                    # bash/python tools) would otherwise keep running
                    # orphaned with nothing left to await or cancel it.
                    if not _tool_task.done():
                        _tool_task.cancel()
                        try:
                            await _tool_task
                        except (asyncio.CancelledError, Exception):
                            pass

            # A skill the model just loaded can prescribe tools that weren't
            # RAG-selected this turn (declared via requires_toolsets in its
            # frontmatter). Union them into the selection so the NEXT round's
            # schema list includes them — otherwise the model reads "use
            # grep" from the skill it fetched but has no grep schema to call.
            if (
                block.tool_type == "manage_skills"
                and _relevant_tools is not None
                and not result.get("error")
            ):
                _ms_args = {}
                _ms_raw = (block.content or "").strip()
                if _ms_raw.startswith("{"):
                    try:
                        _ms_args = json.loads(_ms_raw)
                    except json.JSONDecodeError:
                        _ms_args = {}
                _ms_name = str(_ms_args.get("name", "") or "").strip()
                if _ms_name and _ms_args.get("action") in ("view", "view_ref"):
                    try:
                        from services.memory.skills import SkillsManager as _SkM
                        from src.constants import DATA_DIR as _DD
                        from src.tool_policy import known_tool_names as _ktn
                        _known = _ktn()
                        for _sk in _SkM(_DD).load(owner=owner):
                            if _sk.get("name") == _ms_name:
                                _new = {
                                    t for t in (_sk.get("requires_toolsets") or [])
                                    if t in _known and t not in _relevant_tools
                                }
                                if _new:
                                    _relevant_tools.update(_new)
                                    logger.info(
                                        "[tool-rag] skill '%s' unlocked tools for next round: %s",
                                        _ms_name, sorted(_new),
                                    )
                                break
                    except Exception as _e:
                        logger.debug(f"skill requires_toolsets unlock skipped: {_e}")

            # Extract structured web sources from web_search tool output.
            # web_search returns {"output": ..., "exit_code": 0}; check "output"
            # first so the <!-- SOURCES:…--> marker is found and stripped even
            # when the result doesn't carry a "results" or "stdout" key.
            _src_text = result.get("output") or result.get("results") or result.get("stdout") or ""
            if block.tool_type == "web_search" and _src_text:
                _src_marker = "<!-- SOURCES:"
                _src_idx = _src_text.find(_src_marker)
                if _src_idx >= 0:
                    _src_end = _src_text.find(" -->", _src_idx)
                    if _src_end >= 0:
                        try:
                            _extracted_sources = json.loads(_src_text[_src_idx + len(_src_marker):_src_end])
                            yield f'data: {json.dumps({"type": "web_sources", "data": _extracted_sources})}\n\n'
                            # Strip the marker from the result so it doesn't show in chat
                            _clean = _src_text[:_src_idx].rstrip()
                            if "output" in result:
                                result["output"] = _clean
                            elif "results" in result:
                                result["results"] = _clean
                            elif "stdout" in result:
                                result["stdout"] = _clean
                        except (json.JSONDecodeError, Exception):
                            pass

            # Emit doc-specific event for document tools — the frontend
            # document panel handles this; no need to show content in chat.
            if is_doc_tool and "action" in result:
                if result["action"] == "suggest":
                    yield (
                        f'data: {json.dumps({"type": "doc_suggestions", "doc_id": result["doc_id"], "suggestions": result["suggestions"]})}\n\n'
                    )
                else:
                    yield (
                        f'data: {json.dumps({"type": "doc_update", "doc_id": result["doc_id"], "content": result["content"], "version": result["version"], "title": result.get("title", ""), "language": result.get("language")})}\n\n'
                    )

            # Emit ui_control event for frontend to apply UI changes
            if "ui_event" in result:
                yield (
                    f'data: {json.dumps({"type": "ui_control", "data": result})}\n\n'
                )

            # ask_user: remember the payload now, but emit the interactive event
            # only *after* tool_output below.  Emitting it before tool_output let
            # the subsequent tool-card rewrite/scroll push the choices out of
            # view.  The payload is also copied into the persisted tool event so
            # history reload can reconstruct an unanswered card.
            _pending_ask_user_event = None
            if "ask_user" in result:
                # The question lives in the tool args. ChatMessage.to_dict()
                # replays only role+content to the model next turn — tool_event
                # metadata is dropped — so if the question is never in the saved
                # assistant text, the model can't see it already asked and will
                # loop and re-ask after the user answers. Stream it as assistant
                # text (once) so it persists and is replayed. The card shows the
                # options only, so this is the single visible copy of the question.
                _auq = result["ask_user"]
                _auq_q = (_auq.get("question") or "").strip()
                if _auq_q and _auq_q not in full_response:
                    _auq_delta = ("\n\n" if full_response.strip() else "") + _auq_q
                    full_response += _auq_delta
                    yield 'data: ' + json.dumps({"delta": _auq_delta}) + '\n\n'
                _pending_ask_user_event = _auq
                _awaiting_user = True

            # update_plan: agent wrote back to the plan (ticked a step / revised).
            # Push it to the frontend so the stored plan + docked window update
            # live. Does NOT end the turn — the agent keeps working.
            if "plan_update" in result:
                yield (
                    f'data: {json.dumps({"type": "plan_update", "data": result["plan_update"]})}\n\n'
                )

            # Build output for frontend tool bubble.
            # Document tools get a short summary — content goes to the editor panel.
            output_text = ""
            if is_doc_tool and "action" in result:
                action = result["action"]
                title = result.get("title", "")
                ver = result.get("version", "?")
                if action == "create":
                    output_text = f'Document created: "{title}" (v{ver})'
                elif action == "edit":
                    output_text = f'Document edited: "{title}" (v{ver}, {result.get("applied", 0)} edit(s))'
                elif action == "update":
                    output_text = f'Document updated: "{title}" (v{ver})'
            elif "stdout" in result:
                # On a bash/python timeout the result carries error + (often
                # empty) stdout/stderr; fall back to the error so the "timed
                # out" reason reaches the UI instead of a blank result.
                raw = result["stdout"] or result["stderr"] or result.get("error", "")
                output_text = _smart_truncate(raw)
            elif "output" in result:
                # bash / python canonical result: {"output": ..., "exit_code": ...}
                raw = result["output"] or ""
                output_text = _smart_truncate(raw)
            elif "response" in result:
                # AI interaction tools (chat_with_model, send_to_session)
                label = result.get("model", result.get("session_name", "AI"))
                output_text = _smart_truncate(f"{label}: {result['response']}")
            elif "content" in result:
                output_text = _smart_truncate(result["content"])
            elif "results" in result:
                output_text = _smart_truncate(result["results"])
            elif "session_id" in result and "name" in result:
                output_text = f"Session created: {result['name']} (id: {result['session_id']})"
            elif "success" in result:
                output_text = (
                    f"Written: {result.get('path', '')}"
                    if result["success"]
                    else f"Error: {result.get('error', '')}"
                )
            elif "error" in result:
                output_text = _smart_truncate(result["error"])

            # Emit tool_output (include ui_event data if present)
            tool_output_data = {"type": "tool_output", "tool": block.tool_type, "command": cmd_display, "output": output_text, "exit_code": result.get("exit_code")}
            if is_doc_tool and "action" in result:
                tool_output_data.update({
                    "doc_id": result.get("doc_id"),
                    "document_action": result.get("action"),
                    "document_title": result.get("title", ""),
                    "document_language": result.get("language", ""),
                    "document_version": result.get("version"),
                    "document_content": result.get("content", ""),
                })
            if _pending_ask_user_event:
                # Keep enough state in the streamed tool result for alternate
                # clients to render the prompt without depending on event order.
                tool_output_data["ask_user"] = _pending_ask_user_event
            if "ui_event" in result:
                tool_output_data["ui_event"] = result["ui_event"]
                for k in (
                    "toggle_name", "state", "mode", "model", "endpoint_url",
                    "theme_name", "colors",
                    # ui_control open_email_reply payload — without these the
                    # frontend openReplyDraft bails on undefined uid and the
                    # reply window silently never opens.
                    "uid", "folder", "account_id",
                    # Optional pre-filled body for open_email_reply so the
                    # agent can compose-and-open in one tool call.
                    "body",
                    # ui_control open_panel payload
                    "panel",
                ):
                    if k in result:
                        tool_output_data[k] = result[k]
            # Forward image data from generate_image tool
            for k in ("image_url", "image_prompt", "image_model", "image_size", "image_quality"):
                if k in result:
                    tool_output_data[k] = result[k]
            # Forward screenshots from browser tools (base64 images)
            if result.get("images"):
                img = result["images"][0]
                tool_output_data["screenshot"] = f"data:{img['mimeType']};base64,{img['data']}"
            # Forward a file-write diff for inline before/after rendering
            if "diff" in result:
                tool_output_data["diff"] = result["diff"]
            yield f'data: {json.dumps(tool_output_data)}\n\n'

            if block.tool_type == "manage_notes":
                _notes_action = ""
                try:
                    _notes_args = json.loads(block.content or "{}")
                    if isinstance(_notes_args, dict):
                        _notes_action = str(_notes_args.get("action") or "").lower()
                except Exception:
                    _notes_action = ""
                _notes_text = ""
                if not result.get("error"):
                    if _notes_action in {"list", "search", "find", "view", "lis"}:
                        _notes_text = _note_list_summary_from_tool_output(
                            result.get("output") or result.get("results") or result.get("content") or ""
                        )
                    elif _notes_action in {"add", "update", "delete", "toggle_item"}:
                        _notes_text = str(
                            result.get("response")
                            or result.get("output")
                            or result.get("results")
                            or ""
                        ).strip()
                        if _notes_text.startswith("AI: "):
                            _notes_text = _notes_text[4:].strip()
                        if _notes_text and not re.match(r"^(done|note|item|deleted)\b", _notes_text, re.IGNORECASE):
                            _notes_text = f"Done — {_notes_text}"
                if _notes_text:
                    _clean_current = strip_tool_blocks(full_response).strip()
                    if _notes_text not in _clean_current:
                        _prefix = "\n\n" if _clean_current else ""
                        full_response = (_clean_current + _prefix + _notes_text).strip()
                        yield f'data: {json.dumps({"delta": _prefix + _notes_text})}\n\n'

            # This must be the final UI event for ask_user: the frontend appends
            # the card below the now-settled tool node and cancels any between-
            # round spinner.  The turn ends after the current tool batch.
            if _pending_ask_user_event:
                yield (
                    f'data: {json.dumps({"type": "ask_user", "data": _pending_ask_user_event})}\n\n'
                )

            # Native document tools open in the editor + carry the REAL doc id.
            # Emit a doc_update so the frontend opens/activates it and sends it
            # back as active_doc_id next turn (otherwise the agent can't "see"
            # the document it just created on the follow-up message).
            if block.tool_type in ("create_document", "update_document", "edit_document") and result.get("doc_id"):
                yield (
                    'data: ' + json.dumps({
                        "type": "doc_update",
                        "doc_id": result["doc_id"],
                        "title": result.get("title", ""),
                        "language": result.get("language", ""),
                        "content": result.get("content", ""),
                        "version": result.get("version", 1),
                    }) + '\n\n'
                )

            # Inline research: emit the open-link as part of the assistant's
            # actual response text — a `#research-<id>` anchor that chatRenderer
            # turns into a regular clickable link. Saved with the message, so it
            # PERSISTS across refresh (unlike the old ephemeral injected chip).
            _rsid = result.get("research_session_id")
            if _rsid:
                _anchor = f"\n\n[Open in Deep Research](#research-{_rsid})\n"
                yield 'data: ' + json.dumps({"delta": _anchor}) + '\n\n'

            # Same pattern for notes: when manage_notes creates a note
            # and returns note_id, drop a `[View note](#note-<id>)` link
            # into the stream so chatRenderer's click handler routes to
            # the new openNote() in notes.js — opens the notes panel and
            # scrolls/flashes the matching card. Without this, the agent
            # would write "View note" as a phrase with no target.
            _nid = result.get("note_id")
            if _nid and block.tool_type == "manage_notes":
                _title = (result.get("note_title") or "").strip()
                _label = f"View note: {_title}" if _title else "View note"
                _anchor = f"\n\n[{_label}](#note-{_nid})\n"
                full_response = (full_response.rstrip() + _anchor).strip()
                yield 'data: ' + json.dumps({"delta": _anchor}) + '\n\n'

            # Save for history persistence
            tool_event = {
                "round": round_num,
                "tool": block.tool_type,
                "command": cmd_display,
                "output": output_text,
                "exit_code": result.get("exit_code"),
            }
            if result.get("image_url"):
                for ik in ("image_url", "image_prompt", "image_model", "image_size", "image_quality"):
                    if result.get(ik):
                        tool_event[ik] = result[ik]
            if result.get("doc_id"):
                tool_event["doc_id"] = result["doc_id"]
                tool_event["doc_title"] = result.get("title", "")
            # Persist the file-write/edit diff so it re-renders on reload — without
            # this the diff shows live but vanishes from saved history.
            if result.get("diff"):
                tool_event["diff"] = result["diff"]
            if _pending_ask_user_event:
                # Persist the structured question with the tool event.  On a
                # reload, chatRenderer can restore the card; a later user
                # message removes it as answered.
                tool_event["ask_user"] = _pending_ask_user_event
            tool_events.append(tool_event)

            formatted = format_tool_result(desc, result)
            tool_results.append(formatted)
            tool_result_texts.append(formatted)


        # If budget was hit, stop the loop
        if budget_hit:
            break

        # ask_user posed a question — stop here and wait for the user's choice.
        # Don't feed tool results back or advance a round; the user's selection
        # arrives as the next message and the agent resumes from there. The
        # question text is already in the streamed response, so it persists.
        if _awaiting_user:
            break


        # Feed results back to LLM for next round
        # Pass the CONVERTED calls (aligned 1:1 with tool_result_texts), not the
        # raw native_tool_calls: a call that failed to convert is dropped from
        # tool_blocks but stayed in native_tool_calls, so indexing results by
        # native position mis-attached each result to the wrong tool_call_id
        # (and left the real call answered empty).
        _append_tool_results(messages, round_response, converted_calls,
                             tool_results, tool_result_texts, used_native, round_num,
                             round_reasoning=round_reasoning)

        # Emit agent_step event
        yield (
            f'data: {json.dumps({"type": "agent_step", "round": round_num + 1})}\n\n'
        )

        # Intra-round context compression.
        # Prevent context ballooning across 10+ tool rounds by compressing
        # Intra-round context compression.
        try:
            from src.context_compression import compress_messages

            # ── PreCompact Hooks (state persist/save before compaction) ──
            try:
                from src.hooks.registry import get_registry as _get_hook_reg
                _hr = _get_hook_reg()
                if _hr.loaded:
                    _actions = await _hr.run_pre_compact({"round": round_num, "tokens": estimate_tokens(messages)})
                    if _actions:
                        logger.info("[hooks] PreCompact actions: %s", _actions)
            except Exception as _hook_e:
                logger.debug("PreCompact hook check failed: %s", _hook_e)

            _comp_budget = soft_budget if soft_budget > 0 else 6000
            # Only trigger compression above threshold
            if estimate_tokens(messages) > int(_comp_budget * 0.85):
                _comp_target = int(_comp_budget * 0.50)
                before_b = estimate_tokens(messages)
                messages = compress_messages(messages, target_tokens=_comp_target, keep_last_rounds=3)
                after_b = estimate_tokens(messages)
                logger.info(
                    "[context-compress] intra-round compressed %s -> %s tokens (target=%s, budget=%s)",
                    before_b, after_b, _comp_target, _comp_budget,
                )
        except Exception as e:
            logger.debug(f"[context-compress] intra-round skipped: {e}")

        # Separator in accumulated response
        full_response += "\n\n"
    else:
        # The for-loop completed every allowed round WITHOUT an early `break`
        # (a `break` fires on "done", budget, or error). Reaching this `else`
        # means the agent kept working until it ran out of rounds — so offer
        # Continue instead of stopping silently. This catches ALL exhaustion
        # paths, including a verifier `continue` on the final round (the old
        # bottom-of-loop flag missed those).
        _exhausted_rounds = True

    # If the loop hit the round cap while still working, tell the client so it
    # can show a "Continue" affordance instead of the turn just stopping.
    if _exhausted_rounds:
        logger.info("[agent] round cap (%d) reached mid-task — emitting rounds_exhausted", max_rounds)
        yield f'data: {json.dumps({"type": "rounds_exhausted", "rounds": max_rounds})}\n\n'

    # If the response is completely empty and no tools were executed,
    # yield a fallback message so the user is not left hanging.
    full_response, _fallback_chunk = _empty_response_fallback(
        full_response, round_reasoning, tool_events,
        messages=messages, endpoint_url=endpoint_url,
        model=actual_model, headers=headers,
        temperature=0.3, max_tokens=max_tokens
    )
    if _fallback_chunk:
        yield _fallback_chunk

    # Do not persist raw textual tool-call JSON / role markers as assistant
    # prose. Local finetunes may emit those before the parser catches and
    # executes them; saved history should contain only the user-facing answer.
    full_response = strip_tool_blocks(full_response).strip()


    # --- Final metrics ---
    total_duration = time.time() - total_start
    metrics = _compute_final_metrics(
        messages, full_response, total_duration, time_to_first_token,
        context_length, real_input_tokens, real_output_tokens,
        has_real_usage, tool_events, round_texts, model=actual_model,
        last_round_input_tokens=last_round_input_tokens,
        prep_timings=prep_timings,
        backend_gen_tps=backend_gen_tps,
        backend_prefill_tps=backend_prefill_tps,
    )
    metrics["requested_model"] = requested_model
    yield f"data: {json.dumps({'type': 'metrics', 'data': metrics})}\n\n"

    # Teacher-escalation: inline takeover visible in the chat stream.
    # The student just finished; if Tier 1 flags failure, the teacher
    # gets a turn (with its own tool calls forwarded to the user) and
    # a skill is saved ONLY if the teacher actually succeeds. Skipped
    # when we ARE the teacher to avoid recursion.
    if not _is_teacher_run and not guide_only:
        try:
            from src.teacher_escalation import run_teacher_inline
            async for evt in run_teacher_inline(
                student_endpoint_url=endpoint_url,
                student_messages=messages,
                student_tool_events=tool_events,
                student_reply=full_response,
                owner=owner,
            ):
                yield evt
        except Exception as _esc_err:
            logger.warning(f"teacher escalation hook failed: {_esc_err}", exc_info=True)

    yield "data: [DONE]\n\n"

    # ── Stop Hooks (auto-save, log, downstream triggers) ──
    try:
        from src.hooks.registry import get_registry as _get_hook_reg
        _hr = _get_hook_reg()
        if _hr.loaded:
            _stop_results = await _hr.run_stop(summary=full_response[:500])
            if _stop_results:
                for _sr in _stop_results:
                    if _sr.errors:
                        logger.info("[hooks] Stop hook errors: %s", _sr.errors)
    except Exception as _hook_e:
        logger.debug("Stop hook check failed: %s", _hook_e)
