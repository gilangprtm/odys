"""
src/app_routes.py — route registry extracted from app.py.

Keeps app.py under ~400 lines by moving all `app.include_router(...)` calls
into a single register_all_routes() function.
"""

import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse as _SRR

from core.constants import BASE_DIR, STATIC_DIR, REQUEST_TIMEOUT, OPENAI_API_KEY, SESSIONS_FILE
from src.app_helpers import abs_join, serve_html_with_nonce

logger = logging.getLogger(__name__)

# ── Static-file mount ──────────────────────────────────────────


class _RevalidatingStatic(StaticFiles):
    """Force revalidation of source files (.js/.css/.html)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.endswith((".js", ".css", ".html")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


def mount_static(app):
    os.makedirs(STATIC_DIR, exist_ok=True)
    app.mount("/static", _RevalidatingStatic(directory=STATIC_DIR), name="static")


# ── Exception handlers ─────────────────────────────────────────


def register_exception_handlers(app):
    from core.exceptions import (
        AppError,
        InvalidFileUploadError,
        LLMServiceError,
        SessionNotFoundError,
        WebSearchError,
    )

    @app.exception_handler(SessionNotFoundError)
    async def session_not_found(request, exc):
        return JSONResponse(status_code=404, content={"error": "SESSION_NOT_FOUND", "message": str(exc)})

    @app.exception_handler(InvalidFileUploadError)
    async def invalid_file_upload(request, exc):
        return JSONResponse(status_code=400, content={"error": "INVALID_FILE_UPLOAD", "message": str(exc)})

    @app.exception_handler(LLMServiceError)
    async def llm_service_error(request, exc):
        return JSONResponse(status_code=502, content={"error": "LLM_SERVICE_ERROR", "message": str(exc)})

    @app.exception_handler(WebSearchError)
    async def web_search_error(request, exc):
        return JSONResponse(status_code=502, content={"error": "WEB_SEARCH_ERROR", "message": str(exc)})

    @app.exception_handler(AppError)
    async def app_error_handler(request, exc):
        status_map = {
            "SessionNotFoundError": 404,
        }
        status = status_map.get(type(exc).__name__, 500)
        return JSONResponse(status_code=status, content={"error": type(exc).__name__, "message": str(exc), "detail": exc.detail})


# ── Inline routes kept in app.py ───────────────────────────────

_LEGACY_INDEX = abs_join(BASE_DIR, "static/index.html")


async def _serve_legacy_index(request: Request):
    if os.path.exists(_LEGACY_INDEX):
        return serve_html_with_nonce(request, _LEGACY_INDEX)
    return serve_html_with_nonce(request, abs_join(BASE_DIR, "index.html"))


def register_inline_routes(app, auth_manager):
    AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() != "false"

    # Generated images
    @app.get("/api/generated-image/{filename}")
    async def serve_generated_image(filename: str, request: Request):
        from src.generated_images import GENERATED_IMAGE_HEADERS, resolve_generated_image_path

        img_path = resolve_generated_image_path(filename)
        try:
            from src.auth_helpers import get_current_user
            from core.database import SessionLocal as _SL, GalleryImage as _GI

            _user = get_current_user(request)
            if _user:
                _db = _SL()
                try:
                    _row = _db.query(_GI).filter(_GI.filename == filename).first()
                    if _row is not None and _row.owner and _row.owner != _user:
                        raise HTTPException(status_code=404, detail="Image not found")
                finally:
                    _db.close()
        except HTTPException:
            raise
        except Exception as _e:
            logger.warning("Image ownership verification failed for %r", filename, exc_info=_e)
        ext = filename.rsplit(".", 1)[-1].lower()
        mime = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
            "gif": "image/gif",
            "mp4": "video/mp4",
            "mov": "video/quicktime",
            "webm": "video/webm",
            "mkv": "video/x-matroska",
            "m4v": "video/mp4",
        }.get(ext, "application/octet-stream")
        return FileResponse(str(img_path), media_type=mime, headers=GENERATED_IMAGE_HEADERS)

    # Legacy /legacy routes
    @app.get("/legacy")
    @app.get("/legacy/")
    @app.get("/legacy/{full_path:path}")
    async def serve_legacy(request: Request, full_path: str = ""):
        return await _serve_legacy_index(request)

    # Backgrounds sandbox
    @app.get("/backgrounds")
    async def serve_backgrounds(request: Request):
        return serve_html_with_nonce(request, abs_join(BASE_DIR, "static/backgrounds.html"))

    # Login page
    @app.get("/login")
    async def serve_login(request: Request):
        if not AUTH_ENABLED:
            return _SRR(url="/", status_code=302)
        return serve_html_with_nonce(request, abs_join(BASE_DIR, "static/login.html"))

    # Core API endpoints (before SPA catchall)
    @app.get("/api/version")
    async def get_version():
        from core.constants import APP_VERSION

        return {"version": APP_VERSION}

    @app.get("/api/health")
    async def health_check():
        from datetime import datetime, timezone

        return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

    @app.post("/api/client-perf")
    async def client_perf(request: Request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        try:
            kind = str(data.get("type") or "client").replace("\n", " ")[:80]
            total_ms = float(data.get("total_ms") or 0)
            stages = data.get("stages") if isinstance(data.get("stages"), list) else []
            stage_txt = " ".join(
                f"{str(s.get('name') or '')[:40]}={float(s.get('delta_ms') or 0):.0f}ms"
                for s in stages[:20]
                if isinstance(s, dict)
            )
            extra = str(data.get("extra") or "").replace("\n", " ")[:200]
            logging.getLogger("app.client_perf").warning(
                "client_perf type=%s total=%.0fms %s%s",
                kind,
                total_ms,
                stage_txt,
                f" extra={extra}" if extra else "",
            )
        except Exception:
            logging.getLogger("app.client_perf").debug("client_perf log failed", exc_info=True)
        return {"ok": True}

    @app.get("/api/ready")
    async def readiness_check():
        from src.readiness import check_readiness

        result = check_readiness()
        return JSONResponse(status_code=200 if result.get("ready") else 503, content=result)

    @app.get("/api/runtime")
    async def runtime_info():
        in_docker = os.path.exists("/.dockerenv")
        if not in_docker:
            try:
                with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as fh:
                    cg = fh.read()
                in_docker = any(marker in cg for marker in ("docker", "containerd", "kubepods"))
            except Exception:
                in_docker = False
        ollama_url = (
            os.getenv("OLLAMA_BASE_URL")
            or os.getenv("OLLAMA_URL")
            or ("http://host.docker.internal:11434/v1" if in_docker else "http://127.0.0.1:11434/v1")
        )
        return {"in_docker": in_docker, "ollama_base_url": ollama_url}

    # Heartbeat
    @app.post("/api/activity/heartbeat")
    async def activity_heartbeat():
        from src.interactive_gate import mark_browser_activity

        await mark_browser_activity()

        async def _stop_bg():
            try:
                ts = getattr(request.app.state, "_task_scheduler", None)
                if ts:
                    await ts.stop_background_tasks_for_foreground(reason="browser heartbeat")
            except Exception:
                logging.getLogger("app.foreground_gate").debug("heartbeat task stop failed", exc_info=True)

        asyncio.create_task(_stop_bg())
        return {"ok": True}

    # SPA catchall (must be last)
    @app.get("/")
    async def serve_root(request: Request):
        return await _serve_legacy_index(request)

    @app.get("/{full_path:path}")
    async def serve_catchall(request: Request, full_path: str = ""):
        first = (full_path or "").split("/", 1)[0]
        if first in ("api", "static", "legacy", "login", "backgrounds", "docs", "openapi.json", "redoc"):
            raise HTTPException(status_code=404, detail="Not found")
        return await _serve_legacy_index(request)


# ── Router registration ────────────────────────────────────────


def register_route_routers(app, auth_manager, session_manager, chat_handler, chat_processor,
                           memory_manager, memory_vector, research_handler, upload_handler,
                           webhook_manager, api_key_manager, preset_manager, skills_manager,
                           model_discovery, personal_docs_mgr, rag_manager, rag_available,
                           tts_service, stt_service, task_scheduler, mcp_manager,
                           email_router=None, calendar_router=None, document_router=None,
                           memory_router=None):
    """Register all route modules. This keeps app.py under 400 lines."""
    from datetime import datetime, timezone
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

    logger.info("Registering route routers...")

    # Auth
    from routes.auth_routes import setup_auth_routes
    app.include_router(setup_auth_routes(auth_manager))

    # Uploads
    from routes.upload_routes import setup_upload_routes
    upload_router, upload_cleanup_func = setup_upload_routes(upload_handler)
    app.include_router(upload_router)

    # Emoji proxy
    from routes.emoji_routes import setup_emoji_routes
    app.include_router(setup_emoji_routes())

    # Sessions
    from routes.session_routes import setup_session_routes
    session_config = {"REQUEST_TIMEOUT": REQUEST_TIMEOUT, "OPENAI_API_KEY": OPENAI_API_KEY, "SESSIONS_FILE": SESSIONS_FILE}
    app.include_router(setup_session_routes(
        session_manager, session_config, webhook_manager=webhook_manager, upload_handler=upload_handler,
    ))

    # Admin wipe
    from routes.admin_wipe_routes import setup_admin_wipe_routes
    app.include_router(setup_admin_wipe_routes(session_manager))

    # Memory
    from routes.memory.memory_routes import setup_memory_routes
    memory_router = setup_memory_routes(memory_manager, session_manager, memory_vector=memory_vector)
    app.include_router(memory_router)

    # Skills
    from routes.skills_routes import setup_skills_routes
    app.include_router(setup_skills_routes(skills_manager))

    # Chat
    from routes.chat_routes import setup_chat_routes
    app.include_router(setup_chat_routes(
        session_manager, chat_handler, chat_processor,
        memory_manager, research_handler, upload_handler,
        memory_vector=memory_vector, webhook_manager=webhook_manager,
        skills_manager=skills_manager,
    ))

    # Research
    from routes.research.research_routes import setup_research_routes
    app.include_router(setup_research_routes(research_handler, session_manager=session_manager))

    # History
    from routes.history.history_routes import setup_history_routes
    app.include_router(setup_history_routes(session_manager, upload_handler=upload_handler))

    # Search
    from routes.search_routes import setup_search_routes
    from src.config import config
    app.include_router(setup_search_routes(config))

    # Presets
    from routes.preset_routes import setup_preset_routes
    app.include_router(setup_preset_routes(preset_manager))

    # Diagnostics
    from routes.diagnostics_routes import setup_diagnostics_routes
    app.include_router(setup_diagnostics_routes(rag_manager, rag_available, research_handler, memory_vector))

    # Bridge / Projects / Home / Council / Neuron
    from routes.bridge_routes import router as bridge_router
    app.include_router(bridge_router)
    from routes.odys_projects_routes import router as odys_projects_router
    app.include_router(odys_projects_router)
    from routes.odys_home_routes import router as odys_home_router
    app.include_router(odys_home_router)
    from routes.odys_council_routes import router as odys_council_router
    app.include_router(odys_council_router)
    from routes.odys_neuron_routes import router as odys_neuron_router
    app.include_router(odys_neuron_router)

    # Cleanup
    from routes.cleanup_routes import setup_cleanup_routes
    app.include_router(setup_cleanup_routes(session_manager))

    # Personal docs
    from routes.personal_routes import setup_personal_routes
    app.include_router(setup_personal_routes(personal_docs_mgr, rag_manager, rag_available))

    # Embeddings
    from routes.embedding_routes import setup_embedding_routes
    app.include_router(setup_embedding_routes())

    # Models
    from routes.model_routes import setup_model_routes
    app.include_router(setup_model_routes(model_discovery))

    # Copilot / ChatGPT subscription
    from routes.copilot_routes import setup_copilot_routes
    app.include_router(setup_copilot_routes())
    from routes.chatgpt_subscription_routes import setup_chatgpt_subscription_routes
    app.include_router(setup_chatgpt_subscription_routes())

    # TTS / STT
    from routes.tts_routes import setup_tts_routes
    app.include_router(setup_tts_routes(tts_service))
    from routes.stt_routes import setup_stt_routes
    app.include_router(setup_stt_routes(stt_service))

    # Documents
    from routes.document_routes import setup_document_routes
    document_router = setup_document_routes(session_manager, upload_handler)
    app.include_router(document_router)

    # Signatures
    from routes.signature_routes import setup_signature_routes
    app.include_router(setup_signature_routes())

    # Gallery
    from routes.gallery.gallery_routes import setup_gallery_routes
    app.include_router(setup_gallery_routes())

    # Editor drafts
    from routes.editor_draft_routes import setup_editor_draft_routes
    app.include_router(setup_editor_draft_routes())

    # Scheduled tasks
    from src.task_scheduler import TaskScheduler
    from src.event_bus import set_task_scheduler
    set_task_scheduler(task_scheduler)
    from routes.task_routes import setup_task_routes
    app.include_router(setup_task_routes(task_scheduler))

    # Assistant
    from routes.assistant_routes import setup_assistant_routes
    app.include_router(setup_assistant_routes(task_scheduler))

    # Calendar
    from routes.calendar_routes import setup_calendar_routes
    calendar_router = setup_calendar_routes(upload_handler=upload_handler)
    app.include_router(calendar_router)

    # Shell
    from routes.shell_routes import setup_shell_routes
    app.include_router(setup_shell_routes())

    # Cookbook / HW Fit
    from routes.cookbook_routes import setup_cookbook_routes
    app.include_router(setup_cookbook_routes())
    from routes.hwfit_routes import setup_hwfit_routes
    app.include_router(setup_hwfit_routes())

    # Compare
    from routes.compare_routes import setup_compare_routes
    app.include_router(setup_compare_routes(session_manager))

    # Prefs
    from routes.prefs_routes import setup_prefs_routes
    app.include_router(setup_prefs_routes())

    # Backup
    from routes.backup_routes import setup_backup_routes
    app.include_router(setup_backup_routes(memory_manager, preset_manager, skills_manager))

    # Fonts
    from routes.font_routes import setup_font_routes
    app.include_router(setup_font_routes())

    # MCP
    from src.agent_tools import set_mcp_manager
    from routes.mcp_routes import setup_mcp_routes

    set_mcp_manager(mcp_manager)
    app.include_router(setup_mcp_routes(mcp_manager))

    # AI interaction tools
    from src.ai_interaction import (
        set_session_manager as set_ai_session_manager,
        set_memory_manager as set_ai_memory_manager,
        set_rag_manager as set_ai_rag_manager,
    )
    set_ai_session_manager(session_manager)
    set_ai_memory_manager(memory_manager, memory_vector)
    set_ai_rag_manager(rag_manager, personal_docs_mgr)

    # Webhooks
    from routes.webhook_routes import setup_webhook_routes
    app.include_router(setup_webhook_routes(webhook_manager, auth_manager, session_manager, api_key_manager))

    # API tokens
    from routes.api_token_routes import setup_api_token_routes
    app.include_router(setup_api_token_routes())

    # Notes
    from routes.note_routes import setup_note_routes
    app.include_router(setup_note_routes(task_scheduler, upload_handler=upload_handler))

    # Email
    from routes.email_routes import setup_email_routes
    email_router = setup_email_routes()
    app.include_router(email_router)

    # Codex / Claude
    from routes.codex_routes import setup_codex_routes, setup_claude_routes
    app.include_router(setup_codex_routes(
        email_router=email_router, memory_router=memory_router,
        calendar_router=calendar_router, document_router=document_router,
    ))
    app.include_router(setup_claude_routes())

    # Vault
    from routes.vault_routes import setup_vault_routes
    app.include_router(setup_vault_routes())

    # Contacts
    from routes.contacts.contacts_routes import setup_contacts_routes
    app.include_router(setup_contacts_routes())

    # Companion
    from companion import setup_companion_routes
    app.include_router(setup_companion_routes())

    # Workspace
    from routes.workspace_routes import setup_workspace_routes
    app.include_router(setup_workspace_routes())

    logger.info("All route routers registered")
    return upload_cleanup_func
