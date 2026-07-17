# src/llm_core/config.py
import httpx
import asyncio
import time
import json
import logging
import hashlib
import threading
import re
import os
from contextlib import asynccontextmanager
from fastapi import HTTPException
from typing import Optional, Dict, List, Tuple
from src.model_context import get_context_length, DEFAULT_CONTEXT, is_local_endpoint
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCAL_MODEL_LOCK = asyncio.Lock()
_LOCAL_MODEL_WAITING_FOREGROUND = 0
_LOCAL_MODEL_CURRENT: Dict[str, object] = {}


def _local_model_gate_enabled() -> bool:
    return os.getenv("ODYSSEUS_LOCAL_MODEL_GATE", "true").lower() not in {"0", "false", "no", "off"}


def _gate_workload(workload: Optional[str]) -> str:
    return "background" if str(workload or "").lower() == "background" else "foreground"


@asynccontextmanager
async def _local_model_slot(target_url: str, model: str, workload: Optional[str] = None):
    """Serialize local model traffic, with foreground chat taking priority."""
    if not _local_model_gate_enabled() or not is_local_endpoint(target_url):
        yield
        return

    global _LOCAL_MODEL_WAITING_FOREGROUND
    kind = _gate_workload(workload)
    current_task = asyncio.current_task()
    if kind == "foreground":
        _LOCAL_MODEL_WAITING_FOREGROUND += 1
        current = dict(_LOCAL_MODEL_CURRENT)
        if current.get("workload") == "background":
            task = current.get("task")
            if isinstance(task, asyncio.Task) and not task.done():
                logger.info(
                    "[model-gate] cancelling background local model call for foreground request model=%s",
                    model,
                )
                task.cancel()
    else:
        try:
            from src.interactive_gate import has_foreground_activity
        except Exception:
            has_foreground_activity = lambda: False  # type: ignore
        while _LOCAL_MODEL_WAITING_FOREGROUND > 0 or has_foreground_activity():
            await asyncio.sleep(0.25)

    acquired = False
    try:
        await _LOCAL_MODEL_LOCK.acquire()
        acquired = True
        if kind == "foreground":
            _LOCAL_MODEL_WAITING_FOREGROUND = max(0, _LOCAL_MODEL_WAITING_FOREGROUND - 1)
        _LOCAL_MODEL_CURRENT.clear()
        _LOCAL_MODEL_CURRENT.update({
            "task": current_task,
            "workload": kind,
            "url": target_url,
            "model": model,
            "started": time.time(),
        })
        yield
    finally:
        if kind == "foreground":
            _LOCAL_MODEL_WAITING_FOREGROUND = max(0, _LOCAL_MODEL_WAITING_FOREGROUND - 1)
        if acquired and _LOCAL_MODEL_LOCK.locked():
            owner = _LOCAL_MODEL_CURRENT.get("task")
            if owner is current_task:
                _LOCAL_MODEL_CURRENT.clear()
            _LOCAL_MODEL_LOCK.release()


class LLMConfig:
    """Configuration constants for LLM operations."""
    DEFAULT_TIMEOUT = 30
    DEFAULT_TEMPERATURE = 1.0
    DEFAULT_MAX_TOKENS = 0
    MAX_RETRIES = 3
    RETRY_DELAY = 0.5
    STREAM_TIMEOUT = 300
    CONNECT_TIMEOUT = float(os.getenv('LLM_CONNECT_TIMEOUT', '10') or '10')


def _call_timeout(read_timeout) -> httpx.Timeout:
    """Per-request timeout for non-streaming LLM calls (connect from config)."""
    return httpx.Timeout(connect=LLMConfig.CONNECT_TIMEOUT, read=float(read_timeout), write=10.0, pool=5.0)


def _stream_timeout(read_timeout) -> httpx.Timeout:
    """Per-request timeout for streaming LLM calls (connect from config)."""
    return httpx.Timeout(connect=LLMConfig.CONNECT_TIMEOUT, read=float(read_timeout), write=30.0, pool=5.0)


