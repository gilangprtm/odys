"""
app.py — Slim orchestrator (~300 lines).

Refactored (H2):
  - core/middleware.py     ⟵ middleware classes
  - src/app_lifespan.py    ⟵ lifespan + startup/shutdown
  - src/app_routes.py      ⟵ route registration
"""

import asyncio
import logging
import logging.handlers
import mimetypes
import os
import re
import secrets
import sys

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

# ── Early platform setup ───────────────────────────────────────

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")

if os.name == "nt":
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# ── .env ───────────────────────────────────────────────────────

load_dotenv(encoding="utf-8-sig")

# ── Core imports ───────────────────────────────────────────────

from core.constants import BASE_DIR, STATIC_DIR, DATA_DIR, REQUEST_TIMEOUT, OPENAI_API_KEY, SESSIONS_FILE, AUTH_FILE
from core.database import SessionLocal, ApiToken
from core.middleware import (
    SecurityHeadersMiddleware,
    RequestTimeoutMiddleware,
    InteractiveActivityMiddleware,
    SlowRequestLogMiddleware,
    is_cors_preflight,
)
from core.auth import AuthManager, normalize_known_username
from src.app_helpers import abs_join, serve_html_with_nonce
from src.app_lifespan import setup_lifespan
from src.app_routes import mount_static, register_exception_handlers, register_inline_routes, register_route_routers

# ── Logging ────────────────────────────────────────────────────

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
for _h in list(_root_logger.handlers):
    _root_logger.removeHandler(_h)
_console_h = logging.StreamHandler()
_console_h.setFormatter(_formatter)
_root_logger.addHandler(_console_h)

try:
    _log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _file_h = logging.handlers.RotatingFileHandler(
        os.path.join(_log_dir, "app.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_h.setFormatter(_formatter)
    _root_logger.addHandler(_file_h)
except Exception as e:
    _root_logger.warning(f"File logging init failed (console-only): {e}")

logger = logging.getLogger(__name__)

# ── FastAPI app ────────────────────────────────────────────────

app = FastAPI(title="Odysseus", description="AI chat + memory + research", version="1.0.0")

# ── CORS ───────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=[
        "Accept", "Authorization", "Content-Type", "X-API-Key",
        "X-Auth-Token", "X-Odys-Internal-Token", "X-Odysseus-Owner",
        "X-Requested-With", "X-TZ-Offset",
    ],
)

# ── Response compression ───────────────────────────────────────

app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

# ── Security headers ───────────────────────────────────────────

app.add_middleware(SecurityHeadersMiddleware)

# ── Internal middleware (timeout, interactive gate, slow-log) ──

app.add_middleware(RequestTimeoutMiddleware)
app.add_middleware(InteractiveActivityMiddleware)
app.add_middleware(SlowRequestLogMiddleware)

# ── Auth setup ─────────────────────────────────────────────────

from routes.auth_routes import setup_auth_routes, SESSION_COOKIE

auth_manager = AuthManager()
app.state.auth_manager = auth_manager
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() != "false"
LOCALHOST_BYPASS = os.getenv("LOCALHOST_BYPASS", "false").lower() == "true"

