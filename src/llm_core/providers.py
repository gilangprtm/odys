# src/llm_core/providers.py
import asyncio
import json
import logging
import os
import re
import time
from typing import Optional, Dict, List, Tuple
from fastapi import HTTPException

import httpx

from src.llm_core.config import (
    LLMConfig,
    _detect_provider,
    _normalize_ollama_url,
    _normalize_openai_chat_url,
    _normalize_anthropic_url,
    _normalize_chatgpt_subscription_url,
    _build_ollama_payload,
    _build_chatgpt_responses_payload,
    _parse_ollama_response,
    _provider_headers,
    _format_upstream_error,
    _format_chatgpt_subscription_error,
    _get_cache_key,
    _get_cached_response,
    _set_cached_response,
    _is_host_dead,
    _mark_host_dead,
    _clear_host_dead,
    _host_key,
    _local_model_slot,
    note_model_activity,
    _call_timeout,
    DEAD_HOST_COOLDOWN,
    _get_http_client,
    _apply_local_cache_affinity,
    _apply_local_generation_stability,
    _is_ollama_openai_compat_url,
    _supports_thinking,
    _omit_temperature,
    _uses_max_completion_tokens,
    _MISTRAL_REASONING_EFFORT,
    _restricts_temperature,
    _normalize_mistral_content,
    httpx_post_kimi_aware,
    httpx_post_kimi_aware_async,
    _sanitize_llm_messages,
    get_context_length,
)

logger = logging.getLogger(__name__)


def _dedupe_candidates(candidates):
    """Filter malformed entries and drop a later repeat of an already-seen
    ``(url, model)`` route, preserving order (first occurrence wins).
    """
    seen = set()
    out = []
    for c in candidates or []:
        if not c or not c[0] or not c[1]:
            continue
        key = (c[0], c[1])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ── Sync LLM call ──

def llm_call(url: str, model: str, messages: List[Dict], temperature: float = LLMConfig.DEFAULT_TEMPERATURE,
             max_tokens: int = LLMConfig.DEFAULT_MAX_TOKENS, headers: Optional[Dict] = None,
             timeout: int = LLMConfig.DEFAULT_TIMEOUT, prompt_type: Optional[str] = None) -> str:
    """Synchronous LLM call with optional prompt type enhancement."""
    h = _provider_headers(_detect_provider(url))
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except Exception:
            headers = None
    if isinstance(headers, dict):
        h.update(headers)

    messages_copy = _sanitize_llm_messages(messages)

    sys_parts = []
    non_sys = []
    for m in messages_copy:
        if m.get("role") == "system":
            sys_parts.append(m.get('content') or '')
        else:
            non_sys.append(m)
    if sys_parts:
        messages_copy = [{"role": "system", "content": "\n\n".join(sys_parts)}] + non_sys
    else:
        messages_copy = non_sys

    provider = _detect_provider(url)
    cache_key = _get_cache_key(url, model, messages_copy, temperature, max_tokens)
    cached_response = _get_cached_response(cache_key)
    if cached_response:
        logger.debug(f"Returning cached response for key: {cache_key}")
        return cached_response

    if provider == "anthropic":
        target_url = _normalize_anthropic_url(url)
        h = _build_anthropic_headers(headers)
        payload = _build_anthropic_payload(model, messages_copy, temperature, max_tokens)
    elif provider == "ollama":
        target_url = _normalize_ollama_url(url)
        payload = _build_ollama_payload(
            model, messages_copy, temperature, max_tokens,
            stream=False, num_ctx=get_context_length(url, model),
        )
    else:
        target_url = _normalize_openai_chat_url(url)
        if provider == "copilot":
            from src.copilot import apply_request_headers
            apply_request_headers(h, messages_copy)
        payload = {
            "model": model,
            "messages": messages_copy,
            "temperature": temperature,
        }
        if _omit_temperature(provider, model):
            payload.pop("temperature", None)
        if max_tokens and max_tokens > 0:
            tok_key = "max_completion_tokens" if _uses_max_completion_tokens(model) else "max_tokens"
            payload[tok_key] = max_tokens
        _apply_local_generation_stability(payload, target_url, model)
        if provider == "mistral" and _supports_thinking(model):
            payload["reasoning_effort"] = _MISTRAL_REASONING_EFFORT
    try:
        note_model_activity(target_url, model)
        r = httpx_post_kimi_aware(target_url, h, json=payload, timeout=timeout)
    except Exception as e:
        raise HTTPException(502, f"POST {target_url} failed: {e}")
    if not r.is_success:
        raise HTTPException(502, f"Upstream {target_url} -> {r.status_code}: {r.text}")
    data = r.json()
    try:
        if provider == "anthropic":
            response = _parse_anthropic_response(data)
        elif provider == "ollama":
            response = _parse_ollama_response(data)
        else:
            msg = data["choices"][0]["message"]
            content = msg.get("content")
            if isinstance(content, list):
                text_part, thinking_part = _normalize_mistral_content(content)
                if thinking_part:
                    response = thinking_part + "\n\n" + (text_part or "")
                else:
                    response = text_part or msg.get("reasoning_content") or ""
            else:
                response = content or msg.get("reasoning_content") or ""
        _set_cached_response(cache_key, response)
        return response
    except Exception:
        raise HTTPException(502, f"Unexpected schema from {target_url}: {str(data)[:400]}")


