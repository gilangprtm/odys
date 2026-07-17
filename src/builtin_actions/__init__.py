"""
builtin_actions package.

Registry of built-in automation actions that can be executed by the task
scheduler without needing an LLM call.
"""

# ── Exceptions ──────────────────────────────────────────────────────────────
from .exceptions import TaskNoop, TaskDeferred  # noqa: F401

# ── Email actions module ────────────────────────────────────────────────────
from .email_actions import (  # noqa: F401
    # Tidy / utility
    action_tidy_sessions,
    action_tidy_documents,
    action_consolidate_memory,
    action_ssh_command,
    action_run_script,
    action_run_local,
    action_tidy_research,
    action_tidy_calendar,

    # Internal helpers used by tests and actions.py
    _result_has_work,
    _result_is_config_error,
    _email_task_account_id,

    # Email processing
    action_summarize_emails,
    action_draft_email_replies,
    action_email_auto_translate,

    # Event classification
    _classify_event_heuristic,
    _memory_context_lines,
    action_classify_events,
    action_ping_events,
    action_extract_email_events,

    # Sender signatures
    _SIG_SKIP_PREFIXES,
    action_learn_sender_signatures,
)

# ── Core actions module ─────────────────────────────────────────────────────
from .actions import (  # noqa: F401
    action_daily_brief,
    action_test_skills,
    action_audit_skills,
    action_ping_notes,
    action_check_email_urgency,
    action_cookbook_serve,
    action_neuron_decay,
    action_neuron_vault_sync,
    BUILTIN_ACTION_INFO,
)

# ── Registry ────────────────────────────────────────────────────────────────
# Build BUILTIN_ACTIONS dict with real function references. Must happen
# after all imports to avoid circular imports with .email_actions.
from . import actions as _actions

_actions.BUILTIN_ACTIONS.update({
    "tidy_sessions": action_tidy_sessions,
    "tidy_documents": action_tidy_documents,
    "consolidate_memory": action_consolidate_memory,
    "tidy_research": action_tidy_research,
    "summarize_emails": action_summarize_emails,
    "draft_email_replies": action_draft_email_replies,
    "email_auto_translate": action_email_auto_translate,
    "extract_email_events": action_extract_email_events,
    "classify_events": action_classify_events,
    "daily_brief": action_daily_brief,
    "learn_sender_signatures": action_learn_sender_signatures,
    "ssh_command": action_ssh_command,
    "run_script": action_run_script,
    "run_local": action_run_local,
    "test_skills": action_test_skills,
    "audit_skills": action_audit_skills,
    "check_email_urgency": action_check_email_urgency,
    "cookbook_serve": action_cookbook_serve,
    "neuron_decay": action_neuron_decay,
    "neuron_vault_sync": action_neuron_vault_sync,
})

# Re-export BUILTIN_ACTIONS from the module level so direct imports work.
BUILTIN_ACTIONS = _actions.BUILTIN_ACTIONS  # noqa: F811
