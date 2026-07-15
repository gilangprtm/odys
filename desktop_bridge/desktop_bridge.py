"""Odys Desktop Bridge — Windows host service for safe desktop actions.

Runs on the Windows host (not in Docker). Accepts authenticated requests
from the Odys container to launch whitelisted desktop apps, open folders,
open URLs, and open projects in Cursor.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel, Field
    import uvicorn
except ImportError as exc:
    print("Install requirements: pip install fastapi uvicorn", file=sys.stderr)
    raise SystemExit(1) from exc

from app_registry import (
    get_app_by_id,
    load_registry,
    pick_default_browser,
    resolve_all_apps,
    resolve_app,
)

# ── defaults ─────────────────────────────────────────────

LOG_PATH = Path(os.environ.get("ODY_BRIDGE_LOG", str(Path.home() / "OdysBridge" / "desktop_bridge.log")))

logger = logging.getLogger("odys-desktop-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_TOKEN = os.environ.get("ODY_BRIDGE_TOKEN", "")
if not DEFAULT_TOKEN:
    import secrets
    DEFAULT_TOKEN = secrets.token_urlsafe(32)
    logger.warning("ODY_BRIDGE_TOKEN not set. Generated ephemeral token: %s", DEFAULT_TOKEN)
    logger.warning("Set ODY_BRIDGE_TOKEN env var to persist across restarts.")
DEFAULT_HOST_WORKSPACE = os.environ.get(
    "ODY_BRIDGE_WORKSPACE",
    str(Path.home() / "Documents" / "Sao-Vault"),
)
PORT = int(os.environ.get("ODY_BRIDGE_PORT", "8765"))

logger = logging.getLogger("odys-desktop-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Odys Desktop Bridge", version="1.0")

_ALLOWED_URI_SCHEMES = frozenset({
    "com.epicgames.launcher",
    "steam",
    "spotify",
    "whatsapp",
    "ms-windows-store",
    "ms-gamingoverlay",
})


# ── models ───────────────────────────────────────────────

class CommandRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=64)
    args: Dict[str, Any] = Field(default_factory=dict)


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


# ── internal helpers ─────────────────────────────────────

def _log_event(entry: Dict[str, Any]) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.warning("Could not write log: %s", exc)


def _norm_win(path: str) -> str:
    return str(Path(path).resolve())


def _allowed_path(path: str) -> bool:
    allowed = DEFAULT_HOST_WORKSPACE.rstrip("\\/")
    try:
        resolved = _norm_win(path)
        allowed_res = _norm_win(allowed)
        return (
            resolved.lower() == allowed_res.lower()
            or resolved.lower().startswith(allowed_res.lower() + "\\")
        )
    except (OSError, ValueError):
        return False


def _registered_app_ids() -> set[str]:
    return {(a.get("id") or "").lower() for a in load_registry() if a.get("enabled", True)}


def _is_allowed_uri(uri: str) -> bool:
    scheme = (urlparse(uri).scheme or "").lower()
    return bool(scheme and scheme in _ALLOWED_URI_SCHEMES)


def _resolve_cmd_exe() -> Optional[str]:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for candidate in (windir / "System32" / "cmd.exe", windir / "SysWOW64" / "cmd.exe"):
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _resolve_explorer_exe() -> Optional[str]:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for candidate in (windir / "explorer.exe", windir / "System32" / "explorer.exe"):
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _run_exe(exe: str, args: Optional[List[str]] = None, cwd: Optional[str] = None) -> Dict[str, Any]:
    cmd = [exe] + (args or [])
    launch_cwd = None
    if cwd:
        try:
            launch_cwd = str(Path(cwd).resolve()) if Path(cwd).is_dir() else None
        except OSError:
            launch_cwd = None
    try:
        subprocess.Popen(cmd, shell=False, cwd=launch_cwd)
        _log_event({"launch": cmd})
        return {"ok": True, "message": "Command started", "cmd": cmd, "exe": exe}
    except OSError as exc:
        logger.error("Launch failed %s: %s", cmd, exc)
        return {"ok": False, "message": str(exc), "cmd": cmd, "attempted_paths": [exe]}


# ── launcher primitives ──────────────────────────────────

def _launch_uri(uri: str, label: str) -> Dict[str, Any]:
    if not _is_allowed_uri(uri):
        return {"ok": False, "message": f"URI scheme not allowed: {uri}"}
    cmd_exe = _resolve_cmd_exe()
    if not cmd_exe:
        return {"ok": False, "message": "Command Prompt was not found.", "attempted_paths": []}
    out = _run_exe(cmd_exe, ["/c", "start", "", uri])
    if out.get("ok"):
        out["message"] = f"Opened {label}."
    return out


def _launch_url(url: str, browser: Optional[str] = None) -> Dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "message": "Only http/https URLs allowed."}

    browser_id = (browser or "").strip().lower()
    if browser_id:
        resolved = resolve_app(browser_id)
        if not resolved.get("ok"):
            return {
                "ok": False,
                "message": resolved.get("message"),
                "attempted_paths": resolved.get("attempted_paths") or [],
            }
        exe = str(resolved.get("path") or "")
        out = _run_exe(exe, [url])
        if out.get("ok"):
            out["message"] = f"Opened URL in {browser_id}."
            out["resolved_path"] = exe
        return out

    browser_res = pick_default_browser()
    if browser_res.get("ok"):
        exe = str(browser_res.get("path") or "")
        out = _run_exe(exe, [url])
        if out.get("ok"):
            out["message"] = "Opened URL in default browser."
            out["resolved_path"] = exe
        return out

    cmd_exe = _resolve_cmd_exe()
    if not cmd_exe:
        return {"ok": False, "message": "No browser or cmd.exe available.", "attempted_paths": []}
    out = _run_exe(cmd_exe, ["/c", "start", "", url])
    if out.get("ok"):
        out["message"] = "Opened URL."
    return out


def _taskkill_image(image_name: str) -> Dict[str, Any]:
    if not image_name or not image_name.lower().endswith(".exe"):
        return {"ok": False, "message": "Invalid process image name."}
    cmd_exe = _resolve_cmd_exe()
    if not cmd_exe:
        return {"ok": False, "message": "Command Prompt was not found."}
    out = _run_exe(cmd_exe, ["/c", "taskkill", "/IM", image_name, "/F"])
    if out.get("ok"):
        out["message"] = f"Closed {image_name}."
    return out


# ── app action helpers ───────────────────────────────────

def _close_registered_app(app_name: str) -> Dict[str, Any]:
    app_id = (app_name or "").strip().lower()
    if app_id not in _registered_app_ids():
        return {"ok": False, "message": f"App '{app_id}' is not configured. Add it in apps.json."}
    resolved = resolve_app(app_id)
    if not resolved.get("ok"):
        return {
            "ok": False,
            "message": resolved.get("message"),
            "app": app_id,
            "attempted_paths": resolved.get("attempted_paths") or [],
        }
    app_def = get_app_by_id(app_id) or {}
    display = app_def.get("display_name") or app_id
    path = str(resolved.get("path") or "")
    image = Path(path).name if path else (app_def.get("exe") or "")
    if not image:
        return {"ok": False, "message": f"No executable mapped for {display}."}
    logger.info("Closing %s via taskkill %s", app_id, image)
    out = _taskkill_image(image)
    out["app"] = app_id
    out["image"] = image
    return out


def _launch_registered_app(app_name: str) -> Dict[str, Any]:
    app_id = (app_name or "").strip().lower()
    if app_id not in _registered_app_ids():
        return {"ok": False, "message": f"App '{app_id}' is not configured. Add it in apps.json."}

    resolved = resolve_app(app_id)
    if not resolved.get("ok"):
        _log_event({"launch_failed": app_id, "reason": resolved.get("message")})
        return {
            "ok": False,
            "message": resolved.get("message"),
            "app": app_id,
            "attempted_paths": resolved.get("attempted_paths") or [],
        }

    app_def = get_app_by_id(app_id) or {}
    app_type = (app_def.get("type") or "").lower()
    display = app_def.get("display_name") or app_id
    target = resolved.get("resolved_target") or resolved.get("path")

    if app_type == "url":
        return _launch_url(str(target))

    if app_type in ("uri", "store_app") and target and _is_allowed_uri(str(target)):
        return _launch_uri(str(target), display)

    if app_type in ("direct_exe", "folder_exe", "windows_builtin") and target:
        exe = str(target)
        extra_args = [str(a) for a in (app_def.get("args") or []) if str(a).strip()]
        workdir = (app_def.get("working_directory") or "").strip() or None
        logger.info("Launching %s via %s (%s)", app_id, exe, resolved.get("source"))
        out = _run_exe(exe, extra_args or None, cwd=workdir)
        if out.get("ok"):
            out["message"] = f"Opened {display}."
            out["resolved_path"] = exe
            out["source"] = resolved.get("source")
            out["app"] = app_id
        return out

    return {
        "ok": False,
        "message": resolved.get("message") or f"Could not launch {display}.",
        "attempted_paths": resolved.get("attempted_paths") or [],
    }


def _launch_cursor_with_path(folder: Optional[str] = None) -> Dict[str, Any]:
    resolved = resolve_app("cursor")
    if not resolved.get("ok"):
        _log_event({"launch_failed": "cursor", "reason": resolved.get("message")})
        return {
            "ok": False,
            "message": resolved.get("message"),
            "app": "cursor",
            "attempted_paths": resolved.get("attempted_paths") or [],
        }
    exe = str(resolved.get("path") or resolved.get("resolved_target") or "")
    args = [_norm_win(folder)] if folder else []
    logger.info("Launching Cursor %s (%s)", exe, resolved.get("source"))
    out = _run_exe(exe, args)
    if out.get("ok"):
        out["message"] = f"Opened Cursor{(' at ' + folder) if folder else ''}."
        out["resolved_path"] = exe
        out["source"] = resolved.get("source")
    return out


# ── TTS helpers ──────────────────────────────────────────

def _speak_sapi_pywin32(text: str) -> Dict[str, Any]:
    """Speak via Windows SAPI through pywin32 (preferred)."""
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return {"ok": False, "message": "pywin32 not installed"}
    try:
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        speaker.Speak(text)
        return {"ok": True, "message": "Spoken via SAPI (pywin32)", "engine": "pywin32"}
    except Exception as exc:
        logger.error("SAPI pywin32 speak failed: %s", exc)
        return {"ok": False, "message": str(exc)}


def _speak_sapi_powershell(text: str) -> Dict[str, Any]:
    """Speak via System.Speech through PowerShell (fallback)."""
    # Escape single quotes for PowerShell single-quoted string
    safe = text.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Speak('{safe}')"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "PowerShell TTS failed").strip()
            logger.error("PowerShell TTS failed: %s", err)
            return {"ok": False, "message": err, "engine": "powershell"}
        return {"ok": True, "message": "Spoken via SAPI (PowerShell)", "engine": "powershell"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "TTS timed out", "engine": "powershell"}
    except OSError as exc:
        logger.error("PowerShell TTS launch failed: %s", exc)
        return {"ok": False, "message": str(exc), "engine": "powershell"}


def _speak_text(text: str) -> Dict[str, Any]:
    """Speak text: pywin32 SAPI first, PowerShell System.Speech fallback."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "message": "Empty text"}
    out = _speak_sapi_pywin32(text)
    if out.get("ok"):
        return out
    logger.info("pywin32 TTS unavailable (%s); falling back to PowerShell", out.get("message"))
    return _speak_sapi_powershell(text)