def llm_call_with_fallback(candidates, messages, **kwargs) -> str:
    cands = _dedupe_candidates(candidates)
    if not cands:
        raise HTTPException(503, "No model endpoint configured")
    last_err = None
    for i, (url, model, headers) in enumerate(cands):
        try:
            return llm_call(url, model, messages, headers=headers, **kwargs)
        except Exception as e:
            last_err = e
            tag = "primary" if i == 0 else "candidate"
            logger.warning(f"[fallback] {tag} {model} failed ({type(e).__name__}); trying next")
            continue
    raise last_err if last_err else HTTPException(503, "All fallback candidates failed")


async def llm_call_async_with_fallback(candidates, messages, **kwargs) -> str:
    cands = _dedupe_candidates(candidates)
    if not cands:
        raise HTTPException(503, "No model endpoint configured")
    last_err = None
    for i, (url, model, headers) in enumerate(cands):
        try:
            return await llm_call_async(url, model, messages, headers=headers, **kwargs)
        except Exception as e:
            last_err = e
            tag = "primary" if i == 0 else "candidate"
            logger.warning(f"[fallback] {tag} {model} failed ({type(e).__name__}); trying next")
            continue
    raise last_err if last_err else HTTPException(503, "All fallback candidates failed")


async def llm_call_async(
    url: str,
    model: str,
    messages: List[Dict],
    temperature: float = LLMConfig.DEFAULT_TEMPERATURE,
    max_tokens: int = LLMConfig.DEFAULT_MAX_TOKENS,
    headers: Optional[Dict] = None,
    timeout: int = LLMConfig.STREAM_TIMEOUT,
    max_retries: int = LLMConfig.MAX_RETRIES,
    prompt_type: Optional[str] = None,
    session_id: Optional[str] = None,
    workload: str = "foreground",
) -> str:
    """Asynchronous LLM call using httpx with connection pooling, timeout, retry logic, and performance logging."""
    provider = _detect_provider(url)
    messages_copy = _sanitize_llm_messages(messages)

    sys_parts = []
    non_sys = []
    for m in messages_copy:
        if m.get("role") == "system":
            sys_parts.append(m.get('content') or '')
        else:
            non_sys.append(m)
    if sys_parts:
        messages_copy = [{"role": "system", "content": "\n\n".join(sys_parts)}] + non_sys
    else:
        messages_copy = non_sys

    cache_key = _get_cache_key(url, model, messages_copy, temperature, max_tokens)
    cached_response = _get_cached_response(cache_key)
    if cached_response:
        logger.debug(f"Returning cached response for key: {cache_key}")
        return cached_response

    if provider == "chatgpt-subscription":
        # Reuse stream_llm for ChatGPT Subscription (must stream)
        from src.llm_core.stream import stream_llm
        parts: List[str] = []
        async for chunk in stream_llm(
            url, model, messages_copy,
            temperature=temperature, max_tokens=max_tokens,
            headers=headers, timeout=timeout, workload=workload,
        ):
            event_is_error = False
            for line in str(chunk).splitlines():
                if line.startswith("event:"):
                    event_is_error = line[6:].strip() == "error"
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                if raw == "[DONE]":
                    response = "".join(parts)
                    _set_cached_response(cache_key, response)
                    return response
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event_is_error or data.get("error") or (data.get("status") and data.get("text")):
                    status = int(data.get("status") or 502)
                    text = data.get("text") or data.get("error") or "ChatGPT Subscription request failed"
                    raise HTTPException(status, text)
                delta = data.get("delta")
                if isinstance(delta, str):
                    parts.append(delta)
        response = "".join(parts)
        _set_cached_response(cache_key, response)
        return response

    if provider == "anthropic":
        target_url = _normalize_anthropic_url(url)
        h = _build_anthropic_headers(headers)
        payload = _build_anthropic_payload(model, messages_copy, temperature, max_tokens)
    elif provider == "ollama":
        target_url = _normalize_ollama_url(url)
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        payload = _build_ollama_payload(
            model, messages_copy, temperature, max_tokens,
            stream=False, num_ctx=get_context_length(url, model),
        )
    else:
        target_url = _normalize_openai_chat_url(url)
        h = _provider_headers(provider, headers)
        if provider == "copilot":
            from src.copilot import apply_request_headers
            apply_request_headers(h, messages_copy)
        payload = {
            "model": model,
            "messages": messages_copy,
            "temperature": temperature,
        }
        if _omit_temperature(provider, model):
            payload.pop("temperature", None)
        if max_tokens and max_tokens > 0:
            tok_key = "max_completion_tokens" if _uses_max_completion_tokens(model) else "max_tokens"
            payload[tok_key] = max_tokens
        if _is_ollama_openai_compat_url(url) and _supports_thinking(model):
            payload["think"] = False
        if provider == "mistral" and _supports_thinking(model):
            payload["reasoning_effort"] = _MISTRAL_REASONING_EFFORT
        _apply_local_cache_affinity(payload, url, session_id)
        _apply_local_generation_stability(payload, target_url, model)

    if _is_host_dead(target_url):
        raise HTTPException(503, f"Upstream {_host_key(target_url)} marked unreachable (cooldown active)")

    call_timeout = _call_timeout(timeout)
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        start = time.time()
        try:
            async with _local_model_slot(target_url, model, workload):
                note_model_activity(target_url, model)
                client = _get_http_client()
                r = await httpx_post_kimi_aware_async(client, target_url, h, json=payload, timeout=call_timeout)
            duration = time.time() - start
            if not r.is_success:
                friendly = _format_upstream_error(r.status_code, r.text, target_url)
                logger.warning(
                    f"LLM async call to {target_url} failed in {duration:.2f}s "
                    f"(attempt {attempt}): HTTP {r.status_code} {friendly}"
                )
                if r.status_code in (429, 502, 503, 504) and attempt < max_retries:
                    await asyncio.sleep(LLMConfig.RETRY_DELAY)
                    continue
                raise HTTPException(r.status_code, friendly)
            logger.info(f"LLM async call to {target_url} succeeded in {duration:.2f}s (attempt {attempt})")
            _clear_host_dead(target_url)
            # Handle potential "Extra data" in response or SSE stream forced by router
            text = r.text.strip()
            data = None
            
            # If text looks like SSE stream (multiple "data: " prefixes)
            if "data:" in text:
                try:
                    import re
                    # Extract content from SSE chunks
                    content_parts = []
                    role = "assistant"
                    for line in text.splitlines():
                        line = line.strip()
                        if line.startswith("data:"):
                            chunk_str = line[5:].strip()
                            if chunk_str == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(chunk_str)
                                if "choices" in chunk and chunk["choices"]:
                                    delta = chunk["choices"][0].get("delta", {})
                                    if "content" in delta and delta["content"]:
                                        content_parts.append(delta["content"])
                                    if "role" in delta and delta["role"]:
                                        role = delta["role"]
                            except Exception:
                                pass
                    
                    if content_parts:
                        full_content = "".join(content_parts)
                        # Reconstruct a mock valid OpenAI response dict
                        data = {
                            "choices": [{
                                "message": {
                                    "role": role,
                                    "content": full_content
                                }
                            }]
                        }
                except Exception as e:
                    logger.warning(f"Failed to parse SSE-like response text: {e}")

            if data is None:
                try:
                    data = r.json()
                except json.JSONDecodeError as e:
                    # Try to extract first valid JSON object
                    import re
                    # Find first complete JSON object (non-greedy)
                    match = re.search(r'\{.*?\}', text, re.DOTALL)
                    if match:
                        try:
                            data = json.loads(match.group(0))
                        except json.JSONDecodeError:
                            # Try greedy match as last resort
                            match_greedy = re.search(r'\{.*\}', text, re.DOTALL)
                            if match_greedy:
                                try:
                                    data = json.loads(match_greedy.group(0))
                                except json.JSONDecodeError:
                                    raise HTTPException(502, f"Upstream {target_url} returned invalid JSON: {e}, text: {text[:500]}")
                            else:
                                raise HTTPException(502, f"Upstream {target_url} returned invalid JSON: {e}, text: {text[:500]}")
                    else:
                        raise HTTPException(502, f"Upstream {target_url} returned non-JSON: {text[:500]}")
            try:
                if provider == "anthropic":
                    response = _parse_anthropic_response(data)
                elif provider == "ollama":
                    response = _parse_ollama_response(data)
                else:
                    msg = data["choices"][0]["message"]
                    response = msg.get("content") or msg.get("reasoning_content") or ""
                _set_cached_response(cache_key, response)
                return response
            except Exception:
                raise HTTPException(502, f"Unexpected schema from {target_url}: {str(data)[:400]}")
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            _cooled = _mark_host_dead(target_url)
            duration = time.time() - start
            _tail = f" — host cooled for {DEAD_HOST_COOLDOWN:.0f}s" if _cooled else " — transient, will retry"
            logger.warning(f"LLM async connect to {target_url} failed after {duration:.2f}s: {e}{_tail}")
            if _cooled or attempt >= max_retries:
                raise HTTPException(503, f"Cannot reach {_host_key(target_url)}: {e}")
            await asyncio.sleep(LLMConfig.RETRY_DELAY)
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            duration = time.time() - start
            logger.warning(f"LLM async call attempt {attempt} failed after {duration:.2f}s: {e}")
            if attempt >= max_retries:
                raise HTTPException(502, f"POST {target_url} failed after {max_retries} attempts: {e}")
            await asyncio.sleep(LLMConfig.RETRY_DELAY)


