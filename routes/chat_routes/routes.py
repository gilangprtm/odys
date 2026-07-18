"""Chat routes — /api/chat, /api/chat_stream, /api/inject_context, /api/search.

Extracted from chat_routes.py. All route endpoints live here; helper
functions live in .helpers.
"""

import asyncio
import json
import time
import logging
from typing import Dict, Any, AsyncGenerator, List, Optional

from fastapi import APIRouter, Request, HTTPException, Form, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from core.models import ChatMessage
from src.request_models import ChatRequest
from src.llm_core import llm_call_async, stream_llm, stream_llm_with_fallback
from src.agent_loop import stream_agent_loop
from src import agent_runs
from src.model_context import estimate_tokens
from src.chat_helpers import coerce_message_and_session
from src.session_search import search_session_messages
from src.prompt_security import untrusted_context_message
from core.exceptions import SessionNotFoundError
from src.auth_helpers import effective_user
from routes.session_routes import _verify_session_owner
from routes.document_helpers import _owner_session_filter
from core.database import SessionLocal, get_session_mode, set_session_mode
from core.database import Session as DBSession, ChatMessage as DBChatMessage
from core.database import Document as DBDocument
from core.log_safety import redact_url
from routes.research_routes import _resolve_research_endpoint
from routes.chat_helpers import (
    resolve_session_auth,
    build_chat_context,
    save_assistant_response,
    run_post_response_tasks,
    clean_thinking_for_save,
    _enforce_chat_privileges,
)
from src.action_intents import ToolIntent, classify_tool_intent as _classify_tool_intent
from src.tool_policy import (
    WEB_TOOL_NAMES,
    build_effective_tool_policy,
    is_web_search_explicitly_denied,
    web_search_enabled_for_turn,
)

from .helpers import (
    _active_streams,
    _stream_set,
    _set_user_time_from_request,
    _clear_orphaned_session_endpoint,
    _recover_empty_session_model,
    _is_contextual_web_followup,
    _ensure_current_request_is_latest_user,
    _resolve_request_workspace,
    _is_image_generation_session,
)

logger = logging.getLogger(__name__)


