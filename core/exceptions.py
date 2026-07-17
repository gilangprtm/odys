# core/exceptions.py
"""Custom exceptions for the application — all inherit from AppError."""


class AppError(Exception):
    """Base class for all application-level errors.

    All custom exceptions in this module inherit from ``AppError`` so
    callers can catch them generically with ``except AppError`` when
    they don't need per-type handling. Subclasses SHOULD accept a
    ``detail`` dict for structured error context.
    """

    def __init__(self, message: str, detail: dict | None = None):
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


class SessionNotFoundError(AppError):
    """Raised when a requested session is not found."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session '{session_id}' not found", detail={"session_id": session_id})


class InvalidFileUploadError(AppError):
    """Raised when a file upload fails validation."""

    def __init__(self, message: str, filename: str | None = None):
        self.filename = filename
        self.message = message
        super().__init__(message, detail={"filename": filename} if filename else None)


class LLMServiceError(AppError):
    """Raised when there is an error communicating with the LLM service."""

    def __init__(self, message: str, endpoint: str | None = None):
        self.endpoint = endpoint
        self.message = message
        super().__init__(message, detail={"endpoint": endpoint} if endpoint else None)


class WebSearchError(AppError):
    """Raised when there is an error with web search functionality."""

    def __init__(self, message: str, query: str | None = None):
        self.query = query
        self.message = message
        super().__init__(message, detail={"query": query} if query else None)