def _check_bridge_token(
    x_odys_bridge_token: Optional[str] = None,
    x_atlas_bridge_token: Optional[str] = None,
) -> None:
    token = (x_odys_bridge_token or x_atlas_bridge_token or "").strip()
    if not DEFAULT_TOKEN:
        raise HTTPException(status_code=503, detail="Bridge token not configured on host.")
    if token != DEFAULT_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid bridge token.")


# ── FastAPI routes ───────────────────────────────────────

@app.on_event("startup")
def _log_resolved_apps() -> None:
    snapshot = resolve_all_apps()
    for key, info in (snapshot.get("apps") or {}).items():
        if info.get("available"):
            logger.info("Resolved %s → %s (%s)", key, info.get("path"), info.get("source"))
        else:
            logger.warning("Unresolved %s: %s", key, info.get("message"))


@app.get("/health")
def health():
    apps = resolve_all_apps()
    return {
        "ok": True,
        "service": "odys-desktop-bridge",
        "version": "1.0",
        "app_count": apps.get("app_count", 0),
        "available_apps": apps.get("available_apps") or [],
        "missing_apps": apps.get("missing_apps") or [],
        "resolved_apps": {
            k: {
                "available": v.get("available"),
                "ok": v.get("ok"),
                "path": v.get("path"),
                "source": v.get("source"),
                "message": v.get("message"),
            }
            for k, v in (apps.get("apps") or {}).items()
        },
    }


