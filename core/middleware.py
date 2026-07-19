# core/middleware.py
# Shared middleware, decorators, and request helpers

import asyncio
import logging
import os
import secrets
import time

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response


# Per-process token that lets the in-app tool layer hit admin-gated
# routes via HTTP loopback (the agent's tool calls don't carry the
# admin user's session cookie). Set once at import; tools read the
# same value from this module. Never persisted or exposed externally.
INTERNAL_TOOL_TOKEN = os.environ.get("ODYSSEUS_INTERNAL_TOKEN") or secrets.token_hex(32)
INTERNAL_TOOL_HEADER = "X-Odys-Internal-Token"
# Pseudo-username on in-process tool-loopback requests; require_admin trusts it and it is reserved.
INTERNAL_TOOL_USER = "internal-tool"


def is_cors_preflight(method: str, headers) -> bool:
    """True for a genuine CORS preflight: an OPTIONS request carrying the
    Access-Control-Request-Method header. Such requests are credential-less by
    design and must reach CORSMiddleware to be answered -- gating them on auth
    401s the preflight and breaks every cross-origin browser/WebView client.
    Pure so it can be unit-tested without standing up the app."""
    return method == "OPTIONS" and "access-control-request-method" in headers


def require_admin(request: Request):
    """Raise 403 if the current user isn't an admin.
    Allows access when auth is explicitly disabled, or when the request carries
    the in-process internal-tool token used by loopback agent tools.
    """
    # In-process bypass for tool-layer loopback calls. Two paths:
    # (a) header-direct (caller set X-Odys-Internal-Token), or
    # (b) the auth middleware already validated the token and stamped
    #     request.state.current_user = "internal-tool".
    try:
        hdr = request.headers.get(INTERNAL_TOOL_HEADER)
        if hdr and secrets.compare_digest(hdr, INTERNAL_TOOL_TOKEN):
            return
        if getattr(request.state, "current_user", None) == INTERNAL_TOOL_USER:
            return
    except Exception:
        pass

    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return
    if not auth_mgr or not auth_mgr.is_configured:
        raise HTTPException(403, "Admin only")
    user = getattr(request.state, "current_user", None)
    if not user or not auth_mgr.is_admin(user):
        raise HTTPException(403, "Admin only")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to all responses."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Pre-generate CSP nonce BEFORE the route handler so that inline
        # serve_html_with_nonce (called inside a route) and the response CSP
        # header share the SAME nonce. Otherwise nonce mismatch blocks all scripts.
        request.state.csp_nonce = secrets.token_hex(16)

        response = await call_next(request)
        path = request.url.path

        # Tool render endpoints
        is_tool_render = path.startswith("/api/tools/") and path.endswith("/render")
        # Document library PDF preview endpoint
        is_document_pdf_preview = path.startswith("/api/document/") and path.endswith("/render-pdf")
        # Visual report pages are self-contained HTML — need inline scripts + external images
        is_report = path.startswith("/api/research/report/")

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"

        is_https = (
            request.url.scheme == "https"
            or request.headers.get("X-Forwarded-Proto") == "https"
        )
        if is_https:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        if is_report:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "font-src 'self'; "
                "img-src 'self' data: blob: https:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
        elif is_tool_render:
            # Skip framing headers for tools.
            pass
        elif is_document_pdf_preview:
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; "
                "frame-ancestors 'self'"
            )
        else:
            response.headers["X-Frame-Options"] = "DENY"
            # NOTE: `style-src 'unsafe-inline'` is intentionally retained.
            # `static/index.html` and `static/login.html` ship inline <style>
            # blocks, and several JS modules build runtime `style=""` attrs.
            # Migrating to nonce-only requires templating the HTML files +
            # auditing every JS-set style attribute. Since inline styles
            # don't execute script, the residual risk is visual-only.
            content_type = response.headers.get("content-type", "").lower()
            if content_type.startswith("text/html"):
                nonce = getattr(request.state, "csp_nonce", secrets.token_hex(16))
                request.state.csp_nonce = nonce
                script_src = f"'self' 'nonce-{nonce}' https://cdn.jsdelivr.net"
            else:
                # API JSON responses don't need inline script execution —
                # skip the nonce to save entropy and header bytes.
                script_src = "'self' https://cdn.jsdelivr.net"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src {script_src}; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "img-src 'self' data: blob: https:; "
                "media-src 'self' blob:; "
                "connect-src 'self'; "
                "frame-src 'self'; "
                "frame-ancestors 'none'"
            )
        return response