if AUTH_ENABLED:
    AUTH_EXEMPT_EXACT = {
        "/api/auth/setup", "/api/auth/signup", "/api/auth/login",
        "/api/auth/logout", "/api/auth/status", "/api/auth/features",
        "/api/auth/settings", "/api/auth/integrations/presets",
        "/api/health", "/api/version", "/login",
    }
    AUTH_EXEMPT_PREFIXES = ["/static", "/assets", "/legacy"]
    AUTH_EXEMPT_PATTERNS = [re.compile(r"^/api/tasks/[^/]+/webhook/[^/]+/?$")]

    def _is_auth_exempt(path: str) -> bool:
        if path in AUTH_EXEMPT_EXACT:
            return True
        if any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
            return True
        return any(p.match(path) for p in AUTH_EXEMPT_PATTERNS)

    # Token cache
    _token_cache: dict = {}
    _token_cache_lock = asyncio.Lock()
    app.state._token_cache_dirty = True

    def _token_cache_invalidate():
        app.state._token_cache_dirty = True

    app.state.invalidate_token_cache = _token_cache_invalidate

    def _refresh_token_cache():
        from collections import defaultdict

        new_map = defaultdict(list)
        db = SessionLocal()
        try:
            rows = db.query(ApiToken).filter(ApiToken.is_active == True).all()
            for r in rows:
                owner_key = normalize_known_username(auth_manager.users, getattr(r, "owner", None))
                if not owner_key:
                    logger.warning("Ignoring active API token '%s' for unknown user '%s'", getattr(r, "id", ""), getattr(r, "owner", None))
                    continue
                scopes = [s.strip() for s in (getattr(r, "scopes", "") or "chat").split(",") if s.strip()]
                new_map[r.token_prefix].append((r.id, r.token_hash, owner_key, scopes))
        finally:
            db.close()
        _token_cache.clear()
        _token_cache.update(new_map)
        app.state._token_cache_dirty = False

    _PROXY_FWD_HEADERS = (
        "cf-connecting-ip", "cf-ray", "cf-visitor",
        "x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded",
    )

    def _is_trusted_loopback(request: Request) -> bool:
        host = request.client.host if request.client else None
        if host not in ("127.0.0.1", "::1"):
            return False
        for _h in _PROXY_FWD_HEADERS:
            if request.headers.get(_h):
                return False
        return True

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            if is_cors_preflight(request.method, request.headers):
                return await call_next(request)
            if _is_auth_exempt(path):
                return await call_next(request)
            try:
                from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN as _ITT, INTERNAL_TOOL_USER
                _hdr = request.headers.get(INTERNAL_TOOL_HEADER)
                if _hdr and secrets.compare_digest(_hdr, _ITT) and _is_trusted_loopback(request):
                    _impersonate = (request.headers.get("X-Odysseus-Owner") or "").strip()
                    _auth_mgr = getattr(request.app.state, "auth_manager", None) or auth_manager
                    if _impersonate and _impersonate in getattr(_auth_mgr, "users", {}):
                        request.state.current_user = _impersonate
                    else:
                        request.state.current_user = INTERNAL_TOOL_USER
                    request.state.api_token = False
                    return await call_next(request)
            except Exception:
                logger.warning("Internal tool auth header check failed", exc_info=True)
            if LOCALHOST_BYPASS and _is_trusted_loopback(request):
                return await call_next(request)
            if not auth_manager.is_configured:
                if not path.startswith("/api/"):
                    return RedirectResponse(url="/login", status_code=302)
                return JSONResponse(status_code=401, content={"error": "Setup required"})

            # --- Bearer token ---
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer ody_"):
                raw_token = auth_header[7:]
                if len(raw_token) < 12 or len(raw_token) > 100:
                    return JSONResponse(status_code=401, content={"error": "Invalid API token"})
                prefix = raw_token[:8]
                try:
                    if app.state._token_cache_dirty:
                        async with _token_cache_lock:
                            if app.state._token_cache_dirty:
                                await asyncio.to_thread(_refresh_token_cache)
                    candidates = list(_token_cache.get(prefix, ()))
                    import bcrypt as _bcrypt
                    matched_id = matched_owner = None
                    matched_scopes = []
                    for tid, thash, owner, scopes in candidates:
                        if _bcrypt.checkpw(raw_token.encode(), thash.encode()):
                            matched_id, matched_owner, matched_scopes = tid, owner, scopes
                            break
                    if matched_id:
                        async def _touch(tid):
                            def _do():
                                _db = SessionLocal()
                                try:
                                    _db.query(ApiToken).filter(ApiToken.id == tid).update({"last_used_at": datetime.now(timezone.utc).replace(tzinfo=None)})
                                    _db.commit()
                                finally:
                                    _db.close()
                            try:
                                await asyncio.to_thread(_do)
                            except Exception:
                                pass
                        asyncio.create_task(_touch(matched_id))
                        request.state.current_user = "api"
                        request.state.api_token = True
                        request.state.api_token_id = matched_id
                        request.state.api_token_owner = matched_owner
                        request.state.api_token_scopes = matched_scopes
                        return await call_next(request)
                except Exception:
                    logger.warning("API token auth error", exc_info=False)
                return JSONResponse(status_code=401, content={"error": "Invalid API token"})

            # --- Cookie session ---
            token = request.cookies.get(SESSION_COOKIE)
            if not auth_manager.validate_token(token):
                if path.startswith("/api/"):
                    return JSONResponse(status_code=401, content={"error": "Not authenticated"})
                return RedirectResponse(url="/login", status_code=302)
            request.state.current_user = auth_manager.get_username_for_token(token)
            request.state.api_token = False
            return await call_next(request)

    app.add_middleware(AuthMiddleware)
    logger.info("Auth middleware enabled (AUTH_ENABLED=true)")
