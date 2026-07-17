"""
builtin_actions.py

Registry of built-in automation actions that can be executed by the task
scheduler without needing an LLM call.
"""

import logging
import os
import json
from datetime import datetime
from typing import Tuple

from src.auth_helpers import owner_filter
from core.platform_compat import IS_WINDOWS, find_bash
from core.constants import internal_api_base
from src.constants import DATA_DIR, DEEP_RESEARCH_DIR, TIDY_CALENDAR_STATE_FILE, EMAIL_URGENCY_CACHE_DIR, COOKBOOK_STATE_FILE
from src.interactive_gate import wait_for_interactive_quiet

logger = logging.getLogger(__name__)


class TaskNoop(BaseException):
    """Raised by an action when it determined there's nothing to do.

    Inherits from BaseException (not Exception) so the standard
    `except Exception` wrappers each action uses for real error handling
    don't accidentally catch it. The scheduler explicitly catches TaskNoop,
    drops the queued TaskRun row, advances last_run / next_run, and exits
    silently. Nothing appears in the Activity log; the message is logged
    server-side only.
    """


class TaskDeferred(BaseException):
    """Raised when a task should run later without recording a skipped run."""

    def __init__(self, reason: str, delay_seconds: int = 20 * 60):
        super().__init__(reason)
        self.reason = reason
        self.delay_seconds = delay_seconds



async def action_tidy_sessions(owner: str, **kwargs) -> Tuple[str, bool]:
    """Delete empty sessions for the owner. Pure heuristic —
    the LLM folder-sort phase is skipped (user opted to keep this task
    LLM-free; sorting can be triggered manually via the Chats UI)."""
    try:
        import asyncio
        from src.session_actions import run_auto_sort
        result = await asyncio.wait_for(
            run_auto_sort(owner, skip_llm=True, delete_throwaway=False),
            timeout=60,
        )
        return result, True
    except asyncio.TimeoutError:
        logger.error("tidy_sessions action timed out")
        return "Chat session tidy timed out", False
    except Exception as e:
        logger.error(f"tidy_sessions action failed: {e}")
        return str(e), False


async def action_tidy_documents(owner: str, **kwargs) -> Tuple[str, bool]:
    """Run tidy on documents for the owner."""
    try:
        from src.document_actions import run_document_tidy
        result = await run_document_tidy(owner)
        return result, True
    except Exception as e:
        logger.error(f"tidy_documents action failed: {e}")
        return str(e), False


