"""Chat routes package.

Re-exports all public + private symbols from chat_routes.py
for backward compatibility with existing imports.
"""

# Re-export from helpers sub-module
from .helpers import (
    _active_streams,
    _stream_set,
    _message_plain_text,
    _last_user_plain_text,
    _ensure_current_request_is_latest_user,
    _WEB_FOLLOWUP_RE,
    _RECENT_WEB_CONTEXT_RE,
    _recent_session_text,
    _is_contextual_web_followup,
    _resolve_request_workspace,
    _session_url_matches_endpoint,
    _clear_orphaned_session_endpoint,
    _endpoint_cache_contains_model,
    _is_image_generation_session,
    _recover_empty_session_model,
    _set_user_time_from_request,
)

# Re-export from routes sub-module
from .routes import setup_chat_routes

__all__ = [
    "_active_streams",
    "_stream_set",
    "_message_plain_text",
    "_last_user_plain_text",
    "_ensure_current_request_is_latest_user",
    "_WEB_FOLLOWUP_RE",
    "_RECENT_WEB_CONTEXT_RE",
    "_recent_session_text",
    "_is_contextual_web_followup",
    "_resolve_request_workspace",
    "_session_url_matches_endpoint",
    "_clear_orphaned_session_endpoint",
    "_endpoint_cache_contains_model",
    "_is_image_generation_session",
    "_recover_empty_session_model",
    "_set_user_time_from_request",
    "setup_chat_routes",
]