# ═══════════════════════════════════════════════════════════════
# Middleware classes extracted from app.py (H2 refactor)
# ═══════════════════════════════════════════════════════════════

REQUEST_TIMEOUT_DEFAULT = float(os.getenv("REQUEST_HARD_TIMEOUT", "45"))

# Runtime-mutable set of route prefixes exempt from the hard timeout.
# Routes can append at module level::
#
#     from core.middleware import _TIMEOUT_EXEMPT_PREFIXES
#     _TIMEOUT_EXEMPT_PREFIXES.add("/api/foo/stream")
#
# The @timeout_exempt decorator is a no-op marker for documentation.
_TIMEOUT_EXEMPT_PREFIXES: set[str] = {
    "/api/chat",
    "/api/shell/stream",
    "/api/research",
    "/api/model/download",
    "/api/model/probe",
    "/api/model-endpoints",
    "/api/cookbook/setup",
    "/api/upload",
    "/api/image",
    "/api/memory/audit",
}


def timeout_exempt(route_func):
    """Decorator: mark a route as exempt from the hard request timeout."""
    return route_func


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    """Abort requests exceeding REQUEST_HARD_TIMEOUT (default 45s).
    Whitelisted streaming/long-running paths are exempt."""

    async def dispatch(self, request, call_next):
        path = request.url.path or ""
        if any(path.startswith(p) for p in _TIMEOUT_EXEMPT_PREFIXES):
            return await call_next(request)
        try:
            return await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_DEFAULT)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"detail": f"Request exceeded {REQUEST_TIMEOUT_DEFAULT:.0f}s timeout"},
                status_code=504,
            )


class InteractiveActivityMiddleware(BaseHTTPMiddleware):
    """Pause background tasks during interactive foreground requests."""

    async def dispatch(self, request, call_next):
        from src.interactive_gate import should_track_interactive_request, track_interactive_request

        path = request.url.path or ""
        if not should_track_interactive_request(path, request.method):
            return await call_next(request)

        async def _stop_bg():
            try:
                ts = getattr(request.app.state, "_task_scheduler", None)
                if ts:
                    await ts.stop_background_tasks_for_foreground(
                        reason=f"foreground request {request.method} {path}"
                    )
            except Exception:
                logging.getLogger("app.foreground_gate").debug(
                    "foreground task stop failed", exc_info=True
                )

        asyncio.create_task(_stop_bg())
        async with track_interactive_request(path, request.method):
            return await call_next(request)


class SlowRequestLogMiddleware(BaseHTTPMiddleware):
    """Log requests that take longer than ODYSSEUS_SLOW_REQUEST_LOG_SECONDS (default 0.75s)."""

    async def dispatch(self, request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = getattr(response, "status_code", 0) or 0
            return response
        finally:
            elapsed = time.perf_counter() - start
            try:
                threshold = float(
                    os.getenv("ODYSSEUS_SLOW_REQUEST_LOG_SECONDS", "0.75") or "0.75"
                )
            except Exception:
                threshold = 0.75
            if elapsed >= threshold:
                logging.getLogger("app.slow_request").warning(
                    "slow_request method=%s path=%s status=%s elapsed=%.3fs",
                    request.method,
                    request.url.path,
                    status,
                    elapsed,
                )