# ── Model listing ──

def _model_list_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    for suffix in ("/models", "/chat/completions", "/completions", "/v1/messages", "/responses"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    for suffix in ("/chat", "/tags", "/generate"):
        if base.endswith("/api" + suffix):
            base = base[: -len(suffix)].rstrip("/")
    return base


def _parse_model_cache(raw) -> List[str]:
    if not raw:
        return []
    try:
        models = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if not isinstance(models, list):
        return []
    out = []
    seen = set()
    for item in models:
        mid = str(item or "").strip()
        if not mid or mid in seen:
            continue
        out.append(mid)
        seen.add(mid)
    return out


def _configured_cached_model_ids(
    endpoint_url: str,
    *,
    owner: Optional[str] = None,
    endpoint_id: Optional[str] = None,
) -> List[str]:
    target = _model_list_base(endpoint_url)
    if not target:
        return []
    try:
        from src.database import SessionLocal, ModelEndpoint
    except Exception:
        return []
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if endpoint_id:
            q = q.filter(ModelEndpoint.id == endpoint_id)
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        rows = q.all()
        for ep in rows:
            if _model_list_base(getattr(ep, "base_url", "")) != target:
                continue
            models = _parse_model_cache(getattr(ep, "cached_models", None) or getattr(ep, "models", None))
            if not models:
                continue
            hidden = set(_parse_model_cache(getattr(ep, "hidden_models", None)))
            return [m for m in models if m not in hidden]
    except Exception:
        return []
    finally:
        try:
            db.close()
        except Exception:
            pass
    return []


def list_model_ids(
    base_chat_url: str,
    timeout: int = LLMConfig.DEFAULT_TIMEOUT,
    headers: Optional[Dict] = None,
    *,
    owner: Optional[str] = None,
    endpoint_id: Optional[str] = None,
) -> List[str]:
    cached = _configured_cached_model_ids(base_chat_url, owner=owner, endpoint_id=endpoint_id)
    if cached:
        return cached
    provider = _detect_provider(base_chat_url)
    if provider == "anthropic":
        from src.llm_core.config import ANTHROPIC_MODELS
        return list(ANTHROPIC_MODELS)
    try:
        h = {}
        if headers:
            h.update(headers)
        if provider == "ollama":
            models_url = _ollama_api_root(base_chat_url) + "/tags"
        else:
            from src.endpoint_resolver import build_models_url
            models_url = build_models_url(base_chat_url)
        r = httpx_get_kimi_aware(models_url, h, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else (data.get("data") or [])
        model_ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
        if not model_ids and isinstance(data, dict):
            model_ids = [
                m.get("name") or m.get("model")
                for m in (data.get("models") or [])
                if m.get("name") or m.get("model")
            ]
        return model_ids
    except Exception:
        try:
            if ":11434" in base_chat_url or "ollama" in base_chat_url.lower():
                root = base_chat_url.replace("/v1/chat/completions", "").replace("/chat/completions", "").rstrip("/")
                r = httpx.get(root + "/api/tags", timeout=timeout)
                r.raise_for_status()
                return [m.get("name") or m.get("model") for m in (r.json().get("models") or []) if m.get("name") or m.get("model")]
        except Exception as e:
            logger.warning("Failed to fetch model list from configured endpoint", exc_info=e)
        return []


def normalize_model_id(
    endpoint_url: str,
    requested: str,
    timeout: int = LLMConfig.DEFAULT_TIMEOUT,
    *,
    owner: Optional[str] = None,
    endpoint_id: Optional[str] = None,
) -> Optional[str]:
    avail = list_model_ids(endpoint_url, timeout, owner=owner, endpoint_id=endpoint_id)
    if not avail:
        return None
    if requested in avail:
        return requested
    import os as _os
    req_base = _os.path.basename(requested.rstrip("/"))
    for a in avail:
        if _os.path.basename(a.rstrip("/")) == req_base:
            return a
    return None


# ── OpenAI content → Anthropic content block converter ──
def _convert_openai_content_to_anthropic(content):
    """Convert OpenAI multimodal content blocks to Anthropic format."""
    if not isinstance(content, list):
        return content
    converted = []
    for block in content:
        if not isinstance(block, dict):
            converted.append(block)
            continue
        if block.get("type") == "image_url":
            url = (block.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                try:
                    header, b64_data = url.split(",", 1)
                    media_type = header.split(";")[0].replace("data:", "")
                except (ValueError, IndexError):
                    continue
                converted.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64_data},
                })
            else:
                converted.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        elif block.get("type") == "text":
            converted.append(block)
        else:
            converted.append(block)
    return converted