def setup_chat_routes(
    session_manager,
    chat_handler,
    chat_processor,
    memory_manager,
    research_handler,
    upload_handler,
    memory_vector=None,
    webhook_manager=None,
    skills_manager=None,
) -> APIRouter:
    router = APIRouter(tags=["chat"])

    # ------------------------------------------------------------------ #
    # POST /api/chat (non-streaming)
    # ------------------------------------------------------------------ #
    @router.post("/api/chat", response_model=Dict[str, str])
    async def chat_endpoint(request: Request, chat_request: ChatRequest) -> Dict[str, str]:
        _set_user_time_from_request(request)

        message = chat_request.message
        session = chat_request.session
        att_ids = chat_request.attachments or []
        use_web = chat_request.use_web
        use_research = chat_request.use_research
        time_filter = chat_request.time_filter
        preset_id = chat_request.preset_id

        # Verify the caller owns this session before loading it.
        try:
            _verify_session_owner(request, session)

            try:
                sess = session_manager.get_session(session)
            except KeyError:
                raise HTTPException(404, f"Session '{session}' not found")
            owner = effective_user(request)
            if _clear_orphaned_session_endpoint(sess, owner=owner):
                raise HTTPException(400, "Selected model endpoint was removed. Pick another model in Settings.")

            _recover_empty_session_model(sess, session, owner=owner)
            if not getattr(sess, "model", "").strip():
                raise HTTPException(
                    400,
                    "No model selected for this chat. Open the model picker and choose one before sending.",
                )

            _enforce_chat_privileges(request, sess)

            tool_policy = build_effective_tool_policy(last_user_message=message)
            allow_tool_preprocessing = not tool_policy.block_all_tool_calls

            # Inline memory command
            memory_response = None
            if not tool_policy.blocks("manage_memory"):
                memory_response = await chat_handler.handle_memory_command(sess, message)
            if memory_response:
                return {"response": memory_response}

            # Build shared context
            ctx = await build_chat_context(
                sess, request, chat_handler, chat_processor,
                message=message,
                session_id=session,
                preset_id=preset_id,
                att_ids=att_ids,
                use_web=use_web,
                time_filter=time_filter,
                webhook_manager=webhook_manager,
                allow_tool_preprocessing=allow_tool_preprocessing,
            )

            # Research injection
            research_blocked_by_policy = (
                tool_policy.blocks("trigger_research")
                or tool_policy.blocks("manage_research")
            )
            if use_research and not research_blocked_by_policy:
                try:
                    _r_ep, _r_model, _r_headers = _resolve_research_endpoint(sess)
                    research_ctx = await research_handler.call_research_service(
                        message, _r_ep, _r_model, llm_headers=_r_headers
                    )
                    ctx.messages.insert(
                        len(ctx.preface),
                        untrusted_context_message("research context", research_ctx),
                    )
                except Exception as e:
                    logger.error(f"Research failed: {e}")

            reply = await llm_call_async(
                sess.endpoint_url,
                sess.model,
                ctx.messages,
                headers=sess.headers,
                temperature=ctx.preset.temperature,
                max_tokens=ctx.preset.max_tokens,
                prompt_type=preset_id,
                session_id=session,
            )
            _clean_reply, _clean_md = clean_thinking_for_save(reply, {"model": sess.model})
            sess.add_message(ChatMessage("assistant", _clean_reply, metadata=_clean_md))

            from core.database import update_session_last_accessed
            update_session_last_accessed(session)
            session_manager.save_sessions()

            # Background tasks
            run_post_response_tasks(
                sess, session_manager, session, message, reply, None,
                ctx.uprefs, memory_manager, memory_vector, webhook_manager,
                character_name=ctx.preset.character_name,
                owner=ctx.user,
                allow_background_extraction=not tool_policy.block_all_tool_calls,
            )

            return {"response": reply}
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            raise HTTPException(500, f"Chat endpoint crash: {e}\n{tb}")

    # ------------------------------------------------------------------ #
    # POST /api/chat_stream
    # ------------------------------------------------------------------ #
    @router.post("/api/chat_stream")
    async def chat_stream(request: Request) -> StreamingResponse:
        body = None
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = await request.json()
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"Invalid JSON: {e}")
        except HTTPException:
            raise
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            raise HTTPException(500, f"Request parsing error: {e}\n{tb}")

        _set_user_time_from_request(request)

        form_data = await request.form()
        message = form_data.get("message")
        session = form_data.get("session")
        attachments = form_data.get("attachments")
        use_web = form_data.get("use_web")
        use_research = form_data.get("use_research")
        time_filter = form_data.get("time_filter")
        preset_id = form_data.get("preset_id")
        allow_bash = form_data.get("allow_bash") or (body or {}).get("allow_bash")
        allow_web_search = form_data.get("allow_web_search") or (body or {}).get("allow_web_search")
        use_rag = form_data.get("use_rag")
        search_context = form_data.get("search_context")
        compare_mode = str(form_data.get("compare_mode", "")).lower() == "true"
        incognito = str(form_data.get("incognito", "")).lower() == "true"
        plan_mode = False
        chat_mode = str(form_data.get("mode", "")).lower()
        workspace, workspace_rejected = _resolve_request_workspace(
            request, form_data.get("workspace")
        )
        if plan_mode:
            chat_mode = "agent"
        approved_plan = ""
        if not plan_mode:
            approved_plan = (form_data.get("approved_plan") or "").strip()[:8192]
        user_requested_agent = (chat_mode == "agent")
        _search_enabled = web_search_enabled_for_turn(allow_web_search, use_web)
        auto_escalated = False
        _tool_intent = _classify_tool_intent(message) if isinstance(message, str) else None
        if chat_mode == "chat" and _tool_intent and _tool_intent.needs_tools:
            chat_mode = "agent"
            auto_escalated = True
            logger.info(
                "chat→agent auto-escalation: category=%s reason=%s",
                _tool_intent.category,
                _tool_intent.reason,
            )
        elif chat_mode == "chat" and _search_enabled:
            chat_mode = "agent"
            auto_escalated = True
            logger.info("chat→agent auto-escalation: search enabled")
        active_doc_id = form_data.get("active_doc_id", "").strip()
        logger.info(f"[doc-inject] chat_mode={chat_mode}, active_doc_id={active_doc_id!r}")

        active_email_uid = form_data.get("active_email_uid", "").strip()
        active_email_folder = form_data.get("active_email_folder", "INBOX").strip() or "INBOX"
        active_email_account = form_data.get("active_email_account", "").strip()
        active_email_ctx: Optional[Dict[str, str]] = None
        try:
            from src.tool_implementations import clear_active_email
            clear_active_email()
        except Exception:
            pass
        if active_email_uid:
            active_email_ctx = {
                "uid": active_email_uid,
                "folder": active_email_folder,
                "account": active_email_account,
            }
            try:
                from routes.email_routes import _read_cache_get, _read_cache_key
                _ck = _read_cache_key(active_email_account or None, active_email_folder, active_email_uid, owner=get_current_user(request))
                _cached_email = _read_cache_get(_ck)
                if _cached_email and isinstance(_cached_email, dict):
                    active_email_ctx["subject"] = str(_cached_email.get("subject") or "")
                    active_email_ctx["from"] = str(
                        _cached_email.get("from_address")
                        or _cached_email.get("from")
                        or _cached_email.get("from_name")
                        or ""
                    )
                    _body_preview = (_cached_email.get("body") or "")[:2000]
                    if _body_preview:
                        active_email_ctx["body_preview"] = _body_preview
            except Exception as _e:
                logger.debug(f"[email-inject] cache enrich skipped: {_e}")
            try:
                from src.tool_implementations import set_active_email
                set_active_email(
                    uid=active_email_uid,
                    folder=active_email_folder,
                    account=active_email_account or None,
                    subject=active_email_ctx.get("subject"),
                    sender=active_email_ctx.get("from"),
                )
            except Exception as _e:
                logger.debug(f"[email-inject] set_active_email failed: {_e}")
            logger.info(
                "[email-inject] active_email uid=%s folder=%s account=%s subject=%r",
                active_email_uid, active_email_folder, active_email_account or "(default)",
                active_email_ctx.get("subject", ""),
            )

        try:
            _has_atts = (
                bool(body and isinstance(body.get("attachments"), list) and body["attachments"])
                or bool(form_data.get("attachments"))
            )
            message, session = coerce_message_and_session(
                body, message, session, session_manager, allow_empty=_has_atts,
            )
            _verify_session_owner(request, session)
            sess = session_manager.get_session(session)
            owner = effective_user(request)
            if _clear_orphaned_session_endpoint(sess, owner=owner):
                raise HTTPException(400, "Selected model endpoint was removed. Pick another model in Settings.")
            _recover_empty_session_model(sess, session, owner=owner)
            if not getattr(sess, "model", "").strip():
                raise HTTPException(
                    400,
                    "No model selected for this chat. Open the model picker and choose one before sending.",
                )
            if (
                chat_mode == "chat"
                and isinstance(message, str)
                and (not _tool_intent or not _tool_intent.needs_tools)
                and _is_contextual_web_followup(message, sess)
            ):
                _tool_intent = ToolIntent(True, "web", "contextual web lookup follow-up")
                chat_mode = "agent"
                auto_escalated = True
                logger.info(
                    "chat→agent auto-escalation: category=%s reason=%s",
                    _tool_intent.category,
                    _tool_intent.reason,
                )
        except SessionNotFoundError as e:
            raise HTTPException(404, str(e))
        except (ValueError, ValidationError):
            raise HTTPException(400, "Invalid request parameters")

        _enforce_chat_privileges(request, sess)

        resolve_session_auth(sess, session, owner=effective_user(request))

        do_research = str(use_research).lower() == "true"
        if not do_research:
            if get_session_mode(session) == 'research_pending':
                do_research = True
                logger.info(f"Session {session} in research_pending — auto-triggering research")

        att_ids = []
        if body and isinstance(body.get("attachments"), list):
            att_ids = [str(x) for x in body["attachments"]]
        elif attachments:
            try:
                att_ids = [str(x) for x in json.loads(attachments)]
            except Exception as e:
                logger.warning("Failed to parse attachments JSON, ignoring attachments", exc_info=e)

        no_memory = str(form_data.get("no_memory", "")).lower() == "true"
        pre_context_tool_policy = build_effective_tool_policy(
            last_user_message=message,
        )
        allow_tool_preprocessing = not pre_context_tool_policy.block_all_tool_calls

        ctx = await build_chat_context(
            sess, request, chat_handler, chat_processor,
            message=message,
            session_id=session,
            preset_id=preset_id,
            att_ids=att_ids,
            use_web=use_web,
            use_rag=use_rag,
            time_filter=time_filter,
            incognito=incognito,
            no_memory=no_memory,
            search_context=search_context,
            compare_mode=compare_mode,
            webhook_manager=webhook_manager,
            use_enhanced_message=True,
            agent_mode=(chat_mode == "agent"),
            allow_tool_preprocessing=allow_tool_preprocessing,
        )

        _research_flags = {"do": do_research}

        # Query active document
        active_doc = None
        _doc_db = SessionLocal()
        try:
            if active_doc_id:
                logger.info(f"[doc-inject] active_doc_id from frontend: {active_doc_id}")
                _doc_q = _doc_db.query(DBDocument).filter(DBDocument.id == active_doc_id)
                active_doc = _owner_session_filter(_doc_q, ctx.user).first()
                if active_doc:
                    doc_session = active_doc.session_id
                    doc_owner = getattr(active_doc, "owner", None)
                    if doc_owner and ctx.user and doc_owner != ctx.user:
                        logger.warning(
                            "[doc-inject] ignoring active_doc_id %s owned by another user",
                            active_doc_id,
                        )
                        active_doc = None
                    else:
                        if doc_session and doc_session != session:
                            logger.info(
                                "[doc-inject] cross-session active_doc_id %s (was session %s, now %s) — accepting and rebinding",
                                active_doc_id, doc_session, session,
                            )
                            try:
                                active_doc.session_id = session
                                _doc_db.commit()
                            except Exception as _e:
                                _doc_db.rollback()
                                logger.warning(f"[doc-inject] session rebind failed: {_e}")
                            logger.info("[doc-inject] found by ID: title=%r, lang=%r, is_active=%s, content_len=%d", active_doc.title, active_doc.language, active_doc.is_active, len(active_doc.current_content or ''))
                else:
                    logger.warning("[doc-inject] NOT FOUND by ID %s", active_doc_id)
            if not active_doc:
                _email_doc_q = _doc_db.query(DBDocument).filter(
                    DBDocument.session_id == session,
                    DBDocument.is_active == True,
                    DBDocument.language == "email",
                )
                active_doc = _owner_session_filter(_email_doc_q, ctx.user).order_by(DBDocument.updated_at.desc()).first()
                if active_doc:
                    logger.info(f"[doc-inject] found email draft by session fallback: title={active_doc.title!r}")
            if not active_doc:
                _session_doc_q = _doc_db.query(DBDocument).filter(
                    DBDocument.session_id == session,
                    DBDocument.is_active == True
                )
                active_doc = _owner_session_filter(_session_doc_q, ctx.user).order_by(DBDocument.updated_at.desc()).first()
                if active_doc:
                    logger.info(f"[doc-inject] found by session fallback: title={active_doc.title!r}")
            if not active_doc:
                try:
                    from src.agent_tools.document_tools import get_active_document
                    _mem_id = get_active_document()
                    if _mem_id:
                        _mem_q = _doc_db.query(DBDocument).filter(DBDocument.id == _mem_id)
                        cand = _owner_session_filter(_mem_q, ctx.user).first()
                        if cand and (not cand.session_id or cand.session_id == session):
                            active_doc = cand
                            logger.info(f"[doc-inject] found by in-memory active id: title={active_doc.title!r} (session_id={cand.session_id!r})")
                except Exception as _e:
                    logger.debug(f"[doc-inject] in-memory fallback failed: {_e}")
            if not active_doc:
                logger.info(f"[doc-inject] no active doc for session {session}")
            if active_doc:
                _doc_db.expunge(active_doc)
        except Exception as e:
            logger.warning(f"Failed to query active document: {e}")
        finally:
            _doc_db.close()

        # Build disabled-tools set
        disabled_tools = set()
        if allow_bash is not None and str(allow_bash).lower() != "true":
            disabled_tools.add("bash")
        _explicit_web_intent = bool(_tool_intent and _tool_intent.category == "web")
        if is_web_search_explicitly_denied(allow_web_search) or not _search_enabled:
            disabled_tools.update(WEB_TOOL_NAMES)
        if _explicit_web_intent:
            disabled_tools.update({
                "bash", "python",
                "search_chats", "manage_skills", "manage_memory",
                "read_file", "write_file", "edit_file",
                "create_document", "edit_document", "update_document",
                "send_email", "reply_to_email",
                "manage_notes", "manage_calendar", "manage_tasks",
                "api_call", "builtin_browser",
            })
            if _search_enabled:
                disabled_tools.difference_update(WEB_TOOL_NAMES)
            else:
                disabled_tools.update(WEB_TOOL_NAMES)
        elif _search_enabled:
            disabled_tools.difference_update(WEB_TOOL_NAMES)

        if incognito:
            disabled_tools.update({
                "manage_memory",
                "search_chats",
                "manage_skills",
            })

        if active_email_ctx and active_email_ctx.get("uid"):
            disabled_tools.update({
                "create_document",
                "send_email",
                "reply_to_email",
                "mcp__email__send_email",
                "mcp__email__reply_to_email",
            })

        # Enforce per-user privileges
        _privs = {}
        _user = ctx.user
        if _user and hasattr(request.app.state, 'auth_manager') and request.app.state.auth_manager:
            _privs = request.app.state.auth_manager.get_privileges(_user)
        if _privs:
            if not _privs.get("can_use_bash", True):
                disabled_tools.update({"bash", "python", "read_file", "write_file"})
            if not _privs.get("can_use_browser", True):
                disabled_tools.add("builtin_browser")
            if not _privs.get("can_use_documents", True):
                disabled_tools.update({"create_document", "edit_document", "update_document", "suggest_document"})
            if not _privs.get("can_generate_images", True):
                disabled_tools.add("generate_image")
            if not _privs.get("can_manage_memory", True):
                disabled_tools.update({"manage_memory", "manage_skills"})
            if not _privs.get("can_use_research", True):
                _research_flags["do"] = False
            if not _privs.get("can_use_agent", True):
                chat_mode = 'chat'
        from src.settings import get_setting
        _global_disabled = get_setting("disabled_tools", [])
        if _global_disabled and isinstance(_global_disabled, list):
            disabled_tools.update(_global_disabled)

        if auto_escalated:
            disabled_tools.update({
                "bash", "python", "read_file", "write_file", "builtin_browser",
            })

        if sess.name and sess.name.startswith("[CMP]"):
            disabled_tools.update({"create_document", "edit_document", "update_document"})

        if compare_mode:
            _compare_strip = {
                "create_document", "edit_document", "update_document",
                "chat_with_model", "create_session", "list_sessions",
                "send_to_session",
                "pipeline", "manage_session", "manage_memory", "list_models",
                "generate_image", "ui_control",
            }
            disabled_tools.update(_compare_strip)
            if chat_mode == 'chat':
                disabled_tools.update({"bash", "python", "read_file", "write_file", "web_search", "web_fetch", "search_chats", "manage_tasks"})

        if plan_mode:
            from src.tool_security import plan_mode_disabled_tools
            disabled_tools.update(plan_mode_disabled_tools())

        tool_policy = build_effective_tool_policy(
            disabled_tools=disabled_tools,
            last_user_message=message,
        )
        disabled_tools = tool_policy.all_disabled_names()
        research_blocked_by_policy = bool(
            tool_policy.blocks("trigger_research")
            or tool_policy.blocks("manage_research")
        )
        effective_do_research = bool(
            do_research and _research_flags["do"] and not research_blocked_by_policy
        )

        _effective_mode = 'research' if effective_do_research else (chat_mode or 'chat')
        if _effective_mode in ('agent', 'research', 'chat'):
            set_session_mode(session, _effective_mode)

        async def stream_with_save() -> AsyncGenerator[str, None]:
            research_sources = None
            web_sources = ctx.web_sources

            _active_streams[session] = {"status": "streaming", "partial": "", "query": message, "is_research": effective_do_research, "mode": _effective_mode}

            if workspace_rejected:
                yield f"data: {json.dumps({'type': 'workspace_rejected', 'data': {'path': workspace_rejected}})}\n\n"

            if ctx.preprocessed.attachment_meta:
                yield f"data: {json.dumps({'type': 'attachments', 'data': ctx.preprocessed.attachment_meta})}\n\n"

            for _opened in ctx.auto_opened_docs:
                yield (
                    f'data: {json.dumps({"type": "doc_update", **_opened})}\n\n'
                )

            if ctx.rag_sources:
                yield f"data: {json.dumps({'type': 'rag_sources', 'data': ctx.rag_sources})}\n\n"

            if web_sources:
                yield f"data: {json.dumps({'type': 'web_sources', 'data': web_sources})}\n\n"

            if ctx.used_memories:
                yield f"data: {json.dumps({'type': 'memories_used', 'data': ctx.used_memories})}\n\n"

            if effective_do_research:
                _r_ep, _r_model, _r_headers = _resolve_research_endpoint(sess)
                _auth_keys = list(_r_headers.keys()) if _r_headers else []
                logger.info(f"Research endpoint resolved: model={_r_model}, endpoint={redact_url(_r_ep)}, auth_keys={_auth_keys}, sess_headers_keys={list(sess.headers.keys()) if isinstance(sess.headers, dict) else type(sess.headers)}")

                _prior_json = research_handler._get_session_json(session)
                _history_len = len(sess.history) if hasattr(sess, 'history') else 0
                _is_first_research = not _prior_json and _history_len <= 2 and not compare_mode

                if _is_first_research:
                    logger.info(f"First research message — asking clarifying questions for: {message[:60]}")
                    yield f'data: {json.dumps({"type": "model_info", "model": sess.model, "suffix": "Research"})}\n\n'
                    set_session_mode(session, "research_pending")
                    ctx.messages.insert(0, {"role": "system", "content":
                        "The user wants to start deep web research. Before searching, ask 2-3 brief "
                        "clarifying questions to understand exactly what they want to know. For example: "
                        "what aspects matter most, are they comparing to something, what's their context "
                        "(moving, traveling, curiosity). Be conversational. Keep it short."
                    })
                    _skip_research = True
                else:
                    _skip_research = False

                if not _skip_research:
                    def _on_research_done(_sid, _result, _sources, _findings):
                        if incognito:
                            return
                        try:
                            _s = session_manager.get_session(_sid)
                            if not _s:
                                logger.warning(f"Session {_sid} expired before research completed")
                                return
                            _md = {"research": True, "model": _s.model}
                            if _sources:
                                _md["research_sources"] = _sources
                            if _findings:
                                _md["research_findings"] = _findings
                            _clean_res, _md = clean_thinking_for_save(_result, _md)
                            _s.add_message(ChatMessage("assistant", _clean_res, metadata=_md))
                            session_manager.save_sessions()
                            logger.info(f"Research result persisted to DB for session {_sid}")
                        except Exception as _e:
                            logger.error(f"Failed to persist research to DB: {_e}")

                    _prior_report = ""
                    _prior_findings = None
                    _prior_urls = None
                    _prior_json = research_handler._get_session_json(session)
                    if _prior_json:
                        _prior_report = _prior_json.get("raw_report", "")
                        _prior_findings = _prior_json.get("raw_findings")
                        _src_urls = {s.get("url", "") for s in (_prior_json.get("sources") or []) if s.get("url")}
                        _prior_urls = _src_urls if _src_urls else None
                        if _prior_report:
                            logger.info(f"Continuing research for session {session} with {len(_src_urls)} prior URLs")

                    _research_query = await research_handler.synthesize_query(
                        sess, message, _r_ep, _r_model, _r_headers,
                    )
                    logger.info(f"Research query: {_research_query[:120]}")

                    research_handler.start_research(
                        session, _research_query, _r_ep, _r_model,
                        llm_headers=_r_headers,
                        prior_report=_prior_report,
                        prior_findings=_prior_findings,
                        prior_urls=_prior_urls,
                        on_complete=_on_research_done,
                        owner=_user,
                    )

                    _heartbeat_counter = 0
                    _last_progress = {}
                    _sent_avg = False
                    while True:
                        status = research_handler.get_status(session)
                        if not status or status["status"] != "running":
                            break
                        progress = status.get("progress", {})
                        if progress and progress != _last_progress:
                            _last_progress = progress
                            if not _sent_avg:
                                _sent_avg = True
                                progress = dict(progress)
                                progress["started_at"] = status.get("started_at")
                                avg = status.get("avg_duration")
                                if avg:
                                    progress["avg_duration"] = avg
                            yield f"data: {json.dumps({'type': 'research_progress', 'data': progress})}\n\n"
                            _heartbeat_counter = 0
                        else:
                            _heartbeat_counter += 1
                            yield f": heartbeat {_heartbeat_counter}\n\n"
                        await asyncio.sleep(1.0)

                    research_sources = research_handler.get_sources(session)
                    if research_sources:
                        yield f"data: {json.dumps({'type': 'research_sources', 'data': research_sources})}\n\n"

                    research_findings = research_handler.get_raw_findings(session)
                    if research_findings:
                        yield f"data: {json.dumps({'type': 'research_findings', 'data': research_findings})}\n\n"

                    yield f"data: {json.dumps({'type': 'research_done', 'data': {'session_id': session}})}\n\n"
                    yield "data: [DONE]\n\n"
                    research_handler.clear_result(session)
                    _stream_set(session, status="done")
                    _active_streams.pop(session, None)
                    return

            messages = _ensure_current_request_is_latest_user(ctx.messages, message)

            if ctx.was_compacted:
                yield f"data: {json.dumps({'type': 'compacted', 'context_length': ctx.context_length})}\n\n"
            if ctx.context_trimmed and not ctx.was_compacted:
                yield f"data: {json.dumps({'type': 'context_trimmed', 'data': {'context_length': ctx.context_length, 'messages_before': ctx.context_messages_before_trim, 'messages_after': ctx.context_messages_after_trim, 'tokens_before': ctx.context_tokens_before_trim, 'tokens_after': ctx.context_tokens_after_trim}})}\n\n"

            full_response = ""
            thinking_response = ""
            last_metrics = None

            try:
                from src.endpoint_resolver import resolve_chat_fallback_candidates
                _fallback_candidates = resolve_chat_fallback_candidates(owner=_user)
            except Exception:
                _fallback_candidates = []

            _model_suffix = "Research" if effective_do_research else None
            _model_info = {"type": "model_info", "model": sess.model}
            if _model_suffix:
                _model_info["suffix"] = _model_suffix
            if ctx.preset.character_name:
                _model_info["character_name"] = ctx.preset.character_name
            yield f'data: {json.dumps(_model_info)}\n\n'

            if _is_image_generation_session(sess, owner=_user):
                from src.settings import get_setting
                if tool_policy.blocks("generate_image"):
                    _blocked_msg = tool_policy.reason_for("generate_image")
                    yield f'data: {json.dumps({"delta": _blocked_msg})}\n\n'
                    yield "data: [DONE]\n\n"
                    _active_streams.pop(session, None)
                    return
                if not get_setting("image_gen_enabled", True):
                    yield f'data: {json.dumps({"delta": "Image generation is disabled by the administrator."})}\n\n'
                    yield "data: [DONE]\n\n"
                    _active_streams.pop(session, None)
                    return
                from src.ai_interaction import do_generate_image
                _user_msg = message or ""
                yield f'data: {json.dumps({"type": "tool_start", "tool": "generate_image", "command": _user_msg[:100]})}\n\n'
                yield ": heartbeat\n\n"
                _img_result = await do_generate_image(f"{_user_msg}\n{sess.model}", session, owner=_user)
                _img_output = _img_result.get("results", _img_result.get("error", ""))
                _img_tool_data = {"type": "tool_output", "tool": "generate_image", "command": _user_msg[:100], "output": _img_output, "exit_code": 0 if "error" not in _img_result else 1}
                for _k in ("image_url", "image_id", "image_prompt", "image_model", "image_size", "image_quality"):
                    if _k in _img_result:
                        _img_tool_data[_k] = _img_result[_k]
                yield f'data: {json.dumps(_img_tool_data)}\n\n'
                _desc = _img_result.get("results", _img_result.get("error", "Image generation complete"))
                full_response = _desc
                yield f'data: {json.dumps({"delta": _desc})}\n\n'
                if not incognito:
                    _ev = {"round": 1, "tool": "generate_image", "command": _user_msg[:100], "output": _img_output, "exit_code": 0 if "error" not in _img_result else 1}
                    for _ek in ("image_url", "image_id", "image_prompt", "image_model", "image_size", "image_quality"):
                        if _img_result.get(_ek):
                            _ev[_ek] = _img_result[_ek]
                    sess.add_message(ChatMessage("assistant", full_response, metadata={"tool_events": [_ev], "model": sess.model}))
                    session_manager.save_sessions()
                yield f'data: {json.dumps({"type": "metrics", "data": {"total_time": 0}})}\n\n'
                yield "data: [DONE]\n\n"
                _active_streams.pop(session, None)
                return
            elif chat_mode == "chat":
                _chat_start = time.time()
                _answered_by = None
                _requested_model = sess.model
                _actual_model = None
                try:
                    _chat_candidates = [(sess.endpoint_url, sess.model, sess.headers)] + _fallback_candidates
                    async for chunk in stream_llm_with_fallback(
                        _chat_candidates,
                        messages,
                        temperature=ctx.preset.temperature,
                        max_tokens=ctx.preset.max_tokens,
                        prompt_type=preset_id,
                        tools=None,
                        session_id=session,
                    ):
                        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                            try:
                                data = json.loads(chunk[6:])
                                if "delta" in data:
                                    if data.get("thinking"):
                                        thinking_response += data["delta"]
                                    else:
                                        full_response += data["delta"]
                                        _stream_set(session, partial=full_response)
                                    yield chunk
                                elif data.get("type") == "fallback":
                                    _answered_by = data.get("answered_by") or _answered_by
                                    _actual_model = _actual_model or _answered_by
                                    data["selected_model"] = data.get("selected_model") or _requested_model
                                    yield chunk
                                elif data.get("type") == "model_actual":
                                    _actual_model = data.get("model") or _actual_model
                                    data["requested_model"] = _requested_model
                                    yield f'data: {json.dumps(data)}\n\n'
                                elif data.get("type") == "usage":
                                    last_metrics = data.get("data", {})
                                    _reported_model = last_metrics.get("model")
                                    last_metrics["requested_model"] = _requested_model
                                    last_metrics["model"] = _reported_model or _actual_model or _answered_by or _requested_model
                                    if ctx.context_trimmed:
                                        last_metrics["context_trimmed"] = True
                                        last_metrics["context_messages_before_trim"] = ctx.context_messages_before_trim
                                        last_metrics["context_messages_after_trim"] = ctx.context_messages_after_trim
                                        last_metrics["context_tokens_before_trim"] = ctx.context_tokens_before_trim
                                        last_metrics["context_tokens_after_trim"] = ctx.context_tokens_after_trim
                                    if ctx.context_length and last_metrics.get("input_tokens"):
                                        pct = min(round((last_metrics["input_tokens"] / ctx.context_length) * 100, 1), 100.0)
                                        last_metrics["context_percent"] = pct
                                        last_metrics["context_length"] = ctx.context_length
                                    if last_metrics.get("gen_tps") and not last_metrics.get("tokens_per_second"):
                                        last_metrics["tokens_per_second"] = last_metrics["gen_tps"]
                                        last_metrics["tps_source"] = "backend"
                                    last_metrics.setdefault("response_time", round(time.time() - _chat_start, 2))
                                    yield f'data: {json.dumps({"type": "metrics", "data": last_metrics})}\n\n'
                            except json.JSONDecodeError:
                                yield chunk
                        elif chunk.startswith("event: error"):
                            logger.warning(f"Stream error for {sess.model} on {sess.endpoint_url}: {chunk!r}")
                            yield chunk
                        elif chunk.startswith("event: "):
                            yield chunk
                        elif chunk == "data: [DONE]\n\n":
                            if not last_metrics and full_response:
                                _elapsed = time.time() - _chat_start
                                _est_in = estimate_tokens(messages)
                                _est_out = len(full_response) // 4
                                _tps = round(_est_out / _elapsed, 2) if _elapsed > 0 else 0
                                _ctx_pct = min(round((_est_in / ctx.context_length) * 100, 1), 100.0) if ctx.context_length else 0
                                last_metrics = {
                                    "response_time": round(_elapsed, 2),
                                    "input_tokens": _est_in,
                                    "output_tokens": _est_out,
                                    "tokens_per_second": _tps,
                                    "context_percent": _ctx_pct,
                                    "context_length": ctx.context_length,
                                    "model": _actual_model or _answered_by or _requested_model,
                                    "requested_model": _requested_model,
                                    "usage_source": "estimated",
                                }
                                yield f'data: {json.dumps({"type": "metrics", "data": last_metrics})}\n\n'
                            if full_response:
                                _metrics_to_save = dict(last_metrics or {})
                                if thinking_response.strip() and not _metrics_to_save.get("thinking"):
                                    _metrics_to_save["thinking"] = thinking_response.strip()
                                _saved_id = save_assistant_response(
                                    sess, session_manager, session, full_response, _metrics_to_save,
                                    character_name=ctx.preset.character_name,
                                    web_sources=web_sources,
                                    rag_sources=ctx.rag_sources,
                                    research_sources=research_sources,
                                    used_memories=ctx.used_memories,
                                    do_research=effective_do_research,
                                    incognito=incognito,
                                )
                                if _saved_id:
                                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved_id})}\n\n'
                                run_post_response_tasks(
                                    sess, session_manager, session, message, full_response,
                                    _metrics_to_save, ctx.uprefs, memory_manager, memory_vector, webhook_manager,
                                    incognito=incognito, compare_mode=compare_mode,
                                    character_name=ctx.preset.character_name,
                                    owner=_user,
                                    allow_background_extraction=not tool_policy.block_all_tool_calls,
                                )
                            _stream_set(session, status="done")
                            yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    if full_response:
                        logger.info("Client disconnected mid-stream (chat mode) for session %s, saving partial (%d chars)", session, len(full_response))
                        _stopped_content, _stopped_md = clean_thinking_for_save(
                            full_response,
                            {
                                "stopped": True,
                                "model": _actual_model or _answered_by or _requested_model,
                                "requested_model": _requested_model,
                            },
                        )
                        sess.add_message(ChatMessage("assistant", _stopped_content, metadata=_stopped_md))
                        if not incognito:
                            session_manager.save_sessions()
                    raise
                finally:
                    _active_streams.pop(session, None)
            else:
                # Agent mode: full agent loop with tools
                _agent_rounds = 0
                _agent_tool_calls = 0
                _answered_by = None
                _requested_model = sess.model
                _actual_model = None
                try:
                    from src.settings import get_setting
                    from src.agent_tools import MAX_AGENT_ROUNDS as _DEFAULT_ROUNDS
                    try:
                        _tool_budget = int(get_setting("agent_max_tool_calls", 0))
                    except (TypeError, ValueError):
                        _tool_budget = 0
                    try:
                        _max_rounds = int(get_setting("agent_max_rounds", _DEFAULT_ROUNDS) or _DEFAULT_ROUNDS)
                    except (TypeError, ValueError):
                        _max_rounds = _DEFAULT_ROUNDS
                    _max_rounds = max(1, min(_max_rounds, 200))

                    _forced_tools = None
                    if _search_enabled:
                        _forced_tools = set(WEB_TOOL_NAMES)

                    async for chunk in stream_agent_loop(
                        sess.endpoint_url,
                        sess.model,
                        messages,
                        headers=sess.headers,
                        temperature=ctx.preset.temperature,
                        max_tokens=ctx.preset.max_tokens,
                        prompt_type=preset_id,
                        max_tool_calls=_tool_budget,
                        max_rounds=_max_rounds,
                        context_length=ctx.context_length,
                        active_document=active_doc,
                        active_email=active_email_ctx,
                        session_id=session,
                        disabled_tools=disabled_tools if disabled_tools else None,
                        tool_policy=tool_policy,
                        owner=_user,
                        fallbacks=_fallback_candidates,
                        plan_mode=plan_mode,
                        approved_plan=approved_plan or None,
                        workspace=workspace or None,
                        forced_tools=_forced_tools,
                        uploaded_files=ctx.uploaded_files,
                    ):
                        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                            try:
                                data = json.loads(chunk[6:])
                                if "delta" in data:
                                    if data.get("thinking"):
                                        thinking_response += data["delta"]
                                    else:
                                        full_response += data["delta"]
                                        _stream_set(session, partial=full_response)
                                    yield chunk
                                elif data.get("type") == "web_sources":
                                    web_sources = data.get("data", [])
                                    yield chunk
                                elif data.get("type") in (
                                    "tool_start", "tool_output", "agent_step",
                                    "doc_stream_open", "doc_stream_delta",
                                    "doc_update", "doc_suggestions", "ui_control",
                                    "rounds_exhausted", "budget_exceeded",
                                    "loop_breaker_triggered",
                                    "intent_nudge_exhausted",
                                    "ask_user",
                                    "plan_update",
                                ):
                                    if data.get("type") == "agent_step":
                                        _agent_rounds = max(_agent_rounds, data.get("round", 1))
                                    elif data.get("type") == "tool_start":
                                        _agent_tool_calls += 1
                                    yield chunk
                                elif data.get("type") == "fallback":
                                    _answered_by = data.get("answered_by") or _answered_by
                                    _actual_model = _actual_model or _answered_by
                                    data["selected_model"] = data.get("selected_model") or _requested_model
                                    yield chunk
                                elif data.get("type") == "model_actual":
                                    _actual_model = data.get("model") or _actual_model
                                    data["requested_model"] = _requested_model
                                    yield f'data: {json.dumps(data)}\n\n'
                                elif data.get("type") == "metrics":
                                    last_metrics = data.get("data", {})
                                    _reported_model = last_metrics.get("model")
                                    last_metrics["requested_model"] = last_metrics.get("requested_model") or _requested_model
                                    last_metrics["model"] = _reported_model or _actual_model or _answered_by or _requested_model
                                    if ctx.context_trimmed:
                                        last_metrics["context_trimmed"] = True
                                        last_metrics["context_messages_before_trim"] = ctx.context_messages_before_trim
                                        last_metrics["context_messages_after_trim"] = ctx.context_messages_after_trim
                                        last_metrics["context_tokens_before_trim"] = ctx.context_tokens_before_trim
                                        last_metrics["context_tokens_after_trim"] = ctx.context_tokens_after_trim
                                    yield f'data: {json.dumps({"type": "metrics", "data": last_metrics})}\n\n'
                            except json.JSONDecodeError:
                                yield chunk
                        elif chunk.startswith("event: "):
                            yield chunk
                        elif chunk == "data: [DONE]\n\n":
                            _has_tool_events = bool((last_metrics or {}).get("tool_events"))
                            if full_response or _has_tool_events:
                                _response_to_save = full_response or "Done."
                                _metrics_to_save = dict(last_metrics or {})
                                if thinking_response.strip() and not _metrics_to_save.get("thinking"):
                                    _metrics_to_save["thinking"] = thinking_response.strip()
                                _saved_id = save_assistant_response(
                                    sess, session_manager, session, _response_to_save, _metrics_to_save,
                                    character_name=ctx.preset.character_name,
                                    web_sources=web_sources,
                                    rag_sources=ctx.rag_sources,
                                    used_memories=ctx.used_memories,
                                    incognito=incognito,
                                )
                                if _saved_id:
                                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved_id})}\n\n'
                                run_post_response_tasks(
                                    sess, session_manager, session, message, _response_to_save,
                                    _metrics_to_save, ctx.uprefs, memory_manager, memory_vector, webhook_manager,
                                    incognito=incognito, compare_mode=compare_mode,
                                    character_name=ctx.preset.character_name,
                                    agent_rounds=_agent_rounds,
                                    agent_tool_calls=_agent_tool_calls,
                                    skills_manager=skills_manager,
                                    owner=_user,
                                    extract_skills=user_requested_agent,
                                    allow_background_extraction=not tool_policy.block_all_tool_calls,
                                )
                            _stream_set(session, status="done")
                            yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    try:
                        if full_response:
                            logger.info("Client disconnected mid-stream for session %s, saving partial response (%d chars)", session, len(full_response))
                            _stopped_content2, _stopped_md2 = clean_thinking_for_save(
                                full_response,
                                {
                                    "stopped": True,
                                    "model": _actual_model or _answered_by or _requested_model,
                                    "requested_model": _requested_model,
                                },
                            )
                            sess.add_message(ChatMessage("assistant", _stopped_content2, metadata=_stopped_md2))
                            if not incognito:
                                session_manager.save_sessions()
                    except Exception:
                        logger.exception("Failed to save partial response on disconnect (session %s)", session)
                    raise
                finally:
                    _active_streams.pop(session, None)

        async def _safe_stream() -> AsyncGenerator[str, None]:
            try:
                async for chunk in stream_with_save():
                    yield chunk
            finally:
                _active_streams.pop(session, None)

        if compare_mode:
            return StreamingResponse(_safe_stream(), media_type="text/event-stream")

        agent_runs.start(session, _safe_stream())
        return StreamingResponse(agent_runs.subscribe(session), media_type="text/event-stream")

    # ------------------------------------------------------------------ #
    # GET /api/chat/resume
    # ------------------------------------------------------------------ #
    @router.get("/api/chat/resume/{session_id}")
    async def chat_resume(request: Request, session_id: str) -> StreamingResponse:
        _verify_session_owner(request, session_id)
        if not agent_runs.is_active(session_id):
            raise HTTPException(404, "No active run for this session")
        return StreamingResponse(agent_runs.subscribe(session_id), media_type="text/event-stream")

    # ------------------------------------------------------------------ #
    # POST /api/chat/stop
    # ------------------------------------------------------------------ #
    @router.post("/api/chat/stop/{session_id}")
    async def chat_stop(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        stopped = agent_runs.stop(session_id)
        return {"stopped": stopped}

    # ------------------------------------------------------------------ #
    # GET /api/chat/stream_status
    # ------------------------------------------------------------------ #
    @router.get("/api/chat/stream_status/{session_id}")
    async def chat_stream_status(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        rec = _active_streams.get(session_id)
        if rec is None:
            if agent_runs.is_active(session_id):
                return {"status": "streaming", "detached": True}
            raise HTTPException(404, "No active stream for this session")
        return rec

    # ------------------------------------------------------------------ #
    # POST /api/inject_context
    # ------------------------------------------------------------------ #
    @router.post("/api/inject_context/{session_id}")
    async def inject_context(request: Request, session_id: str, context: str = Form(...)) -> Dict[str, str]:
        _verify_session_owner(request, session_id)
        try:
            sess = session_manager.get_session(session_id)
            msg = untrusted_context_message("injected research context", f"Research Context: {context}")
            sess.add_message(ChatMessage(msg["role"], msg["content"], metadata=msg.get("metadata")))
            session_manager.save_sessions()
            return {"status": "context_injected"}
        except KeyError:
            raise HTTPException(404, "Session not found")

    # ------------------------------------------------------------------ #
    # GET /api/search
    # ------------------------------------------------------------------ #
    @router.get("/api/search")
    async def search_messages(
        request: Request,
        q: str = Query("", min_length=0),
        limit: int = Query(20, ge=1, le=100),
    ) -> List[Dict[str, Any]]:
        if not q or not q.strip():
            return []

        _user = effective_user(request)
        return [
            result.to_dict()
            for result in search_session_messages(
                q,
                limit=limit,
                owner=_user,
                restrict_owner=_user is not None,
                include_legacy_owner=False,
            )
        ]

    # ------------------------------------------------------------------ #
    # POST /api/rewrite
    # ------------------------------------------------------------------ #
    @router.post("/api/rewrite")
    async def rewrite_message(request: Request) -> StreamingResponse:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        session_id = body.get("session_id")
        original_text = body.get("original_text", "")
        instruction = body.get("instruction", "")

        if not session_id or not original_text or not instruction:
            raise HTTPException(400, "session_id, original_text, and instruction are required")

        _verify_session_owner(request, session_id)

        try:
            sess = session_manager.get_session(session_id)
        except (KeyError, SessionNotFoundError):
            raise HTTPException(404, "Session not found")

        messages = [
            {"role": "system", "content": (
                "You are rewriting a previous response. Follow the instruction exactly. "
                "Output ONLY the rewritten text — no preamble, no explanation, no meta-commentary. "
                "Preserve any formatting (markdown, code blocks, lists) from the original."
            )},
            {"role": "user", "content": (
                f"Here is the original response:\n\n{original_text}\n\n"
                f"Instruction: {instruction}"
            )},
        ]

        async def stream_rewrite() -> AsyncGenerator[str, None]:
            full_response = ""
            try:
                async for chunk in stream_llm(
                    sess.endpoint_url,
                    sess.model,
                    messages,
                    headers=sess.headers,
                    temperature=0.7,
                    max_tokens=0,
                    tools=None,
                ):
                    if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                        try:
                            data = json.loads(chunk[6:])
                            if "delta" in data:
                                if not data.get("thinking"):
                                    full_response += data["delta"]
                                yield chunk
                        except json.JSONDecodeError:
                            yield chunk
                    elif chunk.startswith("event: "):
                        yield chunk
                    elif chunk == "data: [DONE]\n\n":
                        from src.research_utils import strip_thinking
                        full_response = strip_thinking(full_response).strip() or full_response
                        if full_response:
                            for msg in reversed(sess.history):
                                if (isinstance(msg, ChatMessage) and msg.role == 'assistant') or \
                                   (isinstance(msg, dict) and msg.get('role') == 'assistant'):
                                    if isinstance(msg, ChatMessage):
                                        msg.content = full_response
                                    else:
                                        msg['content'] = full_response
                                    break
                            db = SessionLocal()
                            try:
                                db_msg = (
                                    db.query(DBChatMessage)
                                    .filter(DBChatMessage.session_id == session_id, DBChatMessage.role == 'assistant')
                                    .order_by(DBChatMessage.timestamp.desc())
                                    .first()
                                )
                                if db_msg:
                                    db_msg.content = full_response
                                    db.commit()
                            except Exception as e:
                                logger.warning("Failed to update rewritten message in DB: %s", e)
                                db.rollback()
                            finally:
                                db.close()
                            session_manager.save_sessions()
                        yield chunk
            except Exception as e:
                logger.error("Rewrite stream error: %s", e)
                yield f'event: error\ndata: {json.dumps({"error": str(e), "status": 500})}\n\n'

        return StreamingResponse(stream_rewrite(), media_type="text/event-stream")

    return router
