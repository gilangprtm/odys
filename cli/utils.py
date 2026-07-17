"""cli/utils.py — shared constants + process/PID helpers for Odys CLI."""

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = ROOT / "desktop_bridge"
BRIDGE_SCRIPT = BRIDGE_DIR / "desktop_bridge.py"
PID_FILE = ROOT / ".odys_pids.json"
BRIDGE_URL = os.environ.get("ODY_BRIDGE_URL", "http://127.0.0.1:8765")
SERVER_URL = os.environ.get("ODY_SERVER_URL", "http://127.0.0.1:7000")


def load_pids() -> dict:
    if PID_FILE.exists():
        try:
            return json.loads(PID_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_pids(pids: dict):
    PID_FILE.write_text(json.dumps(pids, indent=2))
    PID_FILE.chmod(0o600)


def find_process_on_port(port: int) -> int | None:
    """Cari PID yg listen di port tertentu (Windows)."""
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-Command",
                f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess",
            ],
            text=True,
            timeout=5,
        ).strip()
        if out:
            return int(out.splitlines()[0])
    except (subprocess.CalledProcessError, ValueError, OSError):
        pass
    return None


def process_exists(pid: int) -> bool:
    """Cek apakah process dengan PID masih hidup (Windows-compatible)."""
    if not pid:
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x400, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def wait_port(port: int, timeout: int = 8) -> bool:
    """Tunggu sampai port merespon."""
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.5)
    return False


def generate_token() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def bridge_token() -> str:
    """Resolve bridge auth token: env first, then saved pids."""
    env_token = (os.environ.get("ODY_BRIDGE_TOKEN") or "").strip()
    if env_token:
        return env_token
    pids = load_pids()
    return str(pids.get("bridge_token") or "").strip()