def _build_anthropic_payload(model, messages, temperature, max_tokens, stream=False, tools=None):
    """Convert OpenAI-style messages to Anthropic format."""
    system_parts = []
    chat_messages = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(m.get("content") or "")
        elif m.get("role") == "tool":
            chat_messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": m.get("content", "")}],
            })
        elif m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
            content = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args_str = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except (json.JSONDecodeError, TypeError):
                    args = {}
                content.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""), "input": args})
            chat_messages.append({"role": "assistant", "content": content})
        else:
            content = _convert_openai_content_to_anthropic(m["content"])
            chat_messages.append({"role": m["role"], "content": content})
    if temperature is not None:
        temperature = max(0.0, min(temperature, 1.0))
    payload = {
        "model": model,
        "messages": chat_messages,
        "max_tokens": max_tokens if max_tokens and max_tokens > 0 else 4096,
    }
    from src.llm_core.config import _anthropic_rejects_temperature
    if not _anthropic_rejects_temperature(model):
        payload["temperature"] = temperature
    if system_parts:
        system_text = "\n\n".join(system_parts)
        system_block = {"type": "text", "text": system_text}
        if tools or len(system_text) > 4000:
            system_block["cache_control"] = {"type": "ephemeral"}
        payload["system"] = [system_block]
    if stream:
        payload["stream"] = True
    if tools:
        anthropic_tools = []
        for t in tools:
            if t.get("type") == "function":
                fn = t["function"]
                anthropic_tools.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
        if anthropic_tools:
            anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
            payload["tools"] = anthropic_tools
    return payload


def _build_anthropic_headers(headers):
    h = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if headers:
        for k, v in headers.items():
            if k.lower() == "authorization" and isinstance(v, str) and v.startswith("Bearer "):
                h["x-api-key"] = v[7:]
            else:
                h[k] = v
    return h


def _parse_anthropic_response(data: dict) -> str:
    return "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