else:
    # Single-user mode: always set request user to "admin" so the
    # rest of the app (tools, filesystem access) sees an admin owner
    # instead of None, which would block all non-admin tools.
    class DisabledAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.current_user = "admin"
            request.state.api_token = False
            return await call_next(request)
    app.add_middleware(DisabledAuthMiddleware)
    logger.info("Auth middleware disabled (set AUTH_ENABLED=true to enable)")

# ── Static files ───────────────────────────────────────────────

mount_static(app)

# ── YouTube init ───────────────────────────────────────────────

from services.youtube import init_youtube
init_youtube()

# ── RAG ────────────────────────────────────────────────────────

from src.rag_singleton import get_rag_manager
rag_manager = get_rag_manager()
rag_available = rag_manager is not None
logger.info("Vector document RAG %s", "initialized" if rag_available else "not available")

# ── Config ─────────────────────────────────────────────────────

from src.config import config

# ── Component initialization ───────────────────────────────────

from src.app_initializer import initialize_managers

components = initialize_managers(BASE_DIR, rag_manager)

session_manager = components["session_manager"]
from src.assistant_log import set_session_manager as _set_asst_sm
_set_asst_sm(session_manager)
from core.models import set_session_manager_instance
set_session_manager_instance(session_manager)
app.state.session_manager = session_manager

memory_manager = components["memory_manager"]
memory_vector = components.get("memory_vector")
upload_handler = components["upload_handler"]
app.state.upload_handler = upload_handler
personal_docs_mgr = components["personal_docs_manager"]
app.state.personal_docs_manager = personal_docs_mgr
api_key_manager = components["api_key_manager"]
preset_manager = components["preset_manager"]
chat_processor = components["chat_processor"]
research_handler = components["research_handler"]
app.state.research_handler = research_handler
chat_handler = components["chat_handler"]
model_discovery = components["model_discovery"]
skills_manager = components["skills_manager"]

# TTS
from services.tts import get_tts_service
tts_service = get_tts_service()
logger.info("TTS service initialized")

# ── Exception handlers ─────────────────────────────────────────

register_exception_handlers(app)

# ── Webhook manager ────────────────────────────────────────────

from src.webhook_manager import WebhookManager
webhook_manager = WebhookManager(api_key_manager=api_key_manager)

# ── Task scheduler ─────────────────────────────────────────────

from src.task_scheduler import TaskScheduler
task_scheduler = TaskScheduler(session_manager)
app.state._task_scheduler = task_scheduler

# ── MCP manager ────────────────────────────────────────────────

from src.mcp_manager import McpManager
mcp_manager = McpManager()

# ── STT ────────────────────────────────────────────────────────

from services.stt import get_stt_service
stt_service = get_stt_service()

# ── Register routes ────────────────────────────────────────────

from routes.auth_routes import setup_auth_routes
app.include_router(setup_auth_routes(auth_manager))

upload_cleanup_func = register_route_routers(
    app,
    auth_manager=auth_manager,
    session_manager=session_manager,
    chat_handler=chat_handler,
    chat_processor=chat_processor,
    memory_manager=memory_manager,
    memory_vector=memory_vector,
    research_handler=research_handler,
    upload_handler=upload_handler,
    webhook_manager=webhook_manager,
    api_key_manager=api_key_manager,
    preset_manager=preset_manager,
    skills_manager=skills_manager,
    model_discovery=model_discovery,
    personal_docs_mgr=personal_docs_mgr,
    rag_manager=rag_manager,
    rag_available=rag_available,
    tts_service=tts_service,
    stt_service=stt_service,
    task_scheduler=task_scheduler,
    mcp_manager=mcp_manager,
)

# ── Register inline routes ─────────────────────────────────────

register_inline_routes(app, auth_manager)

# ── Lifespan ───────────────────────────────────────────────────

setup_lifespan(app,
    upload_cleanup_func=upload_cleanup_func,
    webhook_manager=webhook_manager,
    mcp_manager=mcp_manager,
    task_scheduler=task_scheduler,
    skills_manager=skills_manager,
    model_discovery=model_discovery,
    upload_handler=upload_handler,
    auth_manager=auth_manager,
)

logger.info("App initialization complete")

# ── Main ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv("APP_BIND", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "7000")),
        log_level="info",
    )