async def action_consolidate_memory(owner: str, **kwargs) -> Tuple[str, bool]:
    """Consolidate/deduplicate memories for the owner."""
    try:
        import json
        import re
        from src.constants import DATA_DIR
        from src.llm_core import llm_call_async_with_fallback
        from src.memory import MemoryManager

        manager = MemoryManager(DATA_DIR)
        all_memories = manager.load_all()

        _owner_clean = (owner or "").strip()
        text_limit = 2000

        def _memory_owner(mem: dict) -> str:
            return (mem.get("owner") or "").strip()

        # Built-in housekeeping can run without an owner. In that case scan all
        # memories, but keep every AI prompt/apply step owner-local.
        if _owner_clean:
            memory_groups = {
                _owner_clean: [m for m in all_memories if _memory_owner(m) == _owner_clean]
            }
        else:
            memory_groups = {}
            for mem in all_memories:
                memory_groups.setdefault(_memory_owner(mem), []).append(mem)

        memory_groups = {group_owner: group for group_owner, group in memory_groups.items() if group}
        if not memory_groups:
            raise TaskNoop("no memories to consolidate")

        total_removed = 0
        total_cleaned = 0
        total_scanned = 0
        removed_examples = []
        ai_reasons = []
        ai_used = False

        async def _try_ai_tidy_group(group_owner: str, group_memories: list) -> bool:
            nonlocal all_memories, total_removed, total_cleaned, total_scanned, ai_used
            if len(group_memories) < 2:
                return False

            from src.task_endpoint import resolve_task_candidates
            candidates = resolve_task_candidates(owner=group_owner or None)
            if not candidates:
                return False

            try:
                items = [
                    {
                        "id": m.get("id"),
                        "category": m.get("category", "fact"),
                        "text": (m.get("text") or "").strip()[:text_limit],
                        "truncated": len((m.get("text") or "").strip()) > text_limit,
                    }
                    for m in group_memories
                    if m.get("id") and (m.get("text") or "").strip()
                ]
                if len(items) < 2:
                    return False
                truncated_ids = {item["id"] for item in items if item.get("truncated")}
                prompt = (
                    "You are tidying a user's saved personal memories. Return ONLY raw JSON, no markdown.\n"
                    "Remove memories that are empty, broken, trivial conversation filler, duplicates, or obsolete "
                    "because a clearer newer memory replaces them. Preserve useful personal facts, preferences, "
                    "contacts, project context, and instructions. If memories conflict, keep the clearest/latest "
                    "one and drop the obsolete one.\n\n"
                    "JSON shape:\n"
                    "{\"keep\":[{\"id\":\"existing id\",\"text\":\"cleaned text\",\"category\":\"fact|preference|identity|event|contact|project|instruction\"}],"
                    "\"drop\":[{\"id\":\"existing id\",\"reason\":\"short reason\"}]}\n\n"
                    f"MEMORIES:\n{json.dumps(items, ensure_ascii=False)}"
                )
                await wait_for_interactive_quiet("memory consolidation action")
                raw = await llm_call_async_with_fallback(
                    candidates,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=4096,
                    timeout=120,
                )
                from src.text_helpers import strip_think

                raw = strip_think(raw or "", prose=False, prompt_echo=False).strip()
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
                start = raw.find("{")
                end = raw.rfind("}")
                if start != -1 and end != -1 and end > start:
                    decision = json.loads(raw[start:end + 1])
                    keep_items = decision.get("keep") if isinstance(decision, dict) else None
                    drop_items = decision.get("drop") if isinstance(decision, dict) else None
                    if isinstance(keep_items, list) and isinstance(drop_items, list):
                        by_id = {m.get("id"): m for m in group_memories if m.get("id")}
                        cleaned_by_id = {}
                        for item in keep_items:
                            if not isinstance(item, dict):
                                continue
                            mid = item.get("id")
                            if mid not in by_id:
                                continue
                            text = (item.get("text") or "").strip()
                            if not text:
                                continue
                            cleaned = {
                                "category": (item.get("category") or by_id[mid].get("category") or "fact").strip(),
                            }
                            original_text = (by_id[mid].get("text") or "").strip()
                            if len(original_text) <= text_limit:
                                cleaned["text"] = text
                            cleaned_by_id[mid] = cleaned

                        # Delete only memories the model EXPLICITLY dropped, never
                        # ones it merely omitted from `keep`. Treating the
                        # complement of `keep` as deletions meant a model that
                        # forgot to re-list an id (common) silently destroyed that
                        # memory. Honor the explicit `drop` set instead.
                        drop_ids = {
                            d.get("id")
                            for d in drop_items
                            if isinstance(d, dict) and d.get("id") in by_id
                        }
                        # Never delete a memory the model only saw truncated.
                        drop_ids -= truncated_ids

                        if drop_ids or cleaned_by_id:
                            changed_text = 0
                            group_ref_ids = {id(m) for m in group_memories}
                            kept_all = []
                            for mem in all_memories:
                                if id(mem) not in group_ref_ids:
                                    kept_all.append(mem)
                                    continue
                                mid = mem.get("id")
                                if mid in drop_ids:
                                    continue
                                cleaned = cleaned_by_id.get(mid) or {}
                                if mid in truncated_ids:
                                    cleaned.pop("text", None)
                                if cleaned.get("text") and cleaned["text"] != mem.get("text"):
                                    mem["text"] = cleaned["text"]
                                    changed_text += 1
                                if cleaned.get("category"):
                                    mem["category"] = cleaned["category"]
                                kept_all.append(mem)

                            removed = sum(1 for m in group_memories if m.get("id") in drop_ids)
                            total_scanned += len(group_memories)
                            if removed or changed_text:
                                all_memories = kept_all
                                total_removed += removed
                                total_cleaned += changed_text
                                ai_used = True
                                ai_reasons.extend([
                                    (d.get("reason") or "").strip()
                                    for d in drop_items
                                    if isinstance(d, dict) and (d.get("reason") or "").strip()
                                ])
                            return True
            except Exception as ai_err:
                logger.warning("AI memory tidy failed; falling back to duplicate cleanup: %s", ai_err)
            return False

        for group_owner, group_memories in memory_groups.items():
            if await _try_ai_tidy_group(group_owner, group_memories):
                continue

            seen = {}
            keep_refs = set()
            total_scanned += len(group_memories)
            for mem in group_memories:
                text = (mem.get("text") or "").strip()
                key = " ".join(text.lower().split())
                if not key:
                    if len(removed_examples) < 3:
                        removed_examples.append("(empty)")
                    continue
                if key in seen:
                    if len(removed_examples) < 3:
                        removed_examples.append(text[:60] + ("..." if len(text) > 60 else ""))
                    continue
                seen[key] = mem
                keep_refs.add(id(mem))

            group_removed = len(group_memories) - len(keep_refs)
            if group_removed == 0:
                continue

            group_ref_ids = {id(m) for m in group_memories}
            all_memories = [
                m for m in all_memories
                if id(m) not in group_ref_ids or id(m) in keep_refs
            ]
            total_removed += group_removed

        if total_removed or total_cleaned:
            manager.save(all_memories)
            if ai_used:
                reasons = ai_reasons[:3]
                reason_text = f": {'; '.join(reasons)}" if reasons else ""
                return (
                    f"AI tidied {total_scanned} memories: "
                    f"removed {total_removed}, cleaned {total_cleaned}{reason_text}",
                    True,
                )
            preview = "; ".join(removed_examples)
            extra = f" (+{total_removed - len(removed_examples)} more)" if total_removed > len(removed_examples) else ""
            return f"Removed {total_removed} duplicate(s) of {total_scanned}: {preview}{extra}", True

        raise TaskNoop(f"scanned {total_scanned} memories, no duplicates")
    except Exception as e:
        logger.error(f"consolidate_memory action failed: {e}")
        return str(e), False


# Registry: action name -> async function(owner, **kwargs) -> (result_str, success_bool)


async def _run_subprocess(argv, *, shell: bool = False, timeout: int = 120, label: str = "Command") -> Tuple[str, bool]:
    """Shared subprocess runner. Wraps the blocking subprocess.run in
    asyncio.to_thread so the event loop stays responsive."""
    import asyncio
    import subprocess
    try:
        result = await asyncio.to_thread(
            subprocess.run, argv, shell=shell, capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout or "").strip()
        if result.returncode != 0 and result.stderr:
            output += "\nSTDERR: " + result.stderr.strip()
        return output or "(no output)", result.returncode == 0
    except subprocess.TimeoutExpired:
        return f"{label} timed out ({timeout}s)", False
    except Exception as e:
        return str(e), False


async def action_ssh_command(owner: str, command: str = "", host: str = "localhost", **kwargs) -> Tuple[str, bool]:
    """Run a shell command locally or on a remote host via SSH."""
    if not command:
        return "No command specified", False
    if host in ("localhost", "127.0.0.1", "local"):
        if IS_WINDOWS:
            bash = find_bash()
            if bash:
                return await _run_subprocess([bash, "-c", command], timeout=120, label="Command")
            return await _run_subprocess(command, shell=True, timeout=120, label="Command")
        return await _run_subprocess(["bash", "-c", command], timeout=120, label="Command")
    return await _run_subprocess(
        ["ssh", "-o", "ConnectTimeout=10", host, command], timeout=120, label="Command",
    )


