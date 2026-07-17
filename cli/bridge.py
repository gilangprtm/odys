"""cli/bridge.py — Desktop Bridge start/stop."""

import os
import subprocess
import sys

from cli.utils import (
    BRIDGE_DIR,
    BRIDGE_SCRIPT,
    generate_token,
    load_pids,
    process_exists,
    save_pids,
    wait_port,
)


def install_bridge_deps():
    """Install bridge dependencies kalau belum ada."""
    req = BRIDGE_DIR / "requirements.txt"
    if not req.exists():
        return
    try:
        import fastapi  # noqa: F401
    except ImportError:
        print("! Menginstall dependensi bridge...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req)],
            cwd=BRIDGE_DIR,
            capture_output=True,
            timeout=60,
        )


def cmd_bridge_start(args):
    if BRIDGE_SCRIPT.exists():
        print("Desktop Bridge menyala...")
        install_bridge_deps()
        token = os.environ.get("ODY_BRIDGE_TOKEN") or generate_token()
        env = {**os.environ, "ODY_BRIDGE_TOKEN": token}
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE_SCRIPT)],
            cwd=BRIDGE_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if wait_port(8765):
            pids = load_pids()
            pids["bridge"] = proc.pid
            pids["bridge_port"] = 8765
            pids["bridge_token"] = token
            save_pids(pids)
            print(f"  ✅ Bridge aktif (PID {proc.pid}, token: {token[:12]}...)")
            print("  📡 http://127.0.0.1:8765")
        else:
            print("  ❌ Bridge gagal start dalam 8 detik")
            proc.kill()
    else:
        print("  ⏭️  desktop_bridge/desktop_bridge.py tidak ditemukan")


def cmd_bridge_stop():
    pids = load_pids()
    pid = pids.get("bridge")
    if pid and process_exists(pid):
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
            print(f"  ✅ Bridge (PID {pid}) dimatikan")
        except Exception:
            print(f"  ❌ Gagal matikan bridge (PID {pid})")
        pids.pop("bridge", None)
        pids.pop("bridge_port", None)
        save_pids(pids)
    else:
        print("  ℹ️  Bridge tidak berjalan")
