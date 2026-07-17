"""cli/server.py — Odys main server start/stop."""

import os
import subprocess
import sys

from cli.utils import ROOT, load_pids, process_exists, save_pids, wait_port


def cmd_server_start(args):
    """Jalankan server utama Odys."""
    req_file = ROOT / "requirements.txt"
    if req_file.exists():
        try:
            import pyotp  # noqa: F401
        except ImportError:
            print("! Menginstall dependensi server...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                cwd=ROOT,
                capture_output=True,
                timeout=120,
            )

    port = int(os.environ.get("APP_PORT", "7000"))
    print(f"Memulai server Odys di http://127.0.0.1:{port}...")

    pids = load_pids()
    env = {**os.environ}
    token = env.get("ODY_BRIDGE_TOKEN") or pids.get("bridge_token") or ""
    if token:
        env["ODY_BRIDGE_TOKEN"] = token
    env.setdefault("ODY_BRIDGE_URL", "http://127.0.0.1:8765")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if wait_port(port, timeout=15):
        pids = load_pids()
        pids["server"] = proc.pid
        pids["server_port"] = port
        if token:
            pids["bridge_token"] = token
        save_pids(pids)
        print(f"  ✅ Server aktif (PID {proc.pid})")
        print(f"  📡 http://127.0.0.1:{port}")
    else:
        print("  ❌ Server gagal start dalam 15 detik")
        proc.kill()


def cmd_server_stop():
    pids = load_pids()
    pid = pids.get("server")
    if pid and process_exists(pid):
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
            print(f"  ✅ Server (PID {pid}) dimatikan")
        except Exception:
            print(f"  ❌ Gagal matikan server (PID {pid})")
        pids.pop("server", None)
        pids.pop("server_port", None)
        save_pids(pids)
    else:
        print("  ℹ️  Server tidak berjalan")