async def action_run_script(owner: str, script: str = "", host: str = "", **kwargs) -> Tuple[str, bool]:
    """Run a script locally, or via SSH when a host is configured."""
    if not script:
        return "No script specified", False
    target_host = (host or os.getenv("ODYSSEUS_SCRIPT_HOST", "localhost")).strip()
    if target_host in ("", "localhost", "127.0.0.1", "local"):
        if IS_WINDOWS and find_bash():
            return await _run_subprocess([find_bash(), "-c", script], timeout=300, label="Script")
        return await _run_subprocess(script, shell=True, timeout=300, label="Script")
    return await _run_subprocess(["ssh", target_host, script], timeout=300, label="Script")


async def action_run_local(owner: str, script: str = "", **kwargs) -> Tuple[str, bool]:
    """Run a script locally (no SSH)."""
    if not script:
        return "No script specified", False
    if IS_WINDOWS and find_bash():
        return await _run_subprocess([find_bash(), "-c", script], timeout=300, label="Script")
    return await _run_subprocess(script, shell=True, timeout=300, label="Script")


async def action_tidy_research(owner: str, **kwargs) -> Tuple[str, bool]:
    """Remove only broken research files (empty or unparseable JSON).

    Research history lives entirely in data/deep_research/<id>.json and is NOT
    backed by chat-session rows — so a file must never be deleted just because
    no chat session matches its id. Only prune files that fail to load."""
    try:
        from pathlib import Path
        import json as _json
        research_dir = Path(DEEP_RESEARCH_DIR)
        if not research_dir.exists():
            raise TaskNoop("no research directory")
        files = list(research_dir.glob("*.json"))
        removed = []
        for p in files:
            try:
                txt = p.read_text(encoding="utf-8").strip()
                if not txt:
                    raise ValueError("empty file")
                _json.loads(txt)  # valid JSON → keep
            except Exception:
                p.unlink(missing_ok=True)
                removed.append(p.stem[:8])
        if not removed:
            raise TaskNoop(f"scanned {len(files)} research file(s), none broken")
        return f"Removed {len(removed)} broken research file(s) of {len(files)}", True
    except Exception as e:
        logger.error(f"tidy_research action failed: {e}")
        return str(e), False


async def action_tidy_calendar(owner: str, **kwargs) -> Tuple[str, bool]:
    """Find duplicate calendar events (same title + start time) and DELETE the dups,
    keeping the oldest (first-seen) instance.

    Incremental: remembers the newest `created_at` already scanned in
    data/tidy_calendar_state.json. If no events have been added since then,
    short-circuits. Otherwise only events newer than the watermark are candidates
    for deletion, but they're checked against the FULL existing set so a new
    duplicate of an old event still gets caught.
    """
    try:
        import json
        from pathlib import Path
        from core.database import SessionLocal, CalendarEvent
        from sqlalchemy import func

        STATE_FILE = Path(TIDY_CALENDAR_STATE_FILE)
        last_watermark = None
        try:
            if STATE_FILE.exists():
                saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if saved.get("last_created_at"):
                    last_watermark = datetime.fromisoformat(saved["last_created_at"])
        except Exception:
            last_watermark = None

        db = SessionLocal()
        try:
            newest = db.query(func.max(CalendarEvent.created_at)).scalar()
            db.query(CalendarEvent).count()

            # Short-circuit: nothing new since last run
            if last_watermark is not None and newest is not None and newest <= last_watermark:
                raise TaskNoop(f"no new events since watermark {last_watermark.strftime('%Y-%m-%d %H:%M')}")

            events = db.query(CalendarEvent).order_by(CalendarEvent.dtstart).all()
            # Build full seen-set from events at or before the watermark (known-clean).
            # Events after the watermark are candidates for deletion.
            seen = {}
            candidates = []
            no_title = 0
            for e in events:
                title = (e.summary or "").strip()
                if not title:
                    no_title += 1
                    continue
                if last_watermark is None or (e.created_at and e.created_at <= last_watermark):
                    # Known-clean region: first occurrence wins
                    key = (title.lower(), e.dtstart)
                    if key not in seen:
                        seen[key] = e
                    # If a dup exists in the known-clean region (first run, or imported later
                    # with the same created_at), still remove it — fall through to candidate check.
                    else:
                        candidates.append(e)
                else:
                    candidates.append(e)

            removed = []
            for e in candidates:
                title = (e.summary or "").strip()
                key = (title.lower(), e.dtstart)
                if key in seen:
                    when = e.dtstart.strftime('%Y-%m-%d %H:%M') if e.dtstart else '?'
                    removed.append(f"{title} @ {when}")
                    db.delete(e)
                else:
                    seen[key] = e

            if removed:
                db.commit()

            # Persist the new watermark (newest created_at among events that survive)
            try:
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                if newest is not None:
                    STATE_FILE.write_text(json.dumps({
                        "last_created_at": newest.isoformat(),
                        "last_run_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                        "scanned": len(events),
                        "removed": len(removed),
                    }, indent=2), encoding="utf-8")
            except Exception as se:
                logger.warning(f"tidy_calendar watermark save failed: {se}")

            new_since = len(candidates)
            parts = [f"Scanned {len(events)} event(s), {new_since} new since last run"]
            if removed:
                preview = "; ".join(removed[:5])
                if len(removed) > 5:
                    preview += f" (+{len(removed) - 5} more)"
                parts.append(f"removed {len(removed)} duplicate(s): {preview}")
            if no_title:
                parts.append(f"{no_title} untitled (kept)")
            if not removed and not no_title:
                parts.append("no duplicates")
            return " · ".join(parts), True
        finally:
            db.close()
    except Exception as e:
        logger.error(f"tidy_calendar action failed: {e}")
        return str(e), False


