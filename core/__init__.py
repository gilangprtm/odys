# core/__init__.py
"""
Chat Core — the essential chat experience.

This package contains only what's needed for:
- Streaming LLM responses
- Session management
- Model routing
- Authentication
"""

from .auth import AuthManager
from .constants import *
from .middleware import (
    SecurityHeadersMiddleware,
    RequestTimeoutMiddleware,
    InteractiveActivityMiddleware,
    SlowRequestLogMiddleware,
)
from .exceptions import (
    AppError,
    SessionNotFoundError,
    InvalidFileUploadError,
    LLMServiceError,
    WebSearchError,
)
from .models import Session, ChatMessage
from .session_manager import SessionManager

__all__ = [
    # Auth
    "AuthManager",
    # Middleware
    "SecurityHeadersMiddleware",
    "RequestTimeoutMiddleware",
    "InteractiveActivityMiddleware",
    "SlowRequestLogMiddleware",
    # Exceptions
    "AppError",
    "SessionNotFoundError",
    "InvalidFileUploadError",
    "LLMServiceError",
    "WebSearchError",
    # Models
    "Session",
    "ChatMessage",
    "SessionManager",
]
