#!/usr/bin/env python3
"""Odys Tray Agent — system tray icon Δ (Windows + wake word Sira).

Monitor bridge (port 8765) + server (port 7000) health.
Quick actions: Buka UI, Stop, Say, Wake word toggle, Autostart toggle.
Ikon berubah warna: hijau = OK, merah = offline.
Wake word "Sira..." — detect via Vosk offline, otomatis rekam+STT+TTs.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont

# ── Logging ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[odys-tray] %(levelname)s %(message)s",
)
log = logging.getLogger("tray")

# ── Paths ────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
PID_FILE = ROOT / ".odys_pids.json"
ODYS_PY = ROOT / "odys.py"
WAKE_MODULE = ROOT / "desktop_tray" / "wake_word.py"

BRIDGE_PORT = 8765
SERVER_PORT = 7000
POLL_INTERVAL = 5  # seconds

# ── Registry autostart ───────────────────────────────────

_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "OdysTray"


def _autostart_set(enable: bool):
    """Add/remove OdysTray from Windows startup."""
    import winreg
    exe = sys.executable
    script = ROOT / "odys.py"
    cmd = f'"{exe}" "{script}" tray'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0,
                           winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, _AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
    except Exception as e:
        log.warning("Autostart %s: %s", "enable" if enable else "disable", e)


def _autostart_status() -> bool:
    """Check if OdysTray is in startup registry."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0,
                           winreg.KEY_QUERY_VALUE) as k:
            try:
                winreg.QueryValueEx(k, _AUTOSTART_NAME)
                return True
            except FileNotFoundError:
                return False
    except Exception:
        return False


# ── Icons ────────────────────────────────────────────────