def _result_has_work(result: str | None) -> bool:
    """Heuristic: did the email pass actually process anything?

    `_run_auto_summarize_once` returns strings like 'Processed 0 emails',
    'No new emails to summarize', 'Tagged 0 / Moved 0', etc. when nothing
    was done. Used to decide whether to record the run or noop it.
    """
    if not isinstance(result, str) or not result:
        return False
    low = result.lower()
    if "processed 0" in low or "no new" in low or "nothing to" in low:
        return False
    # "Tagged 0 / Moved 0" or similar zero-count summaries
    if low.count(" 0") >= 2 and ("tagged" in low or "moved" in low or "drafted" in low):
        return False
    return True


def _result_is_config_error(result: str | None) -> bool:
    if not isinstance(result, str):
        return False
    low = result.lower()
    return (
        "no model configured" in low
        or "no model endpoint configured" in low
        or "no llm endpoint available" in low
    )


def _email_task_account_id(kwargs) -> str | None:
    prompt = (kwargs.get("prompt") or "").strip()
    if not prompt:
        return None
    try:
        data = json.loads(prompt)
        if isinstance(data, dict):
            val = data.get("account_id") or data.get("email_account_id")
            return str(val).strip() or None
    except Exception:
        pass
    for line in prompt.splitlines():
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        if key.strip().lower() in {"account_id", "email_account_id"}:
            return val.strip() or None
    return None


async def action_summarize_emails(owner: str, **kwargs) -> Tuple[str, bool]:
    """Run one pass of email summary background processing."""
    try:
        from routes.email_pollers import _run_auto_summarize_once
        result = await _run_auto_summarize_once(
            do_summary=True,
            do_reply=False,
            account_id=_email_task_account_id(kwargs),
        )
        if _result_is_config_error(result):
            return result, False
        if not _result_has_work(result):
            raise TaskNoop(f"summarize: {result or 'no new emails'}")
        return result, True
    except Exception as e:
        logger.error(f"summarize_emails action failed: {e}")
        return str(e), False


async def action_draft_email_replies(owner: str, **kwargs) -> Tuple[str, bool]:
    """Run one pass of AI reply drafting."""
    try:
        from routes.email_pollers import _run_auto_summarize_once
        result = await _run_auto_summarize_once(
            do_summary=False,
            do_reply=True,
            account_id=_email_task_account_id(kwargs),
            days_back=7,
            progress_cb=kwargs.get("progress_cb"),
        )
        if _result_is_config_error(result):
            return result, False
        if not _result_has_work(result):
            raise TaskNoop(f"draft replies: {result or 'no new emails'}")
        return result, True
    except Exception as e:
        logger.error(f"draft_email_replies action failed: {e}")
        return str(e), False


