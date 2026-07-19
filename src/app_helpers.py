# src/app_helpers.py
import base64
import logging
import os
import secrets

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from core.constants import APP_VERSION

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "static", "templates")
)


def _read_include(path: str) -> str:
    """Read a partial template, allowing only files under static/templates/."""
    # Strip leading slash for join
    rel = path.lstrip("/")
    full = os.path.realpath(os.path.join(_TEMPLATES_DIR, rel))
    if not full.startswith(os.path.realpath(_TEMPLATES_DIR) + os.sep):
        logger.warning("Blocked include path escape: %s", path)
        return f"<!-- blocked: {path} -->"
    if not os.path.isfile(full):
        logger.warning("Include not found: %s", full)
        return f"<!-- not found: {path} -->"
    with open(full, "r", encoding="utf-8") as f:
        return f.read()

def read_if_exists(path: str) -> str:
    """Read file if it exists, return empty string otherwise."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

def file_to_data_url(path: str, mime: str) -> str:
    """Convert file to data URL."""
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"

def abs_join(base_dir: str, rel: str) -> str:
    """Join paths and return absolute path."""
    return os.path.abspath(os.path.join(base_dir, rel))

def serve_html_with_nonce(request: Request, file_path: str) -> HTMLResponse:
    """Read an app-bundled HTML page and inject the CSP nonce into inline <script> tags.

    Callers pass fixed, server-owned template paths (index/login/backgrounds),
    never a client-supplied path. So any read failure here — a missing file
    (broken deployment) or a permission/IO error — is a server fault, not a
    client "not found": map all of them to a logged 500 so a missing core
    template surfaces in 5xx alerting instead of hiding behind a 404. If a
    future caller serves a client-influenced path where 404 is correct, branch
    that at the call site rather than defaulting this shared helper to 404.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        logger.exception("Failed to read page %s", file_path)
        raise HTTPException(500, "Internal server error")
    nonce = getattr(request.state, "csp_nonce", "")
    if not nonce:
        nonce = secrets.token_hex(16)
        request.state.csp_nonce = nonce
    html = html.replace("{{CSP_NONCE}}", nonce)
    html = html.replace("{{APP_VERSION}}", APP_VERSION)
    # Inline partial include pattern: <include path="/static/templates/head.html" />
    import re
    html = re.sub(
        r'<include\s+path="([^"]+)"\s*/>',
        lambda m: _read_include(m.group(1)),
        html,
    )
    return HTMLResponse(html)


def inside_base_dir(base_dir: str, path: str) -> bool:
    """Check if path is inside base directory."""
    if not isinstance(base_dir, str) or not isinstance(path, str):
        return False
    base = os.path.realpath(base_dir)
    p = os.path.realpath(path)
    try:
        return os.path.commonpath([base, p]) == base
    except Exception:
        return False
