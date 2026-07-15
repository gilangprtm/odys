#!/usr/bin/env python3
"""Odys Tray Agent — system tray icon Δ (Windows).

Monitor bridge (port 8765) + server (port 7000) health.
Quick actions: Buka UI, Stop, Say, Restart Bridge, Listen toggle.
Ikon berubah warna: hijau = semuanya OK, merah = ada mati.
Minimize to tray — gak perlu browser terus-terusan.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont

# ── Paths ────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
PID_FILE = ROOT / ".odys_pids.json"
ODYS_PY = ROOT / "odys.py"

BRIDGE_PORT = 8765
SERVER_PORT = 7000
POLL_INTERVAL = 5  # seconds

# ── Icons ────────────────────────────────────────────────

def _make_icon(hue: str = "#00b3a0", status: str = "ok") -> Image.Image:
    """Generate Δ icon in memory. status: ok|warn|err."""
    fills = {"ok": hue, "warn": "#f0ad4e", "err": "#d9534f"}
    fill_hex = fills.get(status, "#888")
    fill_rgb = _parse_hex(fill_hex)
    sz = 48
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Circle background (semi-transparent)
    margin = 2
    draw.ellipse(
        [margin, margin, sz - margin, sz - margin],
        fill=(*fill_rgb, 40),
    )
    # Delta letter
    try:
        fnt = ImageFont.truetype("segoeui.ttf", 28)
    except OSError:
        fnt = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "Δ", font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (sz - tw) // 2
    ty = (sz - th) // 2 - 1
    draw.text((tx, ty), "Δ", fill=fill_hex, font=fnt)
    return img


def _parse_hex(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


# ── Health check ─────────────────────────────────────────

def _http_get(url: str, timeout: float = 3) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "odys-tray/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _port_occupied(port: int) -> bool:
    """Cheap port check via netstat."""
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            if f"127.0.0.1:{port}" in line or f"0.0.0.0:{port}" in line:
                if "LISTENING" in line:
                    return True
        return False
    except Exception:
        return False


def _load_pids() -> dict:
    if PID_FILE.exists():
        try:
            return json.loads(PID_FILE.read_text())
        except Exception:
            return {}
    return {}


# ── Tray state ───────────────────────────────────────────

class TrayApp:
    def __init__(self):
        self.icon = None
        self._stop = threading.Event()
        self._listening = False  # placeholder for voice toggle

    def run(self):
        icon_img = _make_icon(status="warn")  # startup neutral
        menu = self._build_menu()

        self.icon = pystray.Icon(
            "odys-tray",
            icon_img,
            "Odys — loading...",
            menu,
        )
        # Start background poll thread
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        self.icon.run()

    def stop(self):
        self._stop.set()
        if self.icon:
            self.icon.stop()

    # ── Menu ─────────────────────────────────────────

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("🖥  Buka UI", self._open_ui),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"🔊 Say...", self._say_dialog),
            pystray.MenuItem(
                f"{'⏹' if self._listening else '🎤'} Listen",
                self._toggle_listen,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "⟳ Restart Bridge",
                lambda: self._run_odys("stop && odys bridge"),
            ),
            pystray.MenuItem("⏹  Stop All", lambda: self._run_odys("stop")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌ Exit", self.stop),
        )

    def _update_menu(self):
        if self.icon:
            self.icon.menu = self._build_menu()

    # ── Actions ───────────────────────────────────────

    def _open_ui(self):
        port = _load_pids().get("server_port", 7000)
        subprocess.Popen(["start", f"http://127.0.0.1:{port}"], shell=True)

    def _say_dialog(self):
        """Windows popup input → POST /tts"""
        try:
            import tkinter as tk
            from tkinter import simpledialog
            root = tk.Tk()
            root.withdraw()
            text = simpledialog.askstring("Odys Say", "Teks untuk diucapkan:")
            root.destroy()
            if text:
                self._run_odys(f'say "{text}"')
        except Exception:
            pass

    def _toggle_listen(self):
        self._listening = not self._listening
        if self._listening:
            self._run_odys_bg("listen 8")
        self._update_menu()

    def _run_odys(self, cmd: str):
        """Run odys CLI in a hidden window."""
        subprocess.Popen(
            [sys.executable, str(ODYS_PY)] + cmd.split(),
            cwd=ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def _run_odys_bg(self, cmd: str):
        """Run odys CLI detached (no console)."""
        subprocess.Popen(
            [sys.executable, str(ODYS_PY)] + cmd.split(),
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )

    # ── Poll loop ─────────────────────────────────────

    def _poll_loop(self):
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(POLL_INTERVAL)

    def _tick(self):
        bridge = _port_occupied(BRIDGE_PORT)
        server = _port_occupied(SERVER_PORT)
        pids = _load_pids()
        bt = pids.get("bridge_token") or os.environ.get("ODY_BRIDGE_TOKEN") or ""

        # Determine status
        if bridge and server and bt:
            status = "ok"
            tooltip = "✅ All online"
        elif bridge and not server:
            status = "warn"
            tooltip = "⚠️  Bridge jalan, server mati"
        elif server and not bridge:
            status = "warn"
            tooltip = "⚠️  Server jalan, bridge mati"
        elif not bridge and not server:
            status = "err"
            tooltip = "❌ Offline — jalankan odys start"
        else:
            status = "warn"
            tooltip = "⚠️  Sebagian mati"

        if bridge and not bt:
            status = "warn"
            tooltip = "⚠️  Bridge tanpa token"

        # Update icon
        if self.icon:
            icon_img = _make_icon(status=status)
            self.icon.icon = icon_img
            self.icon.title = f"Odys — {tooltip}"

        # If bridge just came online and token available, auto-start listen if toggled
        # (placeholder for wake-word)


# ── Entry ────────────────────────────────────────────────

def main():
    app = TrayApp()
    try:
        app.run()
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    main()
