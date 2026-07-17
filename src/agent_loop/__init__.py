"""src.agent_loop package — split from monolit agent_loop.py (H4).

Public surface preserved for routes/tests:
  stream_agent_loop, TOOL_SECTIONS, get_builtin_overrides, build_active_plan_note,
  plus private helpers imported by tests.
"""

from src.agent_loop.prompts import (
    TOOL_SECTIONS,
    get_builtin_overrides,
    _section_text,
    _compact_tool_line,
    _assemble_prompt,
    AGENT_SYSTEM_PROMPT,
    _AGENT_PREAMBLE,
    _AGENT_RULES,
    _API_AGENT_RULES,
    _LINK_RULES,
    _DOMAIN_RULES,
    _DOMAIN_TOOL_MAP,
    _domain_rules_for_tools,
    _API_HOSTS,
    _MCP_KEYWORDS,
    _ADMIN_SCHEMA_NAMES,
    _TOOL_SELECTION_TIMEOUT_SECONDS,
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
    _ADMIN_KEYWORDS,
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

from src.agent_loop.loop import stream_agent_loop

# Re-export FUNCTION_TOOL_SCHEMAS for tests that import it via agent_loop
from src.agent_tools import FUNCTION_TOOL_SCHEMAS  # noqa: F401

__all__ = [
    "stream_agent_loop",
    "TOOL_SECTIONS",
    "get_builtin_overrides",
    "build_active_plan_note",
    "AGENT_SYSTEM_PROMPT",
    "FUNCTION_TOOL_SCHEMAS",
    "_DOMAIN_TOOL_MAP",
    "_API_HOSTS",
    "_classify_agent_request",
    "_compute_final_metrics",
    "_empty_response_fallback",
    "_append_tool_results",
    "_detect_runaway_call",
    "_normalize_stream_document_fences",
    "_build_system_prompt",
    "_strip_think_blocks",
    "_EXPLICIT_CONTINUATION_RE",
    "_is_explicit_continuation",
    "_endpoint_lookup_keys",
    "_is_ollama_openai_compat_url",
    "_recent_context_for_retrieval",
]