async def action_email_auto_translate(owner: str, **kwargs) -> Tuple[str, bool]:
    """Detect recent foreign-language emails and cache translated text.

    The reader still shows the original body; it simply checks this cache
    before calling the LLM on demand. Keep the scheduled pass deliberately
    small so translation never turns into a mailbox-wide background crawl.
    """
    try:
        import email as _email_mod
        import json as _json
        import re as _re
        import sqlite3 as _sql3
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        from core.database import EmailAccount as _EA, SessionLocal as _SL
        from routes.email_helpers import (
            SCHEDULED_DB,
            _decode_header,
            _email_cache_owner_clause,
            _extract_reply,
            _extract_text,
            _imap_connect,
            email_translation_body_hash,
        )
        from src.settings import load_settings
        from src.task_endpoint import task_llm_call_async

        settings = load_settings()
        if not settings.get("email_auto_translate", False):
            raise TaskNoop("email auto-translate is disabled")

        target_language = (settings.get("email_translate_language") or "English").strip() or "English"
        account_id = _email_task_account_id(kwargs)
        days_back = 7
        max_process = 5
        try:
            data = _json.loads((kwargs.get("prompt") or "").strip() or "{}")
            if isinstance(data, dict):
                days_back = max(1, min(30, int(data.get("days_back") or days_back)))
                max_process = max(1, min(20, int(data.get("max_process") or max_process)))
        except Exception:
            pass

        db = _SL()
        try:
            from sqlalchemy import and_ as _and, or_ as _or
            q = db.query(_EA).filter(_EA.enabled == True)  # noqa: E712
            if owner:
                unowned = _or(_EA.owner == None, _EA.owner == "")  # noqa: E711
                same_mailbox = _or(_EA.imap_user == owner, _EA.from_address == owner)
                q = q.filter(_or(_EA.owner == owner, _and(unowned, same_mailbox)))
            if account_id:
                q = q.filter(_EA.id == account_id)
            accounts = q.all()
        finally:
            db.close()
        if not accounts:
            raise TaskNoop("no email accounts configured")

        def _cached(body_hash: str) -> bool:
            c = _sql3.connect(SCHEDULED_DB)
            try:
                owner_clause, owner_params = _email_cache_owner_clause(owner)
                row = c.execute(
                    f"SELECT 1 FROM email_translations "
                    f"WHERE body_hash = ? AND target_language = ? AND {owner_clause} LIMIT 1",
                    (body_hash, target_language, *owner_params),
                ).fetchone()
                return bool(row)
            finally:
                c.close()

        def _store(
            body_hash: str,
            *,
            uid: str,
            folder: str,
            subject: str,
            sender: str,
            translation: str,
            same_language: bool,
            model_used: str,
        ) -> None:
            c = _sql3.connect(SCHEDULED_DB)
            try:
                c.execute("""
                    INSERT OR REPLACE INTO email_translations
                    (body_hash, owner, target_language, uid, folder, subject, sender,
                     translation, same_language, model_used, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    body_hash, owner, target_language, uid, folder, subject, sender,
                    translation, 1 if same_language else 0, model_used, _dt.now(_tz.utc).replace(tzinfo=None).isoformat(),
                ))
                c.commit()
            finally:
                c.close()

        async def _translate(body: str, subject: str, sender: str) -> tuple[str, bool]:
            content = await task_llm_call_async(
                [
                    {
                        "role": "system",
                        "content": (
                            "You translate emails faithfully. Preserve meaning, names, dates, money, addresses, "
                            "bullet structure, and tone. Do not summarize or answer the email. "
                            "Output only the translation between <<<TRANSLATION>>> and <<<END>>>. "
                            "If the email is already primarily in the target language, output exactly "
                            "<<<SAME_LANGUAGE>>>."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Target language: {target_language}\n\n"
                            f"From: {sender}\nSubject: {subject}\n\n{body[:16000]}\n\n"
                            "Translate the email unless it is already primarily in the target language.\n"
                            "Return only:\n<<<TRANSLATION>>>\ntranslated text\n<<<END>>>"
                        ),
                    },
                ],
                owner=owner,
                temperature=0.2,
                max_tokens=8192,
                timeout=180,
            )
            content = (content or "").strip()
            content = _extract_reply(content)
            if "<<<SAME_LANGUAGE>>>" in content:
                return "", True
            marker = _re.search(r"<<<TRANSLATION>>>\s*(.*?)\s*<<<END>>>", content, _re.S | _re.I)
            if marker:
                content = marker.group(1).strip()
            else:
                content = _re.sub(r"^\s*<<<TRANSLATION>>>\s*", "", content, flags=_re.I).strip()
                content = _re.sub(r"\s*<<<END>>>\s*$", "", content, flags=_re.I).strip()
            return content, False

        since = (_dt.now(_tz.utc).replace(tzinfo=None) - _td(days=days_back)).strftime("%d-%b-%Y")
        examined = 0
        cached = 0
        translated = 0
        same_language = 0
        skipped = 0
        failures = 0
        processed = 0

        for acct in accounts:
            if processed >= max_process:
                break
            imap = None
            try:
                imap = _imap_connect(acct.id, owner=owner)
                imap.select("INBOX", readonly=True)
                status, data = imap.uid("SEARCH", None, f'(SINCE {since})')
                if status != "OK" or not data or not data[0]:
                    continue
                uids = list(reversed(data[0].split()))[:50]
                for uid_b in uids:
                    if processed >= max_process:
                        break
                    uid = uid_b.decode("utf-8", errors="ignore") if isinstance(uid_b, bytes) else str(uid_b)
                    status, msg_data = imap.uid("FETCH", uid, "(RFC822)")
                    if status != "OK" or not msg_data:
                        continue
                    raw = None
                    for part in msg_data:
                        if isinstance(part, tuple) and len(part) > 1:
                            raw = part[1]
                            break
                    if not raw:
                        continue
                    msg = _email_mod.message_from_bytes(raw)
                    subject = _decode_header(msg.get("Subject", ""))
                    sender = _decode_header(msg.get("From", ""))
                    body = (_extract_text(msg) or "").strip()
                    examined += 1
                    if len(body) < 80:
                        skipped += 1
                        continue
                    body_hash = email_translation_body_hash(body)
                    if _cached(body_hash):
                        cached += 1
                        continue
                    translation, is_same_language = await _translate(body, subject, sender)
                    if is_same_language:
                        _store(
                            body_hash,
                            uid=uid,
                            folder="INBOX",
                            subject=subject,
                            sender=sender,
                            translation="",
                            same_language=True,
                            model_used="background-task",
                        )
                        same_language += 1
                        processed += 1
                        continue
                    if not translation:
                        failures += 1
                        continue
                    _store(
                        body_hash,
                        uid=uid,
                        folder="INBOX",
                        subject=subject,
                        sender=sender,
                        translation=translation,
                        same_language=False,
                        model_used="background-task",
                    )
                    translated += 1
                    processed += 1
            except Exception as acct_e:
                failures += 1
                logger.warning(f"email_auto_translate account scan failed for {getattr(acct, 'id', '?')}: {acct_e}")
            finally:
                if imap:
                    try:
                        imap.logout()
                    except Exception:
                        pass

        if translated == 0 and same_language == 0:
            result = (
                f"no uncached foreign-language emails found "
                f"(examined {examined}, cached {cached}, skipped {skipped}, failures {failures})"
            )
            if failures:
                return f"Email Auto Translate failed: {result}", False
            raise TaskNoop(result)
        return (
            f"Email Auto Translate cached {translated} translation(s), marked {same_language} same-language "
            f"(examined {examined}, already cached {cached}, skipped {skipped}, failures {failures})",
            True,
        )
    except TaskNoop:
        raise
    except Exception as e:
        logger.error(f"email_auto_translate action failed: {e}")
        return str(e), False


_TYPE_COLORS = {
    "work":     "#5b8abf",  # blue
    "personal": "#a07ae0",  # purple
    "health":   "#e06c75",  # red
    "travel":   "#e5a33a",  # orange
    "meal":     "#d8b974",  # tan
    "social":   "#82c882",  # green
    "admin":    "#888888",  # gray
    "other":    "#6b9cb5",  # default
}

_HEURISTIC_TYPES = {
    "health":  ["doctor", "dentist", "clinic", "hospital", "appointment", "checkup", "therapy",
                "physio", "chiropract", "vaccine", "blood test", "xray", "scan", "surgery"],
    "travel":  ["flight", "airport", "train", "shinkansen", "boarding", "uber", "taxi", "trip",
                "hotel", "airbnb", "depart", "arrival", "check-in", "checkout"],
    "meal":    ["lunch", "dinner", "breakfast", "brunch", "coffee", "drinks", "restaurant",
                "reservation", "bar", "cafe"],
    "social":  ["birthday", "party", "hangout", "wedding", "date with", "drinks with",
                "anniversary", "baby shower", "graduation", "picnic", "bbq"],
    "admin":   ["bill", "renewal", "tax", "deadline", "filing", "submit", "due date",
                "registration", "license", "passport", "visa", "form"],
    "work":    ["meeting", "standup", "sync", "1:1", "1on1", "review", "interview",
                "demo", "presentation", "kickoff", "retro", "all-hands", "town hall",
                "call with", "client", "deck"],
}

_HEURISTIC_HIGH = ["flight", "interview", "wedding", "surgery", "exam", "deadline",
                   "court", "presentation", "demo", "kickoff", "launch"]
_HEURISTIC_CRITICAL = ["surgery", "court", "wedding day", "funeral", "delivery date"]


def _classify_event_heuristic(summary: str) -> tuple:
    """Quick heuristic classification — returns (event_type, importance) or (None, None) if unclear."""
    s = (summary if isinstance(summary, str) else "").lower()
    etype = None
    for t, kws in _HEURISTIC_TYPES.items():
        if any(k in s for k in kws):
            etype = t
            break
    if any(k in s for k in _HEURISTIC_CRITICAL):
        return etype, "critical"
    if any(k in s for k in _HEURISTIC_HIGH):
        return etype, "high"
    return etype, None


def _memory_context_lines(mems, limit: int = 40) -> list:
    """Render Memory rows into short personal-context bullets for event classify.

    Reads the Memory ORM `text` column. The previous inline code read a
    non-existent `content` attribute, so it raised AttributeError on the first
    row, the surrounding except swallowed it, and the classifier ran with no
    personal context at all. getattr keeps it robust to future schema drift.
    """
    lines: list = []
    for m in mems:
        c = (getattr(m, "text", "") or "").strip()
        if c:
            lines.append(f"- {c[:200]}")
        if len(lines) >= limit:
            break
    return lines


async def action_classify_events(owner: str, **kwargs) -> Tuple[str, bool]:
    """Hybrid classification of upcoming calendar events: fast heuristic for
    obvious cases, LLM fallback for ambiguous ones. Assigns event_type +
    importance + color. Re-classifies anything not already set."""
    try:
        from datetime import timedelta, timezone
        from core.database import SessionLocal, CalendarEvent
        from src.llm_core import llm_call_async_with_fallback
        import re as _re, json as _json

        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            horizon = now + timedelta(days=30)
            events = db.query(CalendarEvent).filter(
                CalendarEvent.dtstart >= now,
                CalendarEvent.dtstart <= horizon,
                CalendarEvent.status != "cancelled",
            ).all()
            if not events:
                return "No upcoming events to classify", True

            from src.task_endpoint import resolve_task_candidates
            llm_candidates = resolve_task_candidates(owner=owner)
            llm_available = bool(llm_candidates)

            # Pull user memories so the LLM has personal context (relationships,
            # job, hobbies). Helps it know e.g. "<name> is your spouse" so their
            # events are personal/social, not work.
            _memory_context = ""
            try:
                from core.database import Memory as _Mem
                _mems = db.query(_Mem).filter(_Mem.owner == owner).limit(60).all() if owner else []
                _lines = _memory_context_lines(_mems)
                if _lines:
                    _memory_context = "USER CONTEXT (relationships, work, life):\n" + "\n".join(_lines) + "\n\n"
            except Exception as _me:
                logger.warning(f"Could not load memory for classify: {_me}")

            classified_h = 0
            classified_llm = 0
            failed = 0
            unchanged = 0
            # Pass 1: heuristic for obvious cases, collect ambiguous for LLM batch
            llm_queue = []  # list of CalendarEvent objects needing LLM
            for ev in events:
                if ev.event_type and ev.importance and ev.importance != "normal":
                    unchanged += 1
                    continue
                etype, importance = _classify_event_heuristic(ev.summary or "")
                if etype and importance:
                    ev.event_type = etype
                    ev.color = _TYPE_COLORS.get(etype)
                    ev.importance = importance
                    classified_h += 1
                    continue
                # Apply partial heuristic; queue for LLM to fill missing
                if etype:
                    ev.event_type = etype
                    ev.color = _TYPE_COLORS.get(etype)
                if llm_available:
                    llm_queue.append(ev)
                elif etype:
                    classified_h += 1
            # Persist heuristic results before LLM pass (in case LLM is slow/unavailable)
            try:
                db.commit()
            except Exception:
                pass

            # Pass 2: batch LLM classification (10 events per call)
            BATCH = 10
            for i in range(0, len(llm_queue), BATCH):
                batch = llm_queue[i:i+BATCH]
                items = [
                    {"i": idx, "title": (ev.summary or "")[:120],
                     "when": ev.dtstart.isoformat() if ev.dtstart else "",
                     "loc": (ev.location or "")[:80]}
                    for idx, ev in enumerate(batch)
                ]
                prompt = (
                    _memory_context +
                    "Classify these calendar events using the USER CONTEXT above (people they know, "
                    "their job, hobbies). Return ONLY a raw JSON array, no prose, no markdown.\n"
                    "Each item: {\"i\": <index>, \"type\": \"work|personal|health|travel|meal|social|admin|other\", "
                    "\"importance\": \"low|normal|high|critical\"}\n\n"
                    "Type guidance:\n"
                    "- personal = family, partner, kids, pets, errands, home stuff\n"
                    "- social = friends, parties, birthdays, hangouts\n"
                    "- work = the user's own job/career commitments only (not their partner's)\n"
                    "- health = doctor, gym, therapy\n"
                    "- travel = flights, trips, hotels\n"
                    "- meal = lunch/dinner/coffee specifically\n"
                    "- admin = bills, taxes, paperwork\n"
                    "- other = anything else\n\n"
                    "Importance guide: critical = surgery/court/wedding day; high = flight/interview/big presentation/exam; "
                    "normal = regular meetings/appointments; low = recurring routine.\n\n"
                    f"EVENTS: {_json.dumps(items)}"
                )
                try:
                    await wait_for_interactive_quiet("calendar classification action")
                    raw = await llm_call_async_with_fallback(
                        llm_candidates,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1, max_tokens=16384,
                        timeout=180,
                    )
                    from src.text_helpers import strip_think as _st
                    raw = _st(raw or "", prose=False, prompt_echo=False)
                    raw = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=_re.MULTILINE).strip()
                    m = _re.search(r"\[.*\]", raw, _re.DOTALL)
                    if not m:
                        logger.warning(f"[classify-llm] no JSON array in response: {raw[:300]!r}")
                        failed += len(batch)
                        continue
                    arr = _json.loads(m.group())
                    by_idx = {x.get("i"): x for x in arr if isinstance(x, dict)}
                    for idx, ev in enumerate(batch):
                        x = by_idx.get(idx)
                        if not x:
                            failed += 1
                            continue
                        t = (x.get("type") or "other").lower()
                        imp = (x.get("importance") or "normal").lower()
                        if t in _TYPE_COLORS:
                            ev.event_type = t
                            ev.color = _TYPE_COLORS[t]
                        if imp in ("low", "normal", "high", "critical"):
                            ev.importance = imp
                        classified_llm += 1
                        logger.info(f"[classify-llm] '{ev.summary}' → type={t} importance={imp}")
                except Exception as e:
                    logger.warning(f"[classify-llm] batch failed: {e}")
                    failed += len(batch)
                # Commit after each batch so partial progress persists
                try:
                    db.commit()
                except Exception as ce:
                    logger.warning(f"[classify-llm] commit failed: {ce}")
            # Final commit covers heuristic-only updates from pass 1
            db.commit()
            parts = [f"Scanned {len(events)} upcoming event(s)"]
            if classified_h:
                parts.append(f"{classified_h} via heuristic")
            if classified_llm:
                parts.append(f"{classified_llm} via LLM")
            if unchanged:
                parts.append(f"{unchanged} already set (skipped)")
            if failed:
                parts.append(f"{failed} LLM failed")
            return " · ".join(parts), True
        finally:
            db.close()
    except Exception as e:
        logger.error(f"classify_events action failed: {e}")
        return str(e), False


async def action_ping_events(owner: str, **kwargs) -> Tuple[str, bool]:
    """Calendar event reminders are now dispatched by Notes."""
    raise TaskNoop("calendar event reminders are handled by Notes")


async def action_extract_email_events(owner: str, **kwargs) -> Tuple[str, bool]:
    """Scan recent emails for booking confirmations / meetings / events
    and auto-add them to the calendar."""
    import asyncio as _aio
    try:
        from routes.email_pollers import _run_auto_summarize_once
        account_id = _email_task_account_id(kwargs)
        attempts = [
            ("3d window, 3 emails", 3, 3, 240),
            ("3d window, 2 emails", 3, 2, 150),
            ("1d window, 1 email", 1, 1, 90),
        ]
        timed_out = []
        last_result = ""
        for label, days_back, max_process, timeout in attempts:
            try:
                result = await _aio.wait_for(
                    _run_auto_summarize_once(
                        do_summary=False,
                        do_reply=False,
                        do_calendar=True,
                        days_back=days_back,
                        account_id=account_id,
                        max_process=max_process,
                    ),
                    timeout=timeout,
                )
                last_result = result or ""
                if _result_is_config_error(result):
                    return f"{result} ({label})", False
                if _result_has_work(result):
                    suffix = f"{label}" if not timed_out else f"{label}; retried after timeout"
                    return f"{result} ({suffix})", True
                raise TaskNoop(f"email→calendar: {result or 'no new emails'} ({label})")
            except _aio.TimeoutError:
                timed_out.append(label)
                logger.warning(f"email calendar extraction timed out for {label}; retrying smaller batch")
                continue
        if timed_out:
            raise TaskNoop(
                "email→calendar: calendar extraction timed out on smaller batches; "
                "will retry on the next scheduled run"
            )
        raise TaskNoop(f"email→calendar: {last_result or 'no new emails'}")
    except Exception as e:
        logger.error(f"extract_email_events action failed: {e}")
        return str(e), False



# Sender local-parts (matched exactly or by prefix) whose mail never carries a
# personal signature worth learning. These compare against the local-part
# (before "@"), so role names must NOT include a trailing "@" — "support@" etc.
# could never match a local-part of "support" and were silently dead.
_SIG_SKIP_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "notifications", "notification", "bounce",
    "newsletter", "support", "info", "admin",
)


async def action_learn_sender_signatures(owner: str, **kwargs) -> Tuple[str, bool]:
    """For each sender with ≥3 recent inbox emails, ask the LLM to extract
    the common signature block across their messages. The cached sig is
    served on the `/read` endpoint so the renderer can fold signatures
    consistently from that address (no more heuristic regex juggling).
    Caps at 20 senders per pass; re-runs after 30 days per sender."""
    try:
        import sqlite3 as _sql3
        import re as _re
        import email as _email_mod
        import asyncio as _aio
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from routes.email_helpers import _email_cache_owner_clause, _imap_connect, SCHEDULED_DB
        from src.llm_core import llm_call_async_with_fallback

        # 1. Pull recent UIDs + From headers cheaply (header-only fetch).
        def _pull_headers():
            results = []
            conn = _imap_connect(None, owner=owner)
            try:
                conn.select("INBOX", readonly=True)
                status, data = conn.uid("SEARCH", None, "ALL")
                if status != "OK" or not data or not data[0]:
                    return results
                uids = data[0].split()[-300:][::-1]  # newest 300
                for uid in uids:
                    try:
                        st, msg_data = conn.uid(
                            "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])"
                        )
                        if st != "OK" or not msg_data or not msg_data[0]:
                            continue
                        raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                        if not raw:
                            continue
                        msg = _email_mod.message_from_bytes(raw)
                        from_raw = msg.get("From", "")
                        from_addr = _email_mod.utils.parseaddr(from_raw)[1].lower().strip()
                        if not from_addr or "@" not in from_addr:
                            continue
                        results.append({
                            "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                            "from_address": from_addr,
                        })
                    except Exception:
                        continue
            finally:
                try: conn.logout()
                except Exception: pass
            return results

        mails = await _aio.to_thread(_pull_headers)
        if not mails:
            return "No emails to scan", True

        # 2. Group by sender; drop addresses that don't carry useful sigs.
        by_sender: dict[str, list[dict]] = {}
        for m in mails:
            addr = m["from_address"]
            local = addr.split("@", 1)[0]
            if any(local == p or local.startswith(p) for p in _SIG_SKIP_PREFIXES):
                continue
            # Skip plus-aliases / list-style addresses too.
            if "+" in local or "-noreply" in addr or "-bounces" in addr:
                continue
            by_sender.setdefault(addr, []).append(m)

        # 3. Eligibility: ≥3 emails AND (no cache OR cache > 30 days old).
        try:
            conn = _sql3.connect(SCHEDULED_DB)
            owner_clause, owner_params = _email_cache_owner_clause(owner)
            cached = {
                r[0]: r[1] for r in conn.execute(
                    f"SELECT from_address, last_built_at FROM sender_signatures WHERE {owner_clause}",
                    owner_params,
                ).fetchall()
            }
            conn.close()
        except Exception:
            cached = {}

        cutoff_iso = (_dt.now(_tz.utc).replace(tzinfo=None) - _td(days=30)).isoformat()
        eligible: list[tuple[str, list[dict]]] = []
        for addr, msgs in by_sender.items():
            if len(msgs) < 3:
                continue
            if cached.get(addr, "") > cutoff_iso:
                continue
            eligible.append((addr, msgs[:5]))  # use up to last 5 emails

        if not eligible:
            return "All sender sigs already cached (or no eligible senders)", True

        from src.task_endpoint import resolve_task_candidates
        candidates = resolve_task_candidates(owner=owner)
        if not candidates:
            return "No LLM endpoint available", False
        model = candidates[0][1]

        analyzed = 0
        no_sig = 0
        for addr, msgs in eligible[:20]:  # cost cap per run

            def _fetch_bodies(_msgs):
                bodies = []
                conn2 = _imap_connect(None, owner=owner)
                try:
                    conn2.select("INBOX", readonly=True)
                    for mm in _msgs:
                        try:
                            st, data = conn2.uid("FETCH", mm["uid"], "(BODY.PEEK[TEXT])")
                            if st != "OK" or not data or not data[0]:
                                continue
                            raw = data[0][1] if isinstance(data[0], tuple) else None
                            if not raw:
                                continue
                            text = raw.decode("utf-8", errors="replace")
                            bodies.append(text[:4000])
                        except Exception:
                            continue
                finally:
                    try: conn2.logout()
                    except Exception: pass
                return bodies

            try:
                bodies = await _aio.to_thread(_fetch_bodies, msgs)
            except Exception as e:
                logger.warning(f"sig learner: fetch bodies failed for {addr}: {e}")
                continue
            if len(bodies) < 2:
                continue

            joined = "\n\n---NEXT EMAIL---\n\n".join(bodies[:5])
            prompt = (
                "You are extracting the literal common SIGNATURE block that "
                "appears at the END of multiple emails from the same sender.\n\n"
                "Return ONLY the exact signature text, verbatim, with original "
                "line breaks preserved. If there is no clear common signature "
                "block across these emails, respond with the single token: "
                "NONE\n\n"
                "INCLUDE: title, company, address, phone, email/url lines, "
                "legal disclaimer block.\n"
                "EXCLUDE: greetings ('Hi', 'Dear'), closing phrases on their "
                "own ('Best regards'), the sender's name on its own line, the "
                "body content, quoted/forwarded threads (lines starting with "
                "'>' or 'On ... wrote:' or 'From: ... Sent:').\n\n"
                f"EMAILS FROM {addr}:\n{joined}"
            )

            try:
                await wait_for_interactive_quiet("sender signature action")
                raw = await llm_call_async_with_fallback(
                    candidates,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=600,
                    timeout=60,
                )
                from src.text_helpers import strip_think as _st
                sig = _st(raw or "", prose=False, prompt_echo=False).strip()
                # Strip surrounding code fences if the LLM added them.
                sig = _re.sub(r"^```[\w]*\n?", "", sig)
                sig = _re.sub(r"\n?```\s*$", "", sig)
                sig = sig.strip()
            except Exception as e:
                logger.warning(f"sig LLM call failed for {addr}: {e}")
                continue

            # NONE sentinel or out-of-bounds → cache a NULL row so we don't
            # re-try for 30 days, then move on.
            if (
                not sig
                or sig.upper().strip().strip(".") == "NONE"
                or len(sig) < 15
                or len(sig) > 3000
            ):
                cached_sig: str | None = None
                no_sig += 1
            else:
                cached_sig = sig

            try:
                conn = _sql3.connect(SCHEDULED_DB)
                owner_value = (owner or "").strip()
                conn.execute(
                    "INSERT OR REPLACE INTO sender_signatures "
                    "(from_address, owner, signature_text, sample_count, last_built_at, model_used, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (addr, owner_value, cached_sig, len(bodies), _dt.now(_tz.utc).replace(tzinfo=None).isoformat(), model, "llm"),
                )
                conn.commit()
                conn.close()
                analyzed += 1
            except Exception as e:
                logger.warning(f"sig cache write failed for {addr}: {e}")

        return f"Learned sigs: {analyzed - no_sig} found, {no_sig} no-sig, of {len(eligible)} eligible", True
    except Exception as e:
        logger.error(f"learn_sender_signatures failed: {e}")
        return str(e), False