@app.get("/apps")
def list_apps():
    return resolve_all_apps()


@app.post("/command")
def run_command(
    body: CommandRequest,
    x_odys_bridge_token: Optional[str] = Header(None, alias="X-Odys-Bridge-Token"),
    x_atlas_bridge_token: Optional[str] = Header(None, alias="X-Atlas-Bridge-Token"),
):
    # Accept both header names for backward compat
    _check_bridge_token(x_odys_bridge_token, x_atlas_bridge_token)

    cmd = body.command.strip()
    args = body.args or {}
    _log_event({"command": cmd, "args": args})

    if cmd == "open_app":
        app_name = (args.get("app") or args.get("launcher") or "").strip().lower()
        return _launch_registered_app(app_name)

    if cmd == "close_app":
        app_name = (args.get("app") or "").strip().lower()
        return _close_registered_app(app_name)

    if cmd == "open_folder":
        path = (args.get("path") or "").strip()
        if not path or not _allowed_path(path):
            return {"ok": False, "message": "Folder path not allowed."}
        exe = _resolve_explorer_exe()
        if not exe:
            return {"ok": False, "message": "Windows Explorer was not found.", "attempted_paths": []}
        out = _run_exe(exe, [_norm_win(path)])
        if out.get("ok"):
            out["message"] = "Opened folder in Explorer."
        return out

    if cmd == "open_project_in_cursor":
        path = (args.get("path") or "").strip()
        projects_root = _norm_win(DEFAULT_HOST_WORKSPACE.rstrip("\\/"))
        if not path or not _allowed_path(path):
            return {"ok": False, "message": "Project path not allowed."}
        resolved_path = _norm_win(path)
        if not resolved_path.lower().startswith(projects_root.lower()):
            return {"ok": False, "message": f"Project must be under {projects_root}"}
        return _launch_cursor_with_path(resolved_path)

    if cmd == "open_url":
        url = (args.get("url") or "").strip()
        browser = (args.get("browser") or "").strip().lower() or None
        return _launch_url(url, browser)

    return {"ok": False, "message": f"Unknown command: {cmd}"}


@app.post("/tts")
def tts(
    body: TTSRequest,
    x_odys_bridge_token: Optional[str] = Header(None, alias="X-Odys-Bridge-Token"),
    x_atlas_bridge_token: Optional[str] = Header(None, alias="X-Atlas-Bridge-Token"),
):
    """Speak text via Windows SAPI (pywin32) or PowerShell System.Speech fallback."""
    _check_bridge_token(x_odys_bridge_token, x_atlas_bridge_token)
    text = body.text.strip()
    _log_event({"tts": text[:120]})
    out = _speak_text(text)
    if not out.get("ok"):
        raise HTTPException(status_code=500, detail=out.get("message") or "TTS failed")
    return out


# ── entrypoint ───────────────────────────────────────────

def main():
    logger.info("Starting Odys Desktop Bridge on 127.0.0.1:%s", PORT)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
