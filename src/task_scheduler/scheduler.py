"""Core TaskScheduler class — lifecycle, loop, and task dispatch."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict

from src.task_action_policy import (
    is_admin_only_task_action,
    owner_has_admin_task_privileges,
)

from .helpers import (
    _utcnow,
    _resolve_task_timezone,
    compute_next_run,
)
from .execution import TaskSchedulerExecutionMixin

logger = logging.getLogger(__name__)


class TaskScheduler(TaskSchedulerExecutionMixin):
    """Background scheduler for ScheduledTask execution.

    Manages the scheduler loop, due-task dispatch, execution lifecycle
    (queued → running → completed), foreground gating, notifications,
    and built-in background scanners (notes, events).
    """

    def __init__(self, session_manager):
        self._session_manager = session_manager
        self._running = False
        self._task = None
        self._executing = set()  # task IDs currently running OR queued behind the semaphore
        # Guards mutations of _executing. _check_due_tasks runs in the loop
        # coroutine; trigger_task() can be called from request handlers; the
        # event bus fires from background tasks. Without this lock long-running
        # tasks could be double-dispatched.
        self._executing_lock = asyncio.Lock()
        self._pending_notifications = []  # completed task notifications
        self._task_defer_counts = {}
        # Strict serial execution — exactly one task runs at a time. Anything
        # else (manual trigger, scheduled dispatch, task chain) waits behind
        # the semaphore as "queued" and starts when the current run finishes.
        # This is a hard guarantee, not configurable.
        self._run_semaphore = asyncio.Semaphore(1)
        self._concurrency_cap = 1
        self._task_handles = {}

    def _set_run_progress(self, run_id: str, message: str):
        """Persist short live progress text for Activity while a run is active."""
        if not run_id:
            return
        try:
            from core.database import SessionLocal, TaskRun
            db = SessionLocal()
            try:
                run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run and run.status in ("queued", "running"):
                    run.result = (message or "")[:4000]
                    db.commit()
            finally:
                db.close()
        except Exception:
            logger.debug("Task progress update failed", exc_info=True)

    def _mark_run_aborted(self, task_id: str, run_id: str | None = None, message: str = "Stopped by user") -> bool:
        """Mark an active run as aborted. Used by stop/cancel paths."""
        try:
            from core.database import SessionLocal, TaskRun
            db = SessionLocal()
            try:
                q = db.query(TaskRun)
                if run_id:
                    q = q.filter(TaskRun.id == run_id)
                else:
                    q = q.filter(
                        TaskRun.task_id == task_id,
                        TaskRun.status.in_(("queued", "running")),
                    ).order_by(TaskRun.started_at.desc())
                run = q.first()
                if not run or run.status not in ("queued", "running"):
                    return False
                run.status = "aborted"
                run.error = message
                run.result = run.result or message
                run.finished_at = _utcnow()
                db.commit()
                return True
            finally:
                db.close()
        except Exception:
            logger.debug("Task abort marker failed for %s", task_id, exc_info=True)
            return False

    def add_notification(self, task_name: str, status: str, task_id: str = None, owner: str = None, body: str = None):
        """Store a notification about a completed task run. Tagged with the
        task's owner so `pop_notifications` can return only that user's
        notifications and prevent cross-tenant drain. `body` is the result
        text — populated when output_target='notification' so the client can
        show a rich browser Notification, not just a toast."""
        self._pending_notifications.append({
            "task_name": task_name,
            "status": status,
            "task_id": task_id,
            "owner": owner,
            "body": (body[:500] + "\u2026") if body and len(body) > 500 else body,
            "timestamp": _utcnow().isoformat() + "Z",
        })
        # Cap at 50 to avoid unbounded growth
        if len(self._pending_notifications) > 50:
            self._pending_notifications = self._pending_notifications[-50:]

    def pop_notifications(self, owner: str = None) -> list:
        """Return and clear pending notifications.

        When `owner` is set, only matching notifications are returned (and
        cleared). Notifications stored before owner-tagging existed (or
        from owner-less tasks) are included when the caller is anonymous
        or when no owner filter is given — preserves backward behaviour
        for the legacy single-user deploy.
        """
        if owner is None:
            notes = self._pending_notifications[:]
            self._pending_notifications.clear()
            return notes
        keep, take = [], []
        for n in self._pending_notifications:
            if n.get("owner") == owner:
                take.append(n)
            else:
                keep.append(n)
        self._pending_notifications = keep
        return take

    async def start(self):
        """Start the scheduler loop and background scanners."""
        # On startup, mark any leftover "running" task_runs as errored.
        try:
            from core.database import SessionLocal, TaskRun
            db = SessionLocal()
            try:
                stale = db.query(TaskRun).filter(
                    TaskRun.status.in_(("running", "queued"))
                ).all()
                if stale:
                    now = _utcnow()
                    for r in stale:
                        old_status = r.status or "running"
                        r.status = "aborted"
                        r.error = "Server restarted while task was " + old_status
                        r.finished_at = now
                    db.commit()
                    logger.info(f"Cleared {len(stale)} stale task_runs from previous run")
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Could not clear stale task_runs on startup: {e}")

        # Advance next_run for active tasks whose next_run is already in the past.
        try:
            from core.database import SessionLocal as _SL, ScheduledTask as _ST
            db = _SL()
            try:
                now = _utcnow()
                overdue = db.query(_ST).filter(
                    _ST.status == "active",
                    _ST.next_run.isnot(None),
                    _ST.next_run < now,
                ).all()
                if overdue:
                    for t in overdue:
                        t.next_run = now + timedelta(seconds=60)
                    db.commit()
                    logger.info(
                        "Pushed next_run forward by 60s for %d overdue active tasks on startup",
                        len(overdue),
                    )
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Could not advance overdue next_run on startup: {e}")

        # Defense-in-depth dedupe sweep
        try:
            from core.database import SessionLocal, CrewMember, ScheduledTask
            db = SessionLocal()
            try:
                from sqlalchemy import func
                groups = db.query(CrewMember.owner, func.count(CrewMember.id).label("n")).filter(
                    CrewMember.is_default_assistant == True,  # noqa: E712
                ).group_by(CrewMember.owner).having(func.count(CrewMember.id) > 1).all()
                for owner, n in groups:
                    rows = db.query(CrewMember).filter(
                        CrewMember.owner == owner,
                        CrewMember.is_default_assistant == True,  # noqa: E712
                    ).order_by(CrewMember.created_at.asc()).all()
                    keep = rows[0]
                    losers = rows[1:]
                    loser_ids = [r.id for r in losers]
                    n_tasks = db.query(ScheduledTask).filter(
                        ScheduledTask.crew_member_id.in_(loser_ids)
                    ).delete(synchronize_session=False)
                    for r in losers:
                        db.delete(r)
                    db.commit()
                    logger.warning(
                        "Default-assistant dedupe: owner=%r had %d rows, kept %s, "
                        "dropped %d crew + %d orphan tasks",
                        owner, n, keep.id, len(losers), n_tasks,
                    )
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Could not dedupe default-assistant rows on startup: {e}")

        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._note_pings_task = asyncio.create_task(self._note_pings_loop())
        logger.info(f"Task scheduler started (concurrency cap: {self._concurrency_cap})")
        # Audit clusters
        try:
            from core.database import SessionLocal, ScheduledTask
            db = SessionLocal()
            try:
                rows = db.query(ScheduledTask).filter(
                    ScheduledTask.status == "active",
                    ScheduledTask.trigger_type == "schedule",
                    ScheduledTask.next_run.isnot(None),
                ).all()
                buckets: Dict[str, list] = {}
                for r in rows:
                    if not r.next_run:
                        continue
                    key = r.next_run.strftime("%H:%M")
                    buckets.setdefault(key, []).append(r.name or r.id)
                clusters = {k: v for k, v in buckets.items() if len(v) > 1}
                if clusters:
                    summary = ", ".join(f"{k} ({len(v)})" for k, v in sorted(clusters.items()))
                    logger.info(f"Task scheduling clusters (>1 task/minute): {summary}")
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"Cluster audit skipped: {e}")

    async def stop(self):
        """Stop the scheduler loop and background scanners."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for attr in ("_note_pings_task", "_event_pings_task"):
            t = getattr(self, attr, None)
            if t:
                t.cancel()
                try: await t
                except asyncio.CancelledError: pass
        logger.info("Task scheduler stopped")

    async def _note_pings_loop(self):
        """Built-in note-due scanner — ticks every 60s inside the scheduler."""
        await asyncio.sleep(30)
        from src.builtin_actions import action_ping_notes, TaskNoop
        while self._running:
            owners = self._known_task_owners()
            for ow in (owners or [""]):
                try:
                    await action_ping_notes(owner=ow)
                except TaskNoop:
                    pass
                except Exception as e:
                    logger.warning(f"ping_notes background scanner errored for owner={ow!r}: {e}")
            await asyncio.sleep(60)

    async def _event_pings_loop(self):
        """Built-in calendar-event scanner — runs every 10 min."""
        await asyncio.sleep(90)
        from src.builtin_actions import action_ping_events, TaskNoop
        while self._running:
            owners = self._known_task_owners()
            for ow in (owners or [""]):
                try:
                    await action_ping_events(owner=ow)
                except TaskNoop:
                    pass
                except Exception as e:
                    logger.warning(f"ping_events background scanner errored for owner={ow!r}: {e}")
            await asyncio.sleep(600)

    def _known_task_owners(self) -> list:
        """Distinct non-empty owners that background scanners should visit."""
        from core.database import SessionLocal, ScheduledTask, Note
        db = SessionLocal()
        try:
            owners = set()
            for r in db.query(ScheduledTask.owner).distinct().all():
                if r[0]:
                    owners.add(r[0])
            note_q = db.query(Note.owner).filter(
                Note.due_date.isnot(None),
                Note.due_date != "",
                Note.archived == False,  # noqa: E712
            ).distinct()
            for r in note_q.all():
                if r[0]:
                    owners.add(r[0])
            return sorted(owners)
        except Exception:
            return []
        finally:
            db.close()

    async def _loop(self):
        await asyncio.sleep(10)
        while self._running:
            try:
                await self._check_due_tasks()
            except Exception:
                logger.exception("Error in task scheduler loop")
            # Sleep until the next scheduled run, capped at 60s.
            sleep_for = 60.0
            try:
                from core.database import SessionLocal as _SL, ScheduledTask as _ST
                _db = _SL()
                try:
                    next_run = _db.query(_ST.next_run).filter(
                        _ST.status == "active",
                        _ST.next_run.isnot(None),
                    ).order_by(_ST.next_run.asc()).first()
                    if next_run and next_run[0]:
                        delta = (next_run[0] - _utcnow()).total_seconds()
                        sleep_for = max(1.0, min(60.0, delta))
                finally:
                    _db.close()
            except Exception:
                pass
            await asyncio.sleep(sleep_for)

    async def _check_due_tasks(self):
        from core.database import SessionLocal, ScheduledTask
        db = SessionLocal()
        try:
            now = _utcnow()
            foreground_active = False
            try:
                from src.interactive_gate import has_foreground_activity
                foreground_active = has_foreground_activity()
            except Exception:
                foreground_active = False
            async with self._executing_lock:
                executing_snapshot = set(self._executing)
                due = db.query(ScheduledTask).filter(
                    ScheduledTask.status == "active",
                    ScheduledTask.next_run <= now,
                    ScheduledTask.id.notin_(executing_snapshot) if executing_snapshot else True,
                ).all()
                to_dispatch = []
                for task in due:
                    if task.id in self._executing:
                        continue
                    if foreground_active:
                        task.next_run = now + timedelta(minutes=15)
                        continue
                    self._executing.add(task.id)
                    to_dispatch.append(task.id)
                if foreground_active and due:
                    db.commit()
            for task_id in to_dispatch:
                asyncio.create_task(self._execute_task(task_id))
        finally:
            db.close()

    async def _execute_task(self, task_id: str, *, bypass_model_slot: bool = False, release_executing: bool = True):
        # Create the run record with status="queued" BEFORE waiting on the
        # semaphore so the UI can show that a manually-triggered task is in
        # line behind another.
        from core.database import SessionLocal, TaskRun
        current = asyncio.current_task()
        if current:
            self._task_handles[task_id] = current
        run_id = str(uuid.uuid4())
        _q_db = SessionLocal()
        try:
            run = TaskRun(
                id=run_id,
                task_id=task_id,
                started_at=_utcnow(),
                status="queued",
                result="Queued \u2014 waiting for a free slot\u2026",
            )
            _q_db.add(run)
            _q_db.commit()
        except Exception:
            logger.exception(f"Failed to create queued run row for task {task_id}")
        finally:
            _q_db.close()

        try:
            if bypass_model_slot or not self._task_needs_model_slot(task_id):
                await self._execute_task_locked(
                    task_id,
                    run_id,
                    release_executing=release_executing,
                    gate_foreground=not bypass_model_slot,
                )
                return

            async with self._run_semaphore:
                await self._execute_task_locked(
                    task_id,
                    run_id,
                    release_executing=release_executing,
                    gate_foreground=True,
                )
        except asyncio.CancelledError:
            self._mark_run_aborted(task_id, run_id)
            self._defer_immediately_due_task(task_id, delay=timedelta(minutes=15))
            raise
        finally:
            handle = self._task_handles.get(task_id)
            if handle is current:
                self._task_handles.pop(task_id, None)
            if release_executing:
                async with self._executing_lock:
                    self._executing.discard(task_id)

    def _defer_immediately_due_task(self, task_id: str, *, delay: timedelta):
        """A queued task can be cancelled before _execute_task_locked gets a DB
        handle. If its next_run stays in the past, the scheduler dispatches it
        again on the next tick and spams aborted Activity rows."""
        try:
            from core.database import SessionLocal, ScheduledTask
            db = SessionLocal()
            try:
                task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                if (
                    task
                    and task.status == "active"
                    and task.next_run is not None
                    and task.next_run <= _utcnow()
                ):
                    task.next_run = _utcnow() + delay
                    db.commit()
            finally:
                db.close()
        except Exception:
            logger.debug("Failed to defer cancelled queued task %s", task_id, exc_info=True)

    async def _execute_task_locked(
        self,
        task_id: str,
        run_id: str,
        *,
        release_executing: bool = True,
        gate_foreground: bool = True,
    ):
        from core.database import SessionLocal, ScheduledTask, TaskRun

        db = SessionLocal()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
            if not task or task.status != "active":
                stale = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if stale and stale.status == "queued":
                    stale.status = "skipped"
                    stale.finished_at = _utcnow()
                    stale.error = f"Task no longer active (status={task.status if task else 'deleted'})"
                    db.commit()
                return

            if (
                is_admin_only_task_action(task.task_type, task.action)
                and not owner_has_admin_task_privileges(task.owner)
            ):
                msg = f"Action '{task.action}' requires admin privileges"
                blocked = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if blocked:
                    blocked.status = "error"
                    blocked.result = msg
                    blocked.error = msg
                    blocked.finished_at = _utcnow()
                task.status = "paused"
                task.next_run = None
                task.last_run = _utcnow()
                logger.warning(
                    "Paused admin-only task %s for non-admin owner %r",
                    task_id,
                    task.owner,
                )
                db.commit()
                return

            if gate_foreground:
                waiting = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if waiting and waiting.status == "queued":
                    waiting.result = "Queued \u2014 waiting for Odysseus to be idle\u2026"
                    db.commit()
                from src.interactive_gate import wait_for_interactive_quiet
                await wait_for_interactive_quiet(f"scheduled task {task.name}")

            # Flip the run from queued \u2192 running
            run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
            if run:
                run.status = "running"
                run.started_at = _utcnow()
                run.result = "Starting\u2026"
                db.commit()
            else:
                run = TaskRun(
                    id=run_id,
                    task_id=task.id,
                    started_at=_utcnow(),
                    status="running",
                    result="Starting\u2026",
                )
                db.add(run)
                db.commit()

            task_type = task.task_type or "llm"

            from src.builtin_actions import TaskDeferred, TaskNoop

            self._last_run_model = None
            foreground_cancel = {"hit": False}
            foreground_monitor = None
            if gate_foreground:
                current_task = asyncio.current_task()

                async def _cancel_if_foreground_active():
                    await asyncio.sleep(0.1)
                    from src.interactive_gate import has_foreground_activity
                    while True:
                        await asyncio.sleep(0.25)
                        if has_foreground_activity():
                            foreground_cancel["hit"] = True
                            logger.info("Task '%s' interrupted because Odysseus became active", task.name)
                            if current_task:
                                current_task.cancel()
                            return

                foreground_monitor = asyncio.create_task(_cancel_if_foreground_active())
            try:
                if task_type == "action":
                    result, success = await self._execute_action(task, run_id=run_id)
                    run.status = "success" if success else "error"
                    run.result = result
                    if not success:
                        run.error = result
                elif task_type == "research":
                    result = await self._execute_research_task(task, db)
                    run.status = "success"
                    run.result = result
                else:
                    # LLM task
                    result = await self._execute_llm_task(task, db)
                    run.status = "success"
                    run.result = result
                if getattr(self, "_last_run_model", None):
                    run.model = self._last_run_model
                if run.status == "success":
                    await self._deliver_task_result(task, result, db, model=getattr(self, "_last_run_model", None))
            except TaskDeferred as defer:
                count = self._task_defer_counts.get(task_id, 0) + 1
                self._task_defer_counts[task_id] = count
                delay_seconds = int(getattr(defer, "delay_seconds", 20 * 60) or (20 * 60))
                if count > 2:
                    delay_seconds = max(delay_seconds, 40 * 60)
                when = _utcnow() + timedelta(seconds=delay_seconds)
                logger.info(
                    "Task '%s' deferred for %ss after %s quiet-window hit(s): %s",
                    task.name, delay_seconds, count, defer,
                )
                run_obj = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run_obj:
                    db.delete(run_obj)
                task.next_run = when
                db.commit()
                return
            except asyncio.CancelledError:
                msg = (
                    "Paused because Odysseus became active"
                    if foreground_cancel.get("hit")
                    else "Stopped by user"
                )
                logger.info("Task '%s' %s", task.name, msg)
                run_obj = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run_obj:
                    run_obj.status = "aborted"
                    run_obj.error = msg
                    run_obj.result = run_obj.result or msg
                    run_obj.finished_at = _utcnow()
                task.last_run = _utcnow()
                if foreground_cancel.get("hit"):
                    task.next_run = _utcnow() + timedelta(minutes=15)
                elif (task.trigger_type or "schedule") == "schedule":
                    task.next_run = compute_next_run(
                        task.schedule, task.scheduled_time,
                        task.scheduled_day, task.scheduled_date,
                        after=_utcnow(),
                        cron_expression=task.cron_expression,
                        tz_name=_resolve_task_timezone(db, task),
                    )
                else:
                    task.next_run = None
                db.commit()
                return
            except TaskNoop as noop:
                logger.info(f"Task '{task.name}' no-op: {noop}")
                run.status = "skipped"
                run.result = str(noop)
                run.finished_at = _utcnow()
                task.last_run = _utcnow()
                if (task.trigger_type or "schedule") == "schedule":
                    task.next_run = compute_next_run(
                        task.schedule, task.scheduled_time,
                        task.scheduled_day, task.scheduled_date,
                        after=_utcnow(),
                        cron_expression=task.cron_expression,
                        tz_name=_resolve_task_timezone(db, task),
                    )
                else:
                    task.next_run = None
                db.commit()
                return
            finally:
                if foreground_monitor and not foreground_monitor.done():
                    foreground_monitor.cancel()
                    try:
                        await foreground_monitor
                    except asyncio.CancelledError:
                        pass

            run.finished_at = _utcnow()

            # Update task
            task.last_run = _utcnow()
            task.run_count = (task.run_count or 0) + 1
            self._task_defer_counts.pop(task_id, None)

            # Compute next run only for schedule-triggered tasks
            if (task.trigger_type or "schedule") == "schedule":
                task.next_run = compute_next_run(
                    task.schedule, task.scheduled_time,
                    task.scheduled_day, task.scheduled_date,
                    after=_utcnow(),
                    cron_expression=task.cron_expression,
                    tz_name=_resolve_task_timezone(db, task),
                )
                if task.next_run is None and task.schedule == "once":
                    task.status = "completed"
            else:
                task.next_run = None

            db.commit()
            logger.info(f"Task '{task.name}' completed (run {run_id})")
            output = task.output_target or "session"
            should_notify = (
                (task.task_type or "llm") in {"llm", "research"}
                and getattr(task, "notifications_enabled", True)
            )
            if should_notify:
                self.add_notification(
                    task.name,
                    run.status,
                    task_id,
                    owner=task.owner,
                    body=run.result if output == "notification" else None,
                )
            elif run.status == "error":
                self.add_notification(
                    task.name,
                    "error",
                    task_id,
                    owner=task.owner,
                    body=run.error or run.result,
                )

            if run.status == "success":
                self._log_to_assistant(db, task, run.result or "[success]")

            # Task chaining
            if run.status == "success" and task.then_task_id:
                chain_id = task.then_task_id
                chain_task = db.query(ScheduledTask).filter(ScheduledTask.id == chain_id).first()
                if not chain_task or chain_task.owner != task.owner:
                    logger.warning(
                        "Skipping chain from %r: target task %s is missing or not owned by %r",
                        task.name, chain_id, task.owner,
                    )
                elif not self._has_chain_cycle(db, chain_id, owner=task.owner):
                    logger.info(f"Chaining: '{task.name}' \u2192 task {chain_id}")
                    asyncio.create_task(self._run_chained(chain_id))
                else:
                    logger.warning(f"Skipping chain from '{task.name}': cycle detected")

        except Exception as exec_exc:
            logger.exception(f"Task {task_id} execution error")
            _owner = None
            try:
                _t = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                _owner = _t.owner if _t else None
            except Exception:
                pass
            _should_notify_error = False
            try:
                _t_for_notify = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                _should_notify_error = (
                    bool(_t_for_notify)
                    and (_t_for_notify.task_type or "llm") in {"llm", "research"}
                    and getattr(_t_for_notify, "notifications_enabled", True)
                )
            except Exception:
                _should_notify_error = False
            if _should_notify_error:
                self.add_notification(f"Task {task_id}", "error", task_id, owner=_owner)
            try:
                err_text = f"{type(exec_exc).__name__}: {exec_exc}"
                run_obj = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run_obj and run_obj.status in ("running", "success"):
                    run_obj.status = "error"
                    run_obj.error = err_text[:2000]
                    run_obj.finished_at = _utcnow()
                task_obj = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                if task_obj and (task_obj.trigger_type or "schedule") == "schedule":
                    task_obj.last_run = _utcnow()
                    try:
                        task_obj.next_run = compute_next_run(
                            task_obj.schedule, task_obj.scheduled_time,
                            task_obj.scheduled_day, task_obj.scheduled_date,
                            after=_utcnow(),
                            cron_expression=task_obj.cron_expression,
                            tz_name=_resolve_task_timezone(db, task_obj),
                        )
                    except Exception:
                        pass
                try:
                    db.commit()
                except Exception as commit_err:
                    logger.warning("Task %s error-path commit failed: %s \u2014 falling back", task_id, commit_err)
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    from datetime import timedelta as _td
                    _recover_db = SessionLocal()
                    try:
                        _r = _recover_db.query(TaskRun).filter(TaskRun.id == run_id).first()
                        if _r and _r.status in ("running", "queued"):
                            _r.status = "aborted"
                            _r.error = f"commit_failed: {type(commit_err).__name__}: {commit_err}"[:2000]
                            _r.finished_at = _utcnow()
                        _t = _recover_db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                        if _t and (_t.trigger_type or "schedule") == "schedule":
                            _t.next_run = _utcnow() + _td(minutes=5)
                            _t.last_run = _utcnow()
                        _recover_db.commit()
                    except Exception as recover_err:
                        logger.error("Task %s recovery commit ALSO failed: %s", task_id, recover_err)
                    finally:
                        _recover_db.close()
            except Exception:
                logger.exception("Task %s error-path failed unexpectedly", task_id)
        finally:
            db.close()
            handle = self._task_handles.get(task_id)
            if handle is asyncio.current_task():
                self._task_handles.pop(task_id, None)
            if release_executing:
                async with self._executing_lock:
                    self._executing.discard(task_id)