def _make_icon(hue: str = "#00b3a0", status: str = "ok") -> Image.Image:
    """Generate Δ icon. status: ok|warn|err|listen."""
    fills = {"ok": hue, "warn": "#f0ad4e", "err": "#d9534f", "listen": "#5bc0de"}
    fill_hex = fills.get(status, "#888")
    fill_rgb = _parse_hex(fill_hex)
    sz = 48
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Circle background
    margin = 2
    draw.ellipse([margin, margin, sz - margin, sz - margin], fill=(*fill_rgb, 40))

    # Delta + wave indicator when listening
    try:
        fnt = ImageFont.truetype("segoeui.ttf", 26)
    except OSError:
        fnt = ImageFont.load_default()
    label = "🎤" if status == "listen" else "Δ"
    bbox = draw.textbbox((0, 0), label, font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (sz - tw) // 2
    ty = (sz - th) // 2 - 1
    draw.text((tx, ty), label, fill=fill_hex, font=fnt)
    return img


def _parse_hex(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


# ── Health check ─────────────────────────────────────────


def _port_occupied(port: int) -> bool:
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=5,
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


# ── Tray app ─────────────────────────────────────────────


class TrayApp:
    def __init__(self):
        self.icon = None
        self._stop = threading.Event()
        self._wake_active = False
        self._wake_detector = None
        self._stt_queue: queue.Queue = queue.Queue()

    def run(self):
        icon_img = _make_icon(status="warn")
        self.icon = pystray.Icon(
            "odys-tray", icon_img, "Odys — loading...", self._build_menu(),
        )
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        # STT processing thread
        stt_t = threading.Thread(target=self._stt_worker, daemon=True)
        stt_t.start()
        self.icon.run()

    def stop(self):
        self._stop.set()
        self._wake_stop()
        if self.icon:
            self.icon.stop()

    # ── Menu ─────────────────────────────────────────

    def _build_menu(self):
        astat = _autostart_status()
        return pystray.Menu(
            pystray.MenuItem("🖥  Buka UI", self._open_ui),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"🔊 Say...", self._say_dialog),
            pystray.MenuItem(
                f"{'⏹' if self._wake_active else '🎤'} Wake: Sira...",
                self._toggle_wake,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                f"{'✅' if astat else '⬜'} Autostart",
                lambda: self._toggle_autostart(),
            ),
            pystray.MenuItem("⟳ Restart Bridge", lambda: self._run_odys("stop && odys bridge")),
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

    def _toggle_autostart(self):
        new_state = not _autostart_status()
        _autostart_set(new_state)
        self._update_menu()

    def _toggle_wake(self):
        if self._wake_active:
            self._wake_stop()
        else:
            self._wake_start()
        self._update_menu()

    def _wake_start(self):
        """Start Vosk wake word detector in background."""
        try:
            sys.path.insert(0, str(ROOT / "desktop_tray"))
            from wake_word import WakeWordDetector
        except ImportError:
            log.error("wake_word.py not found")
            return

        def on_wake():
            log.info("Sira detected! Starting listen...")
            self._wake_callback()

        self._wake_detector = WakeWordDetector(
            on_detected=on_wake,
            keyphrase="sira",
        )
        if not self._wake_detector.available:
            log.warning("Model unavailable — wake word disabled")
            self._wake_detector = None
            return

        self._wake_active = True
        self._set_status_icon("listen")
        self._wake_detector.start()
        log.info("Wake word active — say 'Sira...'")

    def _wake_stop(self):
        if self._wake_detector:
            self._wake_detector.stop()
            self._wake_detector = None
        self._wake_active = False
        self._set_status_icon("ok")
        log.info("Wake word deactivated")

    def _wake_callback(self):
        """Wake word detected: record 5s → STT → post to chat."""
        self._set_status_icon("listen")
        threading.Thread(target=self._record_and_stt, daemon=True).start()

    def _record_and_stt(self):
        """Record 5s audio via sounddevice → POST /api/stt/transcribe."""
        try:
            import sounddevice as sd
            import wave
            import tempfile

            rate = 16000
            log.info("🎤 Recording 5s...")
            frames = sd.rec(int(5 * rate), samplerate=rate, channels=1, dtype="int16")
            sd.wait()

            wav = Path(tempfile.gettempdir()) / f"odys_wake_{int(time.time())}.wav"
            with wave.open(str(wav), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(frames.tobytes())

            log.info(f"  📼 {wav.stat().st_size} bytes — STT...")
            text = self._stt_transcribe(wav)
            if text:
                log.info(f"  ✅ Transkrip: {text}")
                self._process_command(text)
            else:
                log.info("  ⚠️  No speech detected")
            try:
                wav.unlink(missing_ok=True)
            except OSError:
                pass
        except Exception as e:
            log.error(f"Record/STT error: {e}")
        finally:
            self._set_status_icon("listen" if self._wake_active else "ok")

    def _stt_transcribe(self, wav_path: Path) -> str:
        """POST WAV to server STT endpoint."""
        import http.client
        import io

        try:
            boundary = f"----odyswake{int(time.time())}"
            file_bytes = wav_path.read_bytes()
            crlf = b"\r\n"
            body = io.BytesIO()
            body.write(f"--{boundary}".encode() + crlf)
            body.write(f'Content-Disposition: form-data; name="file"; filename="{wav_path.name}"'.encode() + crlf)
            body.write(b"Content-Type: audio/wav" + crlf + crlf)
            body.write(file_bytes)
            body.write(crlf)
            body.write(f"--{boundary}--".encode() + crlf)
            payload = body.getvalue()

            server_url = os.environ.get("ODY_SERVER_URL", "http://127.0.0.1:7000")
            import urllib.parse
            parsed = urllib.parse.urlparse(server_url)
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
            conn.request(
                "POST",
                "/api/stt/transcribe",
                body=payload,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Accept": "application/json",
                },
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            return (data.get("text") or "").strip()
        except Exception as e:
            log.warning(f"STT request failed: {e}")
            return ""

    def _process_command(self, text: str):
        """Route transcribed text: if cmd → run, else TTS echo."""
        text_lower = text.lower().strip()
        log.info(f"Command: {text}")

        # Desktop commands via bridge
        cmd_map = {
            "buka obsidian": ("command", {"command": "open_app", "args": {"app": "obsidian"}}),
            "buka chrome": ("command", {"command": "open_app", "args": {"app": "chrome"}}),
            "buka zcode": ("command", {"command": "open_app", "args": {"app": "zcode"}}),
            "buka terminal": ("command", {"command": "open_app", "args": {"app": "terminal"}}),
            "buka explorer": ("command", {"command": "open_app", "args": {"app": "explorer"}}),
            "matiin chrome": ("command", {"command": "close_app", "args": {"app": "chrome"}}),
            "matikan chrome": ("command", {"command": "close_app", "args": {"app": "chrome"}}),
        }

        for phrase, (action, payload) in cmd_map.items():
            if phrase in text_lower:
                self._bridge_call(action, payload)
                return

        # TTS fallback: echo back
        self._bridge_tts(text)

    def _bridge_call(self, action: str, payload: dict):
        """POST to /api/bridge/command or /tts."""
        token = _load_pids().get("bridge_token") or os.environ.get("ODY_BRIDGE_TOKEN") or ""
        if not token:
            log.warning("No bridge token")
            return
        try:
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
            conn.request(
                "POST", f"/{action}",
                body=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Odys-Bridge-Token": token,
                },
            )
            resp = conn.getresponse()
            log.info(f"Bridge {action}: HTTP {resp.status}")
            conn.close()
        except Exception as e:
            log.warning(f"Bridge call failed: {e}")

    def _bridge_tts(self, text: str):
        """POST text to bridge TTS."""
        token = _load_pids().get("bridge_token") or os.environ.get("ODY_BRIDGE_TOKEN") or ""
        if not token:
            return
        try:
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=30)
            conn.request(
                "POST", "/tts",
                body=json.dumps({"text": text}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Odys-Bridge-Token": token,
                },
            )
            resp = conn.getresponse()
            log.info(f"TTS: {resp.status}")
            conn.close()
        except Exception as e:
            log.warning(f"TTS failed: {e}")

    def _run_odys(self, cmd: str):
        subprocess.Popen(
            [sys.executable, str(ODYS_PY)] + cmd.split(),
            cwd=ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def _set_status_icon(self, status: str):
        if self.icon:
            self.icon.icon = _make_icon(status=status)

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

        if self._wake_active:
            status = "listen"
            tooltip = "🎤 Listening for 'Sira...'"
        elif bridge and server and bt:
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
            tooltip = "❌ Offline"
        else:
            status = "warn"
            tooltip = "⚠️  Sebagian mati"

        if bridge and not bt and not self._wake_active:
            status = "warn"
            tooltip = "⚠️  Bridge tanpa token"

        if self.icon:
            self.icon.icon = _make_icon(status=status)
            self.icon.title = f"Odys — {tooltip}"

    def _stt_worker(self):
        """Background worker for processing STT results."""
        while not self._stop.is_set():
            try:
                text = self._stt_queue.get(timeout=1)
                if text:
                    self._process_command(text)
            except queue.Empty:
                continue


# ── Entry ────────────────────────────────────────────────

def main():
    app = TrayApp()
    try:
        app.run()
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    main()
