"""
src/app_lifespan.py — application lifespan (startup / shutdown) extracted from app.py.

Usage:

    from src.app_lifespan import setup_lifespan
    setup_lifespan(app, upload_cleanup_func=..., webhook_manager=..., ...)
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def setup_lifespan(app, **components):
    """Populate app.router.lifespan_context with a closure over *components.

    Components dict should include:
      upload_cleanup_func, webhook_manager, mcp_manager, task_scheduler,
      skills_manager, model_discovery, upload_handler, auth_manager
    """

    @asynccontextmanager
    async def _lifespan(app):
        # ── Auto-admin (if no users configured) ──
        if "auth_manager" in components:
            try:
                am = components["auth_manager"]
                if not am.is_configured:
                    logger.info("Auto-configuring default admin account (admin/admin)")
                    am.create_user("admin", "admin", is_admin=True)
            except Exception as e:
                logger.warning(f"Auto-admin failed: {e}")
        # ── STARTUP ──
        await _startup_event(app, **components)
        yield
        # ── SHUTDOWN ──
        await _shutdown_event(**components)

    app.router.lifespan_context = _lifespan


# ═══════════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════════

async def _startup_event(app, **c):
    logger.info("Application starting up...")

    upload_cleanup_func = c.get("upload_cleanup_func")
    webhook_manager = c.get("webhook_manager")
    mcp_manager = c.get("mcp_manager")
    task_scheduler = c.get("task_scheduler")
    skills_manager = c.get("skills_manager")
    model_discovery = c.get("model_discovery")
    upload_handler = c.get("upload_handler")

    if webhook_manager:
        webhook_manager.set_loop(asyncio.get_running_loop())

    # Wipe leftover incognito sessions
    try:
        from core.database import SessionLocal as _SL, Session as _DbSess, ChatMessage as _DbMsg

        _db = _SL()
        try:
            _ghosts = _db.query(_DbSess).filter(_DbSess.name.in_(("Nobody", "Incognito"))).all()
            for _g in _ghosts:
                _db.query(_DbMsg).filter(_DbMsg.session_id == _g.id).delete()
                _db.delete(_g)
            if _ghosts:
                _db.commit()
                logger.info(f"Purged {len(_ghosts)} leftover incognito session(s)")
        finally:
            _db.close()
    except Exception as e:
        logger.debug(f"Incognito purge skipped: {e}")

    # Strong refs for fire-and-forget background tasks
    _startup_tasks: list[asyncio.Task] = []
    app.state._startup_tasks = _startup_tasks

    if upload_cleanup_func:
        _ct = asyncio.create_task(upload_cleanup_func())
        _startup_tasks.append(_ct)

    # Background-job monitor
    try:
        from src.bg_monitor import start_bg_monitor

        _startup_tasks.append(start_bg_monitor())
    except Exception as _e:
        logger.warning("Failed to start background-job monitor: %s", _e)

    # MCP server connections (after server is accepting traffic)
    async def _startup_mcp_connections():
        try:
            from src.builtin_mcp import register_builtin_servers

            await register_builtin_servers(mcp_manager)
        except BaseException as e:
            logger.warning(f"Built-in MCP registration failed (non-critical): {type(e).__name__}: {e}")
        try:
            await mcp_manager.connect_all_enabled()
        except asyncio.TimeoutError:
            logger.warning("User MCP startup timed out (non-critical)")
        except BaseException as e:
            logger.warning(f"MCP startup failed (non-critical): {type(e).__name__}: {e}")

    _startup_tasks.append(asyncio.create_task(_startup_mcp_connections()))

    # Startup warmups (opt-in)
    _warmups = str(os.getenv("ODYSSEUS_STARTUP_WARMUPS", "")).lower() in {"1", "true", "yes", "on"}
    if _warmups:
        async def _warmup_tool_index():
            try:
                from src.tool_index import get_tool_index

                idx = await asyncio.to_thread(get_tool_index)
                if idx:
                    await asyncio.to_thread(idx.get_tools_for_query, "warmup", 8)
                    logger.info("[startup] Tool index pre-warmed")
            except Exception as e:
                logger.warning(f"Tool index warmup failed (non-critical): {type(e).__name__}: {e}")

        _startup_tasks.append(asyncio.create_task(_warmup_tool_index()))

        async def _warmup_endpoints():
            try:
                import httpx

                urls = (
                    await asyncio.to_thread(model_discovery.warmup_ping_urls)
                    if model_discovery
                    else []
                )
                for url in urls:
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            await client.get(url)
                        logger.info(f"Warmup ping OK: {url}")
                    except Exception as e:
                        logger.debug(f"Warmup ping failed for endpoint: {e}")
            except Exception as e:
                logger.debug(f"Warmup ping skipped: {e}")

        _startup_tasks.append(asyncio.create_task(_warmup_endpoints()))
    else:
        logger.info("Startup warmups disabled (set ODYSSEUS_STARTUP_WARMUPS=1 to enable)")

    # Keep-alive loop (opt-in)
    _keepalive = str(os.getenv("ODYSSEUS_MODEL_KEEPALIVE", "")).lower() in {"1", "true", "yes", "on"}
    if _keepalive:
        async def _keepalive_loop():
            while True:
                try:
                    await asyncio.sleep(60)
                    await _warmup_endpoints()
                except Exception as e:
                    logger.warning(f"Keepalive loop error: {e}")
                    await asyncio.sleep(300)

        _startup_tasks.append(asyncio.create_task(_keepalive_loop()))

    # Ensure default tasks for all known users
    async def _ensure_default_tasks():
        owners = set()
        try:
            from core.constants import AUTH_FILE

            auth_path = AUTH_FILE
            with open(auth_path, encoding="utf-8") as f:
                users = json.load(f).get("users", {})
            owners.update(users.keys())
        except Exception as e:
            logger.debug(f"Default task auth-owner scan: {e}")
        try:
            from core.database import SessionLocal, ScheduledTask
            from src.task_scheduler import HOUSEKEEPING_DEFAULTS

            builtin_names = []
            for defs in HOUSEKEEPING_DEFAULTS.values():
                builtin_names.append(defs["name"])
                builtin_names.extend(defs.get("legacy_names") or [])
            db_seed = SessionLocal()
            try:
                rows = (
                    db_seed.query(ScheduledTask.owner)
                    .filter(
                        (ScheduledTask.action.in_(list(HOUSEKEEPING_DEFAULTS.keys())))
                        | (ScheduledTask.name.in_(builtin_names))
                    )
                    .distinct()
                    .all()
                )
                owners.update(row[0] for row in rows if row[0])
            finally:
                db_seed.close()
        except Exception as e:
            logger.debug(f"Default task existing-owner scan: {e}")
        try:
            for uname in sorted(owners):
                try:
                    await task_scheduler.ensure_defaults(uname)
                except Exception as e:
                    logger.debug(f"ensure_defaults({uname}): {e}")
        except Exception as e:
            logger.debug(f"Default tasks: {e}")

    await _ensure_default_tasks()

    # Neuron natural boot: vault sync + project seed + memory import + decay
    # Runs silently in background; no-op if vault missing or neuron unavailable.
    async def _neuron_boot():
        try:
            from services.odys_neuron_hooks import natural_boot
            result = await asyncio.to_thread(natural_boot)
            actions = result.get("actions", [])
            if actions:
                logger.info(f"Neuron natural boot: {', '.join(actions)}")
            else:
                logger.debug("Neuron natural boot: nothing to do")
        except Exception as e:
            logger.debug(f"Neuron natural boot skipped: {e}")

    _startup_tasks.append(asyncio.create_task(_neuron_boot()))

    # Skill owner backfill
    try:
        from core.constants import AUTH_FILE

        auth_path = AUTH_FILE
        with open(auth_path, encoding="utf-8") as f:
            users = json.load(f).get("users", {})
        primary_owner = None
        for uname, udata in users.items():
            if udata.get("is_admin") is True:
                primary_owner = uname
                break
        if not primary_owner and users:
            primary_owner = next(iter(users))
        if primary_owner:
            changed = skills_manager.backfill_owner(primary_owner, set(users.keys()))
            if changed:
                logger.info("Assigned %s legacy skill file(s) to %s", changed, primary_owner)
    except Exception as e:
        logger.debug(f"Skill owner backfill skipped: {e}")

    # Start scheduled task runner
    _tasks_inprocess = os.environ.get("ODYSSEUS_INPROCESS_TASKS", "1").strip().lower()
    if _tasks_inprocess not in ("0", "false", "no", "off", ""):
        await task_scheduler.start()
    else:
        logger.info(
            "In-process task scheduler disabled (ODYSSEUS_INPROCESS_TASKS=0); "
            "drive task firing externally (e.g. cron)."
        )

    # Periodic null-owner sweep
    async def _null_owner_sweep_loop():
        while True:
            try:
                await asyncio.sleep(3600)
                from core.database import _migrate_assign_legacy_owner

                await asyncio.to_thread(_migrate_assign_legacy_owner)
            except Exception as e:
                logger.debug(f"Null-owner sweep skipped: {e}")
                await asyncio.sleep(3600)

    _startup_tasks.append(asyncio.create_task(_null_owner_sweep_loop()))

    # Nightly skill audit
    async def _skill_audit_nightly_loop():
        while True:
            try:
                from src.settings import get_setting

                hour = int(get_setting("skill_audit_hour", 2) or 2)
            except Exception:
                hour = 2
            now = datetime.now()
            nxt = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep(max(60, (nxt - now).total_seconds()))
            try:
                from src.settings import get_setting

                if not get_setting("skill_audit_nightly", True):
                    continue
                batch = int(get_setting("skill_audit_batch", 8) or 8)
                from routes.skills_routes import run_scheduled_skill_audit

                await run_scheduled_skill_audit(skills_manager, owner=None, max_skills=batch)
            except Exception as e:
                logger.warning(f"Nightly skill audit failed: {e}")

    _startup_tasks.append(asyncio.create_task(_skill_audit_nightly_loop()))

    # Cookbook serve lifecycle
    from src.cookbook_serve_lifecycle import cookbook_serve_lifecycle_loop

    _startup_tasks.append(asyncio.create_task(cookbook_serve_lifecycle_loop()))

    logger.info("Application startup complete")


# ═══════════════════════════════════════════════════════════════
# Shutdown
# ═══════════════════════════════════════════════════════════════

async def _shutdown_event(**c):
    logger.info("Application shutting down...")

    upload_cleanup_task = c.get("_upload_cleanup_task")
    task_scheduler = c.get("task_scheduler")
    webhook_manager = c.get("webhook_manager")
    mcp_manager = c.get("mcp_manager")

    if upload_cleanup_task:
        upload_cleanup_task.cancel()
        try:
            await upload_cleanup_task
        except asyncio.CancelledError:
            pass
    try:
        if task_scheduler:
            await task_scheduler.stop()
    except Exception:
        pass
    try:
        if webhook_manager:
            await webhook_manager.close()
    except Exception as e:
        logger.warning(f"Webhook manager shutdown error: {e}")
    try:
        if mcp_manager:
            await mcp_manager.disconnect_all()
    except Exception as e:
        logger.warning(f"MCP shutdown error: {e}")
    logger.info("Application shutdown complete")