# Cache for LLM responses
def _get_cache_key(url: str, model: str, messages: List[Dict],
                   temperature: float, max_tokens: int) -> str:
    """Generate cache key for LLM requests."""
    hashable_messages = []
    for msg in messages:
        sorted_items = tuple(sorted(msg.items()))
        hashable_messages.append(sorted_items)
    content = json.dumps({
        'url': url, 'model': model, 'messages': hashable_messages,
        'temp': temperature, 'max_tokens': max_tokens
    }, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()


_response_cache = {}

DEAD_HOST_COOLDOWN = 20.0
_HOST_FAIL_THRESHOLD = 2
_dead_hosts: Dict[str, float] = {}
_host_fails: Dict[str, int] = {}
_host_health_lock = threading.Lock()
_model_activity: Dict[str, float] = {}

_HARMONY_MARKER_RE = re.compile(
    r"<\|channel\|>(analysis|commentary|final)"
    r"|<\|start\|>(?:assistant|system|user|tool)?"
    r"|<\|message\|>"
    r"|<\|end\|>"
    r"|<\|return\|>"
    r"|<\|call\|>"
)
_HARMONY_MARKERS = (
    "<|channel|>analysis", "<|channel|>commentary", "<|channel|>final",
    "<|start|>assistant", "<|start|>system", "<|start|>user", "<|start|>tool",
    "<|start|>", "<|message|>", "<|end|>", "<|return|>", "<|call|>",
)
_HARMONY_MAX_MARKER_LEN = max(len(marker) for marker in _HARMONY_MARKERS)

_VISIBLE_CHAT_TEMPLATE_ARTIFACT_RE = re.compile(
    r"(?:\|end\|)+\|?assistan(?:t)?\|?"
    r"|\|assistan(?:t)?\|"
    r"|<\|im_start\|>\s*assistant"
    r"|<\|im_end\|>",
    re.IGNORECASE,
)


def _strip_visible_chat_template_artifacts(text: str) -> str:
    return _VISIBLE_CHAT_TEMPLATE_ARTIFACT_RE.sub("", text or "")


def _harmony_suffix_hold_len(text: str) -> int:
    limit = min(len(text), _HARMONY_MAX_MARKER_LEN - 1)
    for n in range(limit, 0, -1):
        suffix = text[-n:]
        if any(marker.startswith(suffix) for marker in _HARMONY_MARKERS):
            return n
    return 0


class _HarmonyStreamRouter:
    """Route OpenAI harmony analysis/final channels without leaking markers."""

    def __init__(self) -> None:
        self._buf = ""
        self._seen_harmony = False
        self._channel: Optional[str] = None
        self._in_message = False

    def feed(self, text: str) -> List[Tuple[str, bool]]:
        if not text:
            return []
        self._buf += text
        return self._drain(final=False)

    def flush(self) -> List[Tuple[str, bool]]:
        return self._drain(final=True)

    def _append_text(self, out: List[Tuple[str, bool]], text: str) -> None:
        if not text:
            return
        if not self._seen_harmony:
            out.append((text, False))
            return
        if self._in_message:
            out.append((text, self._channel in ("analysis", "commentary")))

    def _handle_marker(self, match: re.Match[str]) -> None:
        marker = match.group(0)
        self._seen_harmony = True
        if marker.startswith("<|channel|>"):
            self._channel = match.group(1)
            self._in_message = False
        elif marker == "<|message|>":
            self._in_message = True
        else:
            self._in_message = False
            if marker in {"<|end|>", "<|return|>", "<|call|>"}:
                self._channel = None

    def _drain(self, *, final: bool) -> List[Tuple[str, bool]]:
        out: List[Tuple[str, bool]] = []
        while True:
            match = _HARMONY_MARKER_RE.search(self._buf)
            if not match:
                break
            self._append_text(out, self._buf[:match.start()])
            self._handle_marker(match)
            self._buf = self._buf[match.end():]
        hold = 0 if final else _harmony_suffix_hold_len(self._buf)
        emit = self._buf if hold == 0 else self._buf[:-hold]
        self._buf = "" if hold == 0 else self._buf[-hold:]
        self._append_text(out, emit)
        return out


def _stream_delta_event(text: str, *, thinking: bool = False) -> str:
    payload = {"delta": text}
    if thinking:
        payload["thinking"] = True
    return f"data: {json.dumps(payload)}\n\n"


_DEGENERATE_WORD_RE = re.compile(r"[A-Za-z0-9_\u0370-\u03ff\u0400-\u04ff]+")


class _DegenerateStreamGuard:
    """Detect local-model token collapse before it floods the UI."""

    def __init__(self, model: str):
        self.model = model or "model"
        self.last_token = ""
        self.same_run = 0
        self.recent_tokens: List[str] = []
        self.total_chars = 0

    def check(self, text: str) -> Optional[str]:
        if not text:
            return None
        self.total_chars += len(text)
        tokens = [t.lower() for t in _DEGENERATE_WORD_RE.findall(text) if len(t) >= 2]
        if not tokens:
            return None
        for token in tokens:
            if token == self.last_token:
                self.same_run += 1
            else:
                self.last_token = token
                self.same_run = 1
            self.recent_tokens.append(token)
        if len(self.recent_tokens) > 96:
            self.recent_tokens = self.recent_tokens[-96:]

        reason = None
        if self.same_run >= 28 and self.total_chars >= 100:
            reason = f"repeated '{self.last_token}' {self.same_run} times"
        elif len(self.recent_tokens) >= 72:
            top = max(set(self.recent_tokens), key=self.recent_tokens.count)
            count = self.recent_tokens.count(top)
            if count >= 60 and count / max(len(self.recent_tokens), 1) >= 0.78:
                reason = f"repeated '{top}' {count}/{len(self.recent_tokens)} recent tokens"
        if not reason and len(self.recent_tokens) >= 80:
            grams = [tuple(self.recent_tokens[i:i + 4]) for i in range(0, len(self.recent_tokens) - 3)]
            if grams:
                top_gram = max(set(grams), key=grams.count)
                gram_count = grams.count(top_gram)
                if gram_count >= 10:
                    reason = f"repeated phrase '{' '.join(top_gram)}' {gram_count} times"
        if not reason:
            return None
        logger.warning("[degenerate-stream] aborting model=%s reason=%s", self.model, reason)
        message = (
            f"Stopped generation: {self.model} started repeating tokens "
            f"({reason}). Try a different model or lower temperature."
        )
        return f'event: error\ndata: {json.dumps({"status": 502, "text": message, "error": message})}\n\n'


def _model_activity_key(url: str, model: str) -> str:
    return f"{(url or '').strip()}|{(model or '').strip()}"


def _same_model_identity(left: str, right: str) -> bool:
    return (left or "").strip().lower() == (right or "").strip().lower()


def note_model_activity(url: str, model: str):
    if not url or not model:
        return
    _model_activity[_model_activity_key(url, model)] = time.time()


def seconds_since_model_activity(url: str, model: str) -> Optional[float]:
    ts = _model_activity.get(_model_activity_key(url, model))
    if not ts:
        return None
    return max(0.0, time.time() - ts)


def _host_key(url: str) -> str:
    from urllib.parse import urlsplit
    s = urlsplit(url)
    return f"{s.scheme}://{s.netloc}" if s.scheme and s.netloc else url


def _is_host_dead(url: str) -> bool:
    key = _host_key(url)
    with _host_health_lock:
        exp = _dead_hosts.get(key)
        if exp is None:
            return False
        if time.time() >= exp:
            _dead_hosts.pop(key, None)
            return False
        return True


def _mark_host_dead(url: str) -> bool:
    key = _host_key(url)
    with _host_health_lock:
        n = _host_fails.get(key, 0) + 1
        _host_fails[key] = n
        if n >= _HOST_FAIL_THRESHOLD:
            _dead_hosts[key] = time.time() + DEAD_HOST_COOLDOWN
            return True
        return False


def _clear_host_dead(url: str) -> None:
    key = _host_key(url)
    with _host_health_lock:
        _dead_hosts.pop(key, None)
        _host_fails.pop(key, None)


# Shared async HTTP client
_http_client: Optional[httpx.AsyncClient] = None
_http_limits = httpx.Limits(max_connections=100, max_keepalive_connections=30, keepalive_expiry=30.0)


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        from src.tls_overrides import llm_verify
        _http_client = httpx.AsyncClient(
            limits=_http_limits, http2=False, verify=llm_verify(),
        )
    return _http_client


def _get_cached_response(cache_key: str) -> Optional[str]:
    return _response_cache.get(cache_key)


def _set_cached_response(cache_key: str, response: str) -> None:
    if len(_response_cache) > 128:
        keys_to_remove = list(_response_cache.keys())[:64]
        for key in keys_to_remove:
            _response_cache.pop(key, None)
    _response_cache[cache_key] = response


# ── Anthropic native API adapter ──
ANTHROPIC_MODELS = [
    "claude-opus-4-20250514", "claude-opus-4",
    "claude-sonnet-4-20250514", "claude-sonnet-4", "claude-sonnet-4-5-20250929", "claude-sonnet-4-5",
    "claude-haiku-4-20250514", "claude-haiku-4", "claude-haiku-3-5-20241022", "claude-haiku-3-5",
]


def _is_ollama_native_url(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
    except Exception as e:
        logger.warning("Failed to parse URL for Ollama detection", exc_info=e)
        return False
    host = parsed.hostname or ""
    path = (parsed.path or "").rstrip("/")
    if _host_match(url, "ollama.com"):
        return True
    if path.startswith("/v1"):
        return False
    local_ollama_host = host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or parsed.port == 11434
    return local_ollama_host and (path == "" or path == "/api" or path.startswith("/api/"))


def _is_ollama_openai_compat_url(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    host = parsed.hostname or ""
    path = (parsed.path or "").rstrip("/")
    local_ollama_host = host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or parsed.port == 11434
    return local_ollama_host and (path == "/v1" or path.startswith("/v1/"))


def _ollama_api_root(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/api/chat"):
        return url[: -len("/chat")]
    if path.endswith("/api/tags"):
        return url[: -len("/tags")]
    if path.endswith("/api/generate"):
        return url[: -len("/generate")]
    if path.endswith("/api"):
        return url
    if path == "":
        return url + "/api"
    if _host_match(url, "ollama.com"):
        root = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://ollama.com"
        return root.rstrip("/") + "/api"
    return url


def _normalize_ollama_url(url: str) -> str:
    base = _ollama_api_root(url)
    return base.rstrip("/") + "/chat"


def _normalize_openai_chat_url(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if not base:
        return base
    if base.endswith("/chat/completions") or base.endswith("/completions"):
        return base
    if base.endswith("/models"):
        base = base[: -len("/models")].rstrip("/")
    return base + "/chat/completions"


def _normalize_anthropic_url(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/v1/messages"):
        return url
    if url.endswith("/v1"):
        return url + "/messages"
    return url + "/v1/messages"


def _normalize_chatgpt_subscription_url(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if base.endswith("/responses"):
        return base
    return base + "/responses"


def _ollama_normalize_messages(messages: List[Dict]) -> List[Dict]:
    """Adapt Odysseus' canonical OpenAI-style messages to native Ollama /api/chat."""
    out: List[Dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        nm = dict(m)
        tcs = nm.get("tool_calls")
        if tcs:
            new_calls = []
            for tc in tcs:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                call: Dict = {"function": {"name": fn.get("name", ""), "arguments": args or {}}}
                if tc.get("id"):
                    call["id"] = tc["id"]
                new_calls.append(call)
            nm["tool_calls"] = new_calls
        content = nm.get("content")
        if isinstance(content, list):
            text_parts: List[str] = []
            images: List[str] = list(nm.get("images") or [])
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    t = block.get("text")
                    if t:
                        text_parts.append(str(t))
                elif btype == "image_url":
                    url = (block.get("image_url") or {}).get("url", "")
                    if not url:
                        continue
                    if url.startswith("data:"):
                        _, _, b64 = url.partition(",")
                        if b64:
                            images.append(b64)
                    else:
                        logger.warning(
                            "Skipping non-data image_url (Ollama images[] requires base64): %s",
                            url[:80],
                        )
            nm["content"] = "\n".join(text_parts).strip()
            if images:
                nm["images"] = images
        out.append(nm)
    return out


_ollama_normalize_tool_messages = _ollama_normalize_messages


def _build_ollama_payload(
    model: str,
    messages: List[Dict],
    temperature: float,
    max_tokens: int,
    stream: bool = False,
    tools: Optional[List[Dict]] = None,
    num_ctx: Optional[int] = None,
) -> Dict:
    payload: Dict = {
        "model": model,
        "messages": _ollama_normalize_messages(messages),
        "stream": stream,
    }
    options: Dict = {}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens and max_tokens > 0:
        options["num_predict"] = max_tokens
    if num_ctx is not None and num_ctx > 0 and num_ctx != DEFAULT_CONTEXT:
        options["num_ctx"] = num_ctx
    if options:
        payload["options"] = options
    if tools:
        payload["tools"] = tools
    return payload


def _parse_ollama_response(data: dict) -> str:
    message = data.get("message") or {}
    return message.get("content") or data.get("response") or ""


def _host_match(url: str, *domains: str) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


# Kimi Code
KIMI_CODE_USER_AGENTS: tuple[str, ...] = (
    "claude-code/0.1.0", "claude-code/1.0.0", "KimiCLI/1.0",
    "Kilo-Code/1.0", "Roo-Code/1.0", "Cursor/1.0",
)
KIMI_CODE_USER_AGENT = KIMI_CODE_USER_AGENTS[0]
_kimi_code_ua_cache: dict[str, str] = {}


def _is_kimi_code_url(url: str) -> bool:
    if not url or not _host_match(url, "kimi.com"):
        return False
    try:
        return "/coding" in (urlparse(url).path or "")
    except Exception:
        return False


def _kimi_code_base_key(url: str) -> str:
    parsed = urlparse(url)
    path = (parsed.path or "").rstrip("/")
    for suffix in ("/chat/completions", "/models", "/completions"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    path = path.rstrip("/") or "/coding/v1"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _is_kimi_code_access_denied(status: int, body: bytes | str) -> bool:
    if status != 403:
        return False
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else (body or "")
    lower = text.lower()
    return "access_terminated_error" in lower or "coding agents" in lower or "only available for coding" in lower


def _kimi_code_ua_candidates(url: str) -> list[str]:
    if not _is_kimi_code_url(url):
        return []
    base_key = _kimi_code_base_key(url)
    cached = _kimi_code_ua_cache.get(base_key)
    if cached:
        return [cached] + [ua for ua in KIMI_CODE_USER_AGENTS if ua != cached]
    return list(KIMI_CODE_USER_AGENTS)


def _remember_kimi_code_user_agent(url: str, user_agent: str) -> None:
    _kimi_code_ua_cache[_kimi_code_base_key(url)] = user_agent


def apply_kimi_code_headers(headers: Optional[Dict], url: str) -> Dict[str, str]:
    h = dict(headers or {})
    if not _is_kimi_code_url(url):
        return h
    base_key = _kimi_code_base_key(url)
    cached = _kimi_code_ua_cache.get(base_key)
    if cached:
        h["User-Agent"] = cached
        return h
    models_url = base_key.rstrip("/") + "/models"
    from src.tls_overrides import llm_verify
    for ua in KIMI_CODE_USER_AGENTS:
        trial = dict(h)
        trial["User-Agent"] = ua
        try:
            r = httpx.get(models_url, headers=trial, timeout=8, verify=llm_verify())
        except Exception:
            continue
        if _is_kimi_code_access_denied(r.status_code, r.content):
            logger.debug("Kimi Code rejected User-Agent %s (403), trying next", ua)
            continue
        if r.status_code < 400:
            _remember_kimi_code_user_agent(url, ua)
            h["User-Agent"] = ua
            return h
        break
    h.setdefault("User-Agent", KIMI_CODE_USER_AGENT)
    return h


async def apply_kimi_code_headers_async(client, headers: Optional[Dict], url: str) -> Dict[str, str]:
    h = dict(headers or {})
    if not _is_kimi_code_url(url):
        return h
    base_key = _kimi_code_base_key(url)
    cached = _kimi_code_ua_cache.get(base_key)
    if cached:
        h["User-Agent"] = cached
        return h
    models_url = base_key.rstrip("/") + "/models"
    for ua in KIMI_CODE_USER_AGENTS:
        trial = dict(h)
        trial["User-Agent"] = ua
        try:
            r = await client.get(models_url, headers=trial, timeout=8)
        except Exception:
            continue
        if _is_kimi_code_access_denied(r.status_code, r.content):
            logger.debug("Kimi Code rejected User-Agent %s (403), trying next", ua)
            continue
        if r.status_code < 400:
            _remember_kimi_code_user_agent(url, ua)
            h["User-Agent"] = ua
            return h
        break
    h.setdefault("User-Agent", KIMI_CODE_USER_AGENT)
    return h


def httpx_get_kimi_aware(url: str, headers: Optional[Dict], **kwargs):
    h = apply_kimi_code_headers(headers, url)
    if not _is_kimi_code_url(url):
        return httpx.get(url, headers=h, **kwargs)
    last = None
    for ua in _kimi_code_ua_candidates(url):
        trial = dict(h)
        trial["User-Agent"] = ua
        last = httpx.get(url, headers=trial, **kwargs)
        if not _is_kimi_code_access_denied(last.status_code, last.content):
            if last.status_code < 400:
                _remember_kimi_code_user_agent(url, ua)
            return last
    return last


def httpx_post_kimi_aware(url: str, headers: Optional[Dict], **kwargs):
    h = apply_kimi_code_headers(headers, url)
    if not _is_kimi_code_url(url):
        return httpx.post(url, headers=h, **kwargs)
    last = None
    for ua in _kimi_code_ua_candidates(url):
        trial = dict(h)
        trial["User-Agent"] = ua
        last = httpx.post(url, headers=trial, **kwargs)
        if not _is_kimi_code_access_denied(last.status_code, last.content):
            if last.status_code < 400:
                _remember_kimi_code_user_agent(url, ua)
            return last
    return last


async def httpx_post_kimi_aware_async(client, url: str, headers: Optional[Dict], **kwargs):
    h = await apply_kimi_code_headers_async(client, headers, url)
    if not _is_kimi_code_url(url):
        return await client.post(url, headers=h, **kwargs)
    last = None
    for ua in _kimi_code_ua_candidates(url):
        trial = dict(h)
        trial["User-Agent"] = ua
        last = await client.post(url, headers=trial, **kwargs)
        if not _is_kimi_code_access_denied(last.status_code, last.content):
            if last.status_code < 400:
                _remember_kimi_code_user_agent(url, ua)
            return last
    return last


# ── Provider detection ──
def _detect_provider(url: str) -> str:
    if _is_ollama_native_url(url):
        return "ollama"
    if _host_match(url, "anthropic.com"):
        return "anthropic"
    if _host_match(url, "opencode.ai/zen/go"):
        return "opencode-go"
    if _host_match(url, "opencode.ai/zen"):
        return "opencode-zen"
    if _host_match(url, "openrouter.ai"):
        return "openrouter"
    if _host_match(url, "groq.com"):
        return "groq"
    if _host_match(url, "nvidia.com"):
        return "nvidia"
    if _host_match(url, "moonshot.ai") or _host_match(url, "moonshot.cn"):
        return "moonshot"
    from src.chatgpt_subscription import is_chatgpt_subscription_base
    if is_chatgpt_subscription_base(url):
        return "chatgpt-subscription"
    from src.copilot import is_copilot_base
    if is_copilot_base(url):
        return "copilot"
    if _host_match(url, "cerebras.ai"):
        return "cerebras"
    if _host_match(url, "mistral.ai"):
        return "mistral"
    return "openai"


def _is_self_hosted_openai_compatible(url: str) -> bool:
    if _detect_provider(url) != "openai" or _host_match(url, "openai.com"):
        return False
    from src.model_context import is_local_endpoint
    return is_local_endpoint(url)


def _apply_local_cache_affinity(payload: Dict, url: str, session_id: Optional[str]) -> None:
    if not session_id:
        return
    if not _is_self_hosted_openai_compatible(url):
        return
    payload.setdefault("session_id", str(session_id))
    payload.setdefault("cache_prompt", True)


def _is_local_minimax_mlx_request(url: str, model: str) -> bool:
    if not model:
        return False
    m = model.lower()
    if "minimax" not in m and "mini-max" not in m:
        return False
    try:
        from src.model_context import is_local_endpoint
        return is_local_endpoint(url)
    except Exception:
        return False


def _apply_local_generation_stability(payload: Dict, url: str, model: str) -> None:
    if not _is_local_minimax_mlx_request(url, model):
        return
    if "temperature" in payload:
        try:
            payload["temperature"] = min(float(payload.get("temperature") or 0.2), 0.2)
        except (TypeError, ValueError):
            payload["temperature"] = 0.2
    payload.setdefault("top_p", 0.9)
    payload.setdefault("top_k", 20)
    payload.setdefault("repetition_penalty", 1.12)
    payload.setdefault("repetition_context_size", 256)
    payload.setdefault("frequency_penalty", 0.08)
    payload.setdefault("frequency_context_size", 256)
    payload.setdefault("presence_penalty", 0.02)
    payload.setdefault("presence_context_size", 256)
    payload.setdefault("stop", ["<|im_end|>", "<|endoftext|>", "</s>"])
    if not payload.get("max_tokens") and not payload.get("max_completion_tokens"):
        payload["max_tokens"] = 2048


def _provider_headers(provider: str, headers: Optional[Dict] = None) -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if isinstance(headers, dict):
        h.update(headers)
    if provider == "openrouter":
        h.setdefault("HTTP-Referer", "https://github.com/pewdiepie-archdaemon/odysseus")
        h.setdefault("X-OpenRouter-Title", "Odysseus")
    if provider == "copilot":
        from src.copilot import copilot_headers
        for k, v in copilot_headers(None).items():
            h.setdefault(k, v)
    return h


def _provider_label(url: str) -> str:
    if not url:
        return "provider"
    if _host_match(url, "anthropic.com"): return "Anthropic"
    if _host_match(url, "ollama.com"): return "Ollama Cloud"
    if _host_match(url, "x.ai"): return "xAI"
    if _host_match(url, "openai.com"): return "OpenAI"
    if _host_match(url, "openrouter.ai"): return "OpenRouter"
    if _host_match(url, "opencode.ai/zen/go"): return "OpenCode Go"
    if _host_match(url, "opencode.ai/zen"): return "OpenCode Zen"
    if _host_match(url, "groq.com"): return "Groq"
    from src.chatgpt_subscription import is_chatgpt_subscription_base
    if is_chatgpt_subscription_base(url): return "ChatGPT Subscription"
    from src.copilot import is_copilot_base
    if is_copilot_base(url): return "GitHub Copilot"
    if _host_match(url, "cerebras.ai"): return "cerebras"
    if _host_match(url, "mistral.ai"): return "Mistral"
    if _host_match(url, "deepseek.com"): return "DeepSeek"
    if _host_match(url, "nvidia.com"): return "NVIDIA"
    if _host_match(url, "googleapis.com"): return "Google"
    if _host_match(url, "together.xyz", "together.ai"): return "Together"
    if _host_match(url, "fireworks.ai"): return "Fireworks"
    if _host_match(url, "kimi.com"):
        try:
            if "/coding" in (urlparse(url).path or ""):
                return "Kimi Code"
        except Exception:
            pass
    if _is_ollama_native_url(url): return "Ollama"
    try:
        _parsed_local = urlparse(url)
        host = (_parsed_local.hostname or "").lower()
    except Exception:
        return "provider"
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return "local endpoint"
    return host or "provider"


def _message_content_as_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                if part:
                    parts.append(str(part))
                continue
            if isinstance(part.get("text"), str):
                parts.append(part["text"])
                continue
            if isinstance(part.get("content"), str):
                parts.append(part["content"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def _chatgpt_subscription_instructions(messages: List[Dict]) -> str:
    instructions = [
        _message_content_as_text(msg.get("content")).strip()
        for msg in messages or []
        if (msg.get("role") or "") == "system"
    ]
    instructions = [part for part in instructions if part]
    if instructions:
        return "\n\n".join(instructions)
    return "You are a helpful AI assistant."


# ── Models that require max_completion_tokens instead of max_tokens ──
_MAX_COMPLETION_TOKENS_MODELS = {"o1", "o3", "o4", "gpt-4.5", "gpt-5"}


def _uses_max_completion_tokens(model: str) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(m.startswith(p) or f"/{p}" in m for p in _MAX_COMPLETION_TOKENS_MODELS)


# ── Temperature helpers ──
_FIXED_TEMPERATURE_MODELS = ("o1", "o3", "o4", "gpt-5", "kimi-for-coding")


def _restricts_temperature(model: str) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(m.startswith(p) or f"/{p}" in m for p in _FIXED_TEMPERATURE_MODELS)


def _moonshot_rejects_custom_temperature(provider: str, model: str) -> bool:
    if provider != "moonshot" or not isinstance(model, str):
        return False
    model_id = model.lower().rsplit("/", 1)[-1]
    return bool(re.match(r"^kimi-k2\.(?:5|6)(?:$|[-_:])", model_id))


def _omit_temperature(provider: str, model: str) -> bool:
    return _restricts_temperature(model) or _moonshot_rejects_custom_temperature(provider, model)


def _anthropic_rejects_temperature(model: str) -> bool:
    if not isinstance(model, str) or not model:
        return False
    match = re.search(r"(?<![a-z])opus[-_]?(\d+)[-_.](\d{1,2})(?!\d)", model.lower())
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2))) >= (4, 7)


_MISTRAL_REASONING_EFFORT = os.getenv("ODYSSEUS_MISTRAL_REASONING_EFFORT", "high")

_THINKING_MODEL_PATTERNS = (
    "qwen3", "qwq", "deepseek-r1", "deepseek-reasoner", "minimax",
    "m2-reap", "gemma", "stepfun", "step-3", "step3",
    "magistral", "mistral-small", "mistral-medium",
)


def _supports_thinking(model: str) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(p in m for p in _THINKING_MODEL_PATTERNS)


def _normalize_mistral_content(content):
    if isinstance(content, str):
        return content, ""
    if not isinstance(content, list):
        return "", ""
    text_parts = []
    thinking_parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text", "")
            if t:
                text_parts.append(t)
        elif btype == "thinking":
            inner = block.get("thinking", [])
            if isinstance(inner, list):
                for tb in inner:
                    if isinstance(tb, dict) and tb.get("text"):
                        thinking_parts.append(tb["text"])
            elif isinstance(inner, str):
                thinking_parts.append(inner)
    return "".join(text_parts), "".join(thinking_parts)


# ── ChatGPT Subscription payload builder ──
def _build_chatgpt_responses_payload(
    model: str,
    messages: List[Dict],
    temperature: float,
    max_tokens: int,
    *,
    stream: bool = False,
) -> Dict:
    from src.chatgpt_subscription import build_responses_input
    conversation = [msg for msg in (messages or []) if (msg.get("role") or "") != "system"]
    payload: Dict = {
        "model": model,
        "instructions": _chatgpt_subscription_instructions(messages),
        "input": build_responses_input(conversation),
        "stream": stream,
        "store": False,
    }
    if not _restricts_temperature(model):
        payload["temperature"] = temperature
    return payload


# ── Error formatters ──
def _format_chatgpt_subscription_error(status_code: int, text: str) -> str:
    if status_code in (401, 403):
        return "ChatGPT Subscription credentials expired or were rejected. Reconnect the provider."
    if status_code == 429:
        return "ChatGPT Subscription quota or rate limit was reached. Retry after the upstream limit resets."
    return _format_upstream_error(status_code, text, "https://chatgpt.com/backend-api/codex")


# ── Message sanitisation ──

def _as_content_blocks(content) -> List[Dict]:
    if isinstance(content, list):
        return content
    if content:
        return [{"type": "text", "text": str(content)}]
    return []


def _is_untrusted_context_content(content) -> bool:
    if isinstance(content, str):
        return (
            content.startswith("UNTRUSTED SOURCE DATA\n")
            or "<<<UNTRUSTED_SOURCE_DATA>>>" in content
        )
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and _is_untrusted_context_content(block.get("text") or "")
            for block in content
        )
    return False


_REFERENCE_CONTEXT_BOUNDARY = "Reference context received."


def _sanitize_llm_messages(messages: List[Dict]) -> List[Dict]:
    """Strip Odysseus-only metadata before sending messages to providers."""
    allowed = {"role", "content", "name", "tool_call_id", "tool_calls", "function_call", "reasoning_content"}
    cleaned = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        item = {k: v for k, v in msg.items() if k in allowed and v is not None}
        role = item.get("role")
        if not role:
            continue
        if role == "assistant":
            if "content" not in item and item.get("tool_calls"):
                item["content"] = None
            if "content" in item or item.get("tool_calls"):
                cleaned.append(item)
        elif role == "tool":
            if "content" in item and "tool_call_id" in item:
                cleaned.append(item)
        elif "content" in item:
            cleaned.append(item)

    # Repair tool-call adjacency
    repaired: List[Dict] = []
    i = 0
    while i < len(cleaned):
        msg = cleaned[i]
        role = msg.get("role")
        if role == "tool":
            logger.debug("Dropping orphan tool message before provider request")
            i += 1
            continue
        tool_calls = msg.get("tool_calls") if role == "assistant" else None
        if not tool_calls:
            repaired.append(msg)
            i += 1
            continue
        call_ids = [str(tc.get("id")) for tc in tool_calls if isinstance(tc, dict) and tc.get("id")]
        expected = set(call_ids)
        answered_ids = []
        tool_batch = []
        j = i + 1
        while j < len(cleaned) and cleaned[j].get("role") == "tool":
            tid = str(cleaned[j].get("tool_call_id") or "")
            if tid in expected and tid not in answered_ids:
                answered_ids.append(tid)
                tool_batch.append(cleaned[j])
            else:
                logger.debug("Dropping unmatched/duplicate tool message before provider request")
            j += 1
        if not tool_batch:
            plain = {k: v for k, v in msg.items() if k != "tool_calls"}
            if (plain.get("content") or "").strip():
                repaired.append(plain)
            else:
                logger.debug("Dropping unanswered assistant tool_calls before provider request")
            i = j
            continue
        answered = set(answered_ids)
        pruned_calls = [tc for tc in tool_calls if isinstance(tc, dict) and str(tc.get("id")) in answered]
        fixed = dict(msg)
        fixed["tool_calls"] = pruned_calls
        if "content" not in fixed:
            fixed["content"] = None
        repaired.append(fixed)
        repaired.extend(tool_batch)
        if len(pruned_calls) != len(tool_calls):
            logger.debug("Pruned unanswered assistant tool_calls before provider request")
        i = j

    # Merge consecutive user messages
    merged: List[Dict] = []
    for item in repaired:
        if not merged:
            merged.append(item)
            continue
        last = merged[-1]
        if last.get("role") == "user" and item.get("role") == "user":
            if _is_untrusted_context_content(last.get("content")):
                merged.append({"role": "assistant", "content": _REFERENCE_CONTEXT_BOUNDARY})
                merged.append(item)
                continue
            last_copy = dict(last)
            lc = last_copy.get("content")
            ic = item.get("content")
            if isinstance(lc, list) or isinstance(ic, list):
                merged_blocks = _as_content_blocks(lc) + _as_content_blocks(ic)
                if merged_blocks:
                    last_copy["content"] = merged_blocks
                else:
                    last_copy.pop("content", None)
            else:
                last_str = str(lc) if lc is not None else ""
                item_str = str(ic) if ic is not None else ""
                new_content = "\n\n".join(part for part in (last_str, item_str) if part)
                if new_content:
                    last_copy["content"] = new_content
                else:
                    last_copy.pop("content", None)
            merged[-1] = last_copy
        else:
            merged.append(item)
    return merged


def _format_upstream_error(status: int, body: bytes | str, url: str) -> str:
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = str(body)
    provider = _provider_label(url)
    detail = ""
    try:
        j = json.loads(body) if body else {}
        if isinstance(j, dict):
            err = j.get("error") or j
            if isinstance(err, dict):
                detail = (err.get("message") or err.get("detail") or "").strip()
            elif isinstance(err, str):
                detail = err.strip()
    except Exception:
        detail = (body or "").strip()[:240]
    if status in (401, 403):
        msg = f"{provider} rejected the API key"
        if status == 403:
            msg = f"{provider} denied access (403)"
        if detail:
            msg += f" — {detail}"
        msg += ". Check Model Endpoints → {} and re-paste the key.".format(provider)
        return msg
    if status == 404:
        return f"{provider} returned 404 — check the base URL and model name." + (f" ({detail})" if detail else "")
    if status == 429:
        return f"{provider} rate-limited the request (429)." + (f" {detail}" if detail else "")
    if status >= 500:
        return f"{provider} is having an outage (HTTP {status})." + (f" {detail}" if detail else "")
    return f"{provider} returned HTTP {status}" + (f": {detail}" if detail else "")
