"""Odys Desktop Bridge — app registry & path resolver.

Configurable whitelist of desktop apps that can be launched safely.
Adapted from AtlasOS Community (MIT License → DNA Odys).
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

_REGISTRY_PATH = Path(__file__).resolve().parent / "apps.json"

_ALLOWED_URI_SCHEMES = frozenset({
    "com.epicgames.launcher",
    "steam",
    "spotify",
    "whatsapp",
    "ms-windows-store",
    "ms-gamingoverlay",
})

_STORE_APP_HINTS: Dict[str, str] = {
    "whatsapp": (
        "WhatsApp is a Microsoft Store app. Please add its executable/URI to "
        "desktop_bridge/apps.json after locating it, or use whatsapp: if registered."
    ),
    "spotify": (
        "Spotify is a Microsoft Store app. Please install desktop Spotify or add "
        "its URI/path to apps.json."
    ),
}

_registry_cache: Optional[List[Dict[str, Any]]] = None
_alias_index_cache: Optional[Dict[str, str]] = None


# ── helpers ──────────────────────────────────────────────

def _expand_path(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = os.path.expandvars(text)
    text = text.replace("%USERNAME%", os.environ.get("USERNAME", ""))
    text = text.replace("%USERPROFILE%", os.environ.get("USERPROFILE", ""))
    text = text.replace("%LOCALAPPDATA%", os.environ.get("LOCALAPPDATA", ""))
    text = os.path.expanduser(text)
    return text


def _exists_file(path: str) -> Optional[str]:
    try:
        p = Path(path)
        if p.is_file():
            return str(p.resolve())
    except OSError:
        pass
    return None


def _which_exe(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return _exists_file(found)
    return None


def _windir() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows"))


def _localappdata() -> Path:
    val = (os.environ.get("LOCALAPPDATA") or "").strip()
    if val:
        return Path(val)
    try:
        return Path.home() / "AppData" / "Local"
    except RuntimeError:
        return Path(r"C:\Users\Default\AppData\Local")


# ── registry loading ─────────────────────────────────────

def load_registry(force_reload: bool = False) -> List[Dict[str, Any]]:
    global _registry_cache
    if _registry_cache is not None and not force_reload:
        return _registry_cache
    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        apps = data.get("apps") if isinstance(data, dict) else None
        if not isinstance(apps, list):
            apps = []
        _registry_cache = [a for a in apps if isinstance(a, dict)]
    except (OSError, json.JSONDecodeError):
        _registry_cache = []
    return _registry_cache


def invalidate_cache() -> None:
    global _registry_cache, _alias_index_cache
    _registry_cache = None
    _alias_index_cache = None


def get_app_by_id(app_id: str) -> Optional[Dict[str, Any]]:
    key = (app_id or "").strip().lower()
    for app in load_registry():
        if (app.get("id") or "").lower() == key:
            return app
    return None


# ── alias indexing ───────────────────────────────────────

def build_alias_index() -> Dict[str, str]:
    global _alias_index_cache
    if _alias_index_cache is not None:
        return _alias_index_cache
    index: Dict[str, str] = {}
    for app in load_registry():
        if not app.get("enabled", True):
            continue
        app_id = (app.get("id") or "").lower()
        if not app_id:
            continue
        index[app_id] = app_id
        for alias in app.get("aliases") or []:
            a = str(alias).strip().lower()
            if a:
                index[a] = app_id
    _alias_index_cache = index
    return index


def resolve_alias(text: str) -> Optional[str]:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not norm:
        return None
    index = build_alias_index()
    if norm in index:
        return index[norm]
    matches = [
        (alias, app_id)
        for alias, app_id in index.items()
        if norm == alias or norm.startswith(alias + " ")
    ]
    if matches:
        matches.sort(key=lambda x: len(x[0]), reverse=True)
        return matches[0][1]
    return None


# ── file resolution ──────────────────────────────────────

def _search_glob(pattern: str) -> List[str]:
    expanded = _expand_path(pattern)
    if not expanded:
        return []
    try:
        hits = glob.glob(expanded, recursive=True)
    except (OSError, ValueError):
        return []
    out: List[str] = []
    for hit in hits:
        resolved = _exists_file(hit)
        if resolved:
            out.append(resolved)
    out.sort(key=lambda p: Path(p).stat().st_mtime if Path(p).exists() else 0, reverse=True)
    return out


def _resolve_windows_builtin(app: Dict[str, Any], attempted: List[str]) -> Tuple[Optional[str], str]:
    exe_name = (app.get("exe") or "").strip()
    candidates = [
        _windir() / "System32" / exe_name,
        _windir() / "SysWOW64" / exe_name,
        Path(exe_name) if Path(exe_name).is_absolute() else None,
    ]
    for c in candidates:
        if c is None:
            continue
        s = str(c)
        attempted.append(s)
        found = _exists_file(s)
        if found:
            return found, s
    found = _which_exe(exe_name)
    if found:
        attempted.append(found)
        return found, f"which:{exe_name}"
    return None, ""


def _resolve_folder_exe(app: Dict[str, Any], attempted: List[str]) -> Tuple[Optional[str], str]:
    app_id = (app.get("id") or "").lower()
    env_key = (app.get("env_path_key") or "").strip()

    # 1. Try env override first
    if env_key:
        env_val = (os.environ.get(env_key) or "").strip()
        if env_val:
            expanded = _expand_path(env_val)
            attempted.append(expanded)
            if expanded.lower().endswith(".exe"):
                found = _exists_file(expanded)
                if found:
                    return found, f"env:{env_key}"
            else:
                joined = str(Path(expanded) / (app.get("exe") or ""))
                attempted.append(joined)
                found = _exists_file(joined)
                if found:
                    return found, f"env:{env_key}"

    # 2. Fallback paths
    for raw in app.get("fallback_paths") or []:
        expanded = _expand_path(str(raw))
        if not expanded:
            continue
        attempted.append(expanded)
        if expanded.lower().endswith(".exe"):
            found = _exists_file(expanded)
            if found:
                return found, expanded
        else:
            joined = str(Path(expanded) / (app.get("exe") or ""))
            attempted.append(joined)
            found = _exists_file(joined)
            if found:
                return found, joined

    # 3. Default path + exe
    folder_raw = (app.get("path") or "").strip()
    exe_name = (app.get("exe") or "").strip()
    folder = _expand_path(folder_raw) if folder_raw else ""
    if folder:
        attempted.append(folder)
        folder_path = Path(folder)
        if folder_path.is_file() and folder_path.suffix.lower() == ".exe":
            return str(folder_path.resolve()), folder
        if folder_path.is_dir() and exe_name:
            joined = str(folder_path / exe_name)
            attempted.append(joined)
            found = _exists_file(joined)
            if found:
                return found, joined

    # 4. Multiple exe names (primary + fallbacks)
    exes_to_try: List[str] = []
    if exe_name:
        exes_to_try.append(exe_name)
    for fb in app.get("fallback_exes") or []:
        if fb not in exes_to_try:
            exes_to_try.append(fb)

    if folder and exes_to_try:
        for name in exes_to_try:
            joined = str(Path(folder) / name)
            attempted.append(joined)
            found = _exists_file(joined)
            if found:
                return found, joined

    # 5. Glob search
    search_glob = (app.get("search_glob") or "").strip()
    if search_glob:
        for hit in _search_glob(search_glob):
            attempted.append(hit)
            return hit, hit

    # 6. PATH fallback for known apps
    if app_id == "cursor":
        for name in ("cursor", "Cursor"):
            found = _which_exe(name)
            if found:
                attempted.append(found)
                return found, f"which:{name}"

    return None, ""


def _resolve_url(app: Dict[str, Any]) -> Tuple[Optional[str], str]:
    url = (app.get("url") or "").strip()
    if url and urlparse(url).scheme in ("http", "https"):
        return url, "url"
    return None, ""


def _resolve_uri(app: Dict[str, Any]) -> Tuple[Optional[str], str]:
    uri = (app.get("uri") or "").strip()
    if uri and _is_allowed_uri(uri):
        return uri, "uri"
    return None, ""


def _resolve_store_app(app: Dict[str, Any], attempted: List[str]) -> Tuple[Optional[str], str]:
    uri = (app.get("uri") or "").strip()
    if uri:
        attempted.append(uri)
        if _is_allowed_uri(uri):
            return uri, "uri"
    path = (app.get("path") or "").strip()
    if path:
        expanded = _expand_path(path)
        attempted.append(expanded)
        found = _exists_file(expanded)
        if found:
            return found, expanded
    return None, ""


def _is_allowed_uri(uri: str) -> bool:
    scheme = (urlparse(uri).scheme or "").lower()
    return bool(scheme and scheme in _ALLOWED_URI_SCHEMES)


# ── public resolvers ─────────────────────────────────────

def resolve_app_entry(app: Dict[str, Any]) -> Dict[str, Any]:
    app_id = (app.get("id") or "").lower()
    display = app.get("display_name") or app_id
    app_type = (app.get("type") or "").lower()
    attempted: List[str] = []
    target: Optional[str] = None
    source = ""
    message = ""

    if app_type == "direct_exe":
        raw_path = _expand_path(app.get("path") or "")
        if raw_path:
            attempted.append(raw_path)
            found = _exists_file(raw_path)
            if found:
                target, source = found, raw_path
            else:
                message = f"Executable not found: {raw_path}"
    elif app_type == "folder_exe":
        target, source = _resolve_folder_exe(app, attempted)
        if not target:
            exe = app.get("exe") or "executable"
            message = f"{exe} was not found."
    elif app_type == "windows_builtin":
        target, source = _resolve_windows_builtin(app, attempted)
        if not target:
            message = f"{app.get('exe') or 'Application'} was not found."
    elif app_type == "url":
        target, source = _resolve_url(app)
        if not target:
            message = f"URL for {display} is not configured."
    elif app_type == "uri":
        target, source = _resolve_uri(app)
        if not target:
            message = f"URI for {display} is not configured or not allowed."
    elif app_type == "store_app":
        target, source = _resolve_store_app(app, attempted)
        if not target:
            message = _STORE_APP_HINTS.get(app_id, f"{display} is not installed or not registered.")
    else:
        message = f"Unknown app type '{app_type}' for {display}."

    available = bool(target)
    resolved_path = (
        target
        if app_type in ("direct_exe", "folder_exe", "windows_builtin", "store_app")
        and target
        and not str(target).endswith(":")
        else None
    )
    if app_type in ("url", "uri", "store_app") and target and (str(target).startswith("http") or ":" in str(target)):
        resolved_path = target

    return {
        "id": app_id,
        "display_name": display,
        "type": app_type,
        "available": available,
        "ok": available,
        "path": resolved_path,
        "resolved_target": target,
        "source": source,
        "aliases": list(app.get("aliases") or []),
        "message": message if not available else f"Resolved {display}",
        "attempted_paths": attempted,
        "enabled": app.get("enabled", True),
    }


def resolve_app(app_name: str) -> Dict[str, Any]:
    key = (app_name or "").strip().lower()
    app_id = resolve_alias(key) or key
    app = get_app_by_id(app_id)
    if not app:
        return {
            "ok": False,
            "app": app_id,
            "available": False,
            "path": None,
            "source": "",
            "message": f"App '{app_id}' is not configured. Add it in desktop_bridge/apps.json.",
            "attempted_paths": [],
        }
    if not app.get("enabled", True):
        display = app.get("display_name") or app_id
        return {
            "ok": False,
            "app": app_id,
            "available": False,
            "path": None,
            "source": "",
            "message": f"{display} is disabled. Enable it in apps.json.",
            "attempted_paths": [],
            "enabled": False,
        }
    result = resolve_app_entry(app)
    result["app"] = app_id
    return result


def resolve_all_apps() -> Dict[str, Any]:
    apps: Dict[str, Any] = {}
    available: List[str] = []
    missing: List[str] = []
    for app in load_registry():
        app_id = (app.get("id") or "").lower()
        if not app_id:
            continue
        info = resolve_app_entry(app)
        apps[app_id] = info
        if info.get("available"):
            available.append(app_id)
        else:
            missing.append(app_id)
    return {
        "ok": True,
        "app_count": len(apps),
        "available_apps": available,
        "missing_apps": missing,
        "apps": apps,
    }


def pick_default_browser() -> Dict[str, Any]:
    for app_id in ("brave", "chrome", "edge", "msedge"):
        res = resolve_app(app_id)
        if res.get("ok"):
            res["browser_id"] = app_id
            return res
    return {"ok": False, "message": "No browser found. Install Brave, Chrome, or Edge, or set env override."}
