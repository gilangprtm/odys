#!/usr/bin/env python3
"""odys — CLI untuk manage Odys.

Pemakaian:
    odys install     Cek prerequisite + install dependensi + tambah PATH
    odys start       Jalankan bridge + server utama
    odys stop        Matikan semua proses
    odys status      Status bridge & server
    odys bridge      Jalankan bridge aja (tanpa server utama)
    odys say <teks>  TTS via Desktop Bridge (Windows SAPI)
    odys listen      Rekam mic → STT server (/api/stt/transcribe)
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRIDGE_DIR = ROOT / "desktop_bridge"
BRIDGE_SCRIPT = BRIDGE_DIR / "desktop_bridge.py"
PID_FILE = ROOT / ".odys_pids.json"
BRIDGE_URL = os.environ.get("ODY_BRIDGE_URL", "http://127.0.0.1:8765")
SERVER_URL = os.environ.get("ODY_SERVER_URL", "http://127.0.0.1:7000")


def _load_pids() -> dict:
    if PID_FILE.exists():
        try:
            return json.loads(PID_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_pids(pids: dict):
    PID_FILE.write_text(json.dumps(pids, indent=2))
    PID_FILE.chmod(0o600)


def _find_process_on_port(port: int) -> int | None:
    """Cari PID yg listen di port tertentu (Windows)."""
    try:
        out = subprocess.check_output(
            ["powershell", "-Command",
             f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess"],
            text=True, timeout=5
        ).strip()
        if out:
            return int(out.splitlines()[0])
    except (subprocess.CalledProcessError, ValueError, OSError):
        pass
    return None


def _process_exists(pid: int) -> bool:
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
def _wait_port(port: int, timeout: int = 8) -> bool:
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


# ── Bridge ───────────────────────────────────────────────

def _generate_token() -> str:
    import secrets
    return secrets.token_urlsafe(32)


def _install_bridge_deps():
    """Install bridge dependencies kalau belum ada."""
    req = BRIDGE_DIR / "requirements.txt"
    if not req.exists():
        return
    try:
        import fastapi  # noqa
    except ImportError:
        print("! Menginstall dependensi bridge...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req)],
            cwd=BRIDGE_DIR, capture_output=True, timeout=60
        )


def cmd_bridge_start(args):
    if BRIDGE_SCRIPT.exists():
        print("Desktop Bridge menyala...")
        _install_bridge_deps()
        token = os.environ.get("ODY_BRIDGE_TOKEN") or _generate_token()
        env = {**os.environ, "ODY_BRIDGE_TOKEN": token}
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE_SCRIPT)],
            cwd=BRIDGE_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if _wait_port(8765):
            pids = _load_pids()
            pids["bridge"] = proc.pid
            pids["bridge_port"] = 8765
            pids["bridge_token"] = token
            _save_pids(pids)
            print(f"  ✅ Bridge aktif (PID {proc.pid}, token: {token[:12]}...)")
            print(f"  📡 http://127.0.0.1:8765")
        else:
            print("  ❌ Bridge gagal start dalam 8 detik")
            proc.kill()
    else:
        print("  ⏭️  desktop_bridge/desktop_bridge.py tidak ditemukan")


def cmd_bridge_stop():
    pids = _load_pids()
    pid = pids.get("bridge")
    if pid and _process_exists(pid):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
            print(f"  ✅ Bridge (PID {pid}) dimatikan")
        except Exception:
            print(f"  ❌ Gagal matikan bridge (PID {pid})")
        pids.pop("bridge", None)
        pids.pop("bridge_port", None)
        _save_pids(pids)
    else:
        print("  ℹ️  Bridge tidak berjalan")


# ── Server utama ─────────────────────────────────────────

def cmd_server_start(args):
    """Jalankan server utama Odysseus."""
    # Cek dependensi dulu
    req_file = ROOT / "requirements.txt"
    if req_file.exists():
        try:
            import pyotp  # noqa — test critical dep
        except ImportError:
            print("! Menginstall dependensi server...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                cwd=ROOT, capture_output=True, timeout=120
            )

    port = int(os.environ.get("APP_PORT", "7000"))
    print(f"Memulai server Odysseus di http://127.0.0.1:{port}...")

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    if _wait_port(port, timeout=15):
        pids = _load_pids()
        pids["server"] = proc.pid
        pids["server_port"] = port
        _save_pids(pids)
        print(f"  ✅ Server aktif (PID {proc.pid})")
        print(f"  📡 http://127.0.0.1:{port}")
    else:
        print("  ❌ Server gagal start dalam 15 detik")
        proc.kill()


def cmd_server_stop():
    pids = _load_pids()
    pid = pids.get("server")
    if pid and _process_exists(pid):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
            print(f"  ✅ Server (PID {pid}) dimatikan")
        except Exception:
            print(f"  ❌ Gagal matikan server (PID {pid})")
        pids.pop("server", None)
        pids.pop("server_port", None)
        _save_pids(pids)
    else:
        print("  ℹ️  Server tidak berjalan")


# ── Start (bridge + server) ─────────────────────────────

def cmd_start(args):
    cmd_bridge_start(args)
    cmd_server_start(args)


# ── Stop ─────────────────────────────────────────────────

def cmd_stop(args):
    cmd_bridge_stop()
    cmd_server_stop()
    print("Odys berhenti.")
    if PID_FILE.exists():
        PID_FILE.unlink()


# ── Status ───────────────────────────────────────────────

def cmd_status(args):
    pids = _load_pids()
    print("Odys Status:")
    print()

    # Bridge
    bridge_pid = pids.get("bridge")
    if bridge_pid and _process_exists(bridge_pid):
        print(f"  🔵 Bridge    ✅ Aktif   (PID {bridge_pid}, port 8765)")
    else:
        pid = _find_process_on_port(8765)
        if pid:
            print(f"  🔵 Bridge    ✅ Aktif   (PID {pid}, port 8765) — via deteksi port")
        else:
            print(f"  🔵 Bridge    ⚪ Tidak berjalan")

    # Server
    server_pid = pids.get("server")
    if server_pid and _process_exists(server_pid):
        print(f"  🟠 Server    ✅ Aktif   (PID {server_pid}, port {pids.get('server_port', '?')})")
    else:
        pid = _find_process_on_port(7000)
        if pid:
            print(f"  🟠 Server    ✅ Aktif   (PID {pid}, port 7000) — via deteksi port")
        else:
            print(f"  🟠 Server    ⚪ Tidak berjalan")

    print()
    print(f"  📁 Project : {ROOT}")
    token_display = os.environ.get("ODY_BRIDGE_TOKEN") or pids.get("bridge_token", "(auto)")
    print(f"  🔑 Token   : {str(token_display)[:16]}...")


# ── Audio (say / listen) ─────────────────────────────────

def _bridge_token() -> str:
    """Resolve bridge auth token: env first, then saved pids."""
    env_token = (os.environ.get("ODY_BRIDGE_TOKEN") or "").strip()
    if env_token:
        return env_token
    pids = _load_pids()
    return str(pids.get("bridge_token") or "").strip()


def _http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None, timeout: int = 60) -> dict:
    data = None
    req_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body:
                return {"ok": True}
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(err_body)
        except json.JSONDecodeError:
            detail = err_body
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Tidak bisa hubungi {url}: {exc.reason}") from exc


def cmd_say(args):
    """Kirim teks ke Desktop Bridge POST /tts (Windows SAPI)."""
    text = " ".join(getattr(args, "subargs", []) or []).strip()
    if not text:
        print("Pemakaian: odys say <teks>")
        print('Contoh:   odys say "selamat pagi tuan"')
        return 1

    token = _bridge_token()
    if not token:
        print("❌ Bridge token tidak ditemukan.")
        print("   Jalankan `odys bridge` dulu, atau set ODY_BRIDGE_TOKEN.")
        return 1

    if not _find_process_on_port(8765) and not _wait_port(8765, timeout=1):
        print("❌ Desktop Bridge tidak berjalan di port 8765.")
        print("   Jalankan: odys bridge")
        return 1

    print(f'🔊 Mengucapkan: "{text}"')
    try:
        out = _http_json(
            "POST",
            f"{BRIDGE_URL.rstrip('/')}/tts",
            payload={"text": text},
            headers={"X-Odys-Bridge-Token": token},
            timeout=120,
        )
    except RuntimeError as exc:
        print(f"❌ TTS gagal: {exc}")
        return 1

    if out.get("ok"):
        engine = out.get("engine") or "?"
        print(f"  ✅ {out.get('message') or 'OK'} (engine: {engine})")
        return 0
    print(f"  ❌ {out.get('message') or out}")
    return 1


def _record_mic_wav(seconds: int = 5) -> Path | None:
    """Rekam mic Windows via PowerShell + System.Media.SoundRecorder / WASAPI fallback.

    Prefer: sounddevice (if installed) → else PowerShell NAudio-free approach
    using Windows Media Foundation via .NET System.Speech is not for recording.
    Use powershell + mciSendString via winmm, or ffmpeg if available.
    """
    out_path = Path(tempfile.gettempdir()) / f"odys_listen_{int(time.time())}.wav"
    # Try ffmpeg first (cleanest)
    try:
        ff = subprocess.run(
            [
                "ffmpeg", "-y", "-f", "dshow",
                "-i", "audio=Microphone",
                "-t", str(seconds),
                "-ac", "1", "-ar", "16000",
                str(out_path),
            ],
            capture_output=True, text=True, timeout=seconds + 15,
        )
        if ff.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 44:
            return out_path
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # PowerShell: record via WASAPI using SoundRecorder-style MCI
    ps = f'''
$ErrorActionPreference = "Stop"
$out = "{str(out_path).replace(chr(92), chr(92)+chr(92))}"
$seconds = {int(seconds)}
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class Mci {{
  [DllImport("winmm.dll", EntryPoint="mciSendStringA", CharSet=CharSet.Ansi)]
  public static extern int Send(string cmd, System.Text.StringBuilder ret, int len, IntPtr hwnd);
}}
"@
$sb = New-Object System.Text.StringBuilder 256
[void][Mci]::Send("open new type waveaudio alias odysmic", $sb, $sb.Capacity, [IntPtr]::Zero)
[void][Mci]::Send("set odysmic time format ms bitspersample 16 channels 1 samplespersec 16000", $sb, $sb.Capacity, [IntPtr]::Zero)
[void][Mci]::Send("record odysmic", $sb, $sb.Capacity, [IntPtr]::Zero)
Start-Sleep -Seconds $seconds
[void][Mci]::Send("stop odysmic", $sb, $sb.Capacity, [IntPtr]::Zero)
[void][Mci]::Send("save odysmic `"$out`"", $sb, $sb.Capacity, [IntPtr]::Zero)
[void][Mci]::Send("close odysmic", $sb, $sb.Capacity, [IntPtr]::Zero)
if (-not (Test-Path $out)) {{ throw "Record failed: $out missing" }}
'''
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=seconds + 20,
        )
        if completed.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 44:
            return out_path
        err = (completed.stderr or completed.stdout or "record failed").strip()
        print(f"  ⚠️  Rekam mic gagal: {err[:300]}")
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"  ⚠️  Rekam mic error: {exc}")
    return None


def _post_multipart_stt(wav_path: Path) -> dict:
    """POST wav ke server STT /api/stt/transcribe."""
    boundary = f"----odys{int(time.time() * 1000)}"
    file_bytes = wav_path.read_bytes()
    filename = wav_path.name
    crlf = bytes([13, 10])
    parts = [
        f"--{boundary}".encode() + crlf,
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode() + crlf,
        b"Content-Type: audio/wav" + crlf + crlf,
        file_bytes,
        crlf,
        f"--{boundary}--".encode() + crlf,
    ]
    body = b"".join(parts)
    url = f"{SERVER_URL.rstrip('/')}/api/stt/transcribe"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {err_body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Tidak bisa hubungi STT {url}: {exc.reason}") from exc


def cmd_listen(args):
    """Rekam mic (default 5s) lalu kirim ke /api/stt/transcribe."""
    seconds = 5
    sub = getattr(args, "subargs", []) or []
    if sub:
        try:
            seconds = max(1, min(60, int(sub[0])))
        except ValueError:
            print("Pemakaian: odys listen [detik]")
            print("Contoh:   odys listen 5")
            return 1

    if not _find_process_on_port(7000) and not _wait_port(7000, timeout=1):
        print("⚠️  Server utama (port 7000) sepertinya tidak jalan — STT mungkin gagal.")
        print("   Jalankan: odys start")

    print(f"🎤 Merekam {seconds}s... (bicara sekarang)")
    wav = _record_mic_wav(seconds)
    if not wav:
        print("❌ Gagal merekam dari microphone.")
        return 1

    print(f"  📼 File: {wav} ({wav.stat().st_size} bytes)")
    print("  🧠 Mengirim ke STT...")
    try:
        out = _post_multipart_stt(wav)
    except RuntimeError as exc:
        print(f"❌ STT gagal: {exc}")
        return 1
    finally:
        try:
            wav.unlink(missing_ok=True)
        except OSError:
            pass

    text = (out.get("text") or "").strip()
    if text:
        print(f"  ✅ Transkrip: {text}")
    else:
        print(f"  ⚠️  Tidak ada teks. Response: {out}")
    return 0 if text else 1


# ── Help ─────────────────────────────────────────────────

def cmd_help(args):
    print(__doc__)


# ── Install ──────────────────────────────────────────────

def cmd_install(args):
    """Cek prerequisite, install dependensi, tambah PATH."""
    import platform
    import shutil
    import subprocess as sp

    ok = True
    print("═══ Odys Install ═══")
    print()

    # 1. Python
    py_ver = sys.version_info
    print(f"  Python   : {py_ver.major}.{py_ver.minor}.{py_ver.micro} ", end="")
    if py_ver.major >= 3 and py_ver.minor >= 10:
        print("✅")
    else:
        print("❌ (minimal Python 3.11)")
        ok = False

    # 2. pip
    try:
        sp.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True, timeout=10)
        print("  pip      : ✅")
    except Exception:
        print("  pip      : ❌ (tidak ditemukan)")
        ok = False

    # 3. Docker (optional — warning aja)
    docker_found = shutil.which("docker") or shutil.which("docker.exe")
    if docker_found:
        print(f"  Docker   : ✅ ({docker_found})")
    else:
        print("  Docker   : ⚠️  (opsional — hanya untuk container mode)")

    # 4. Install dependencies
    print()
    print("Menginstall dependensi...")

    # Server deps
    req_file = ROOT / "requirements.txt"
    if req_file.exists():
        print(f"  📦 pip install -r requirements.txt ...")
        r = sp.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
            capture_output=True, text=True, timeout=180
        )
        if r.returncode == 0:
            print("  ✅ Dependensi server terinstall")
        else:
            print(f"  ❌ Gagal: {r.stderr[-200:]}" if r.stderr else "  ❌ Gagal")
            ok = False

    # Bridge deps
    bridge_req = BRIDGE_DIR / "requirements.txt"
    if bridge_req.exists():
        print(f"  📦 pip install -r desktop_bridge/requirements.txt ...")
        r = sp.run(
            [sys.executable, "-m", "pip", "install", "-r", str(bridge_req)],
            capture_output=True, text=True, timeout=120
        )
        if r.returncode == 0:
            print("  ✅ Dependensi bridge terinstall")
        else:
            print(f"  ❌ Gagal: {r.stderr[-200:]}" if r.stderr else "  ❌ Gagal")
            ok = False

    # 5. Tambah PATH
    print()
    print("Menambahkan PATH...")
    odys_dir = str(ROOT)
    try:
        current_path = os.environ.get("PATH", "")
        entries = [p.strip().lower() for p in current_path.split(";") if p.strip()]
        if odys_dir.lower() not in entries:
            # Tambah via setx (User PATH)
            sp.run(
                ["powershell", "-Command",
                 f"[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';{odys_dir}', 'User')"],
                capture_output=True, timeout=10, check=True
            )
            print(f"  ✅ PATH ditambahkan: {odys_dir}")
            print("  💡 Buka terminal BARU agar PATH efektif")
        else:
            print("  ℹ️  PATH sudah ada")
    except Exception as exc:
        print(f"  ⚠️  Gagal tambah PATH otomatis: {exc}")
        print(f"  💡 Tambah manual: Tambahkan folder berikut ke PATH user:")
        print(f"      {odys_dir}")

    # 6. Selesai
    print()
    if ok:
        print("═══ ✅ Install selesai ═══")
        print("Buka CMD/terminal BARU lalu ketik:")
        print("    odys start")
    else:
        print("═══ ⚠️  Install selesai dengan error ═══")
        print("Cek pesan error di atas, perbaiki, lalu jalankan ulang:")
        print("    odys install")


# ── Entry ────────────────────────────────────────────────

def main():
    """
    Entry point. Kalau script dipanggil langsung (`python odys.py`) atau
    via entry point (`odys` setelah `pip install -e .`).
    """
    parser = argparse.ArgumentParser(
        description="Odys — CLI manager",
        usage="odys <command> [args]"
    )
    parser.add_argument("command", nargs="?", default="help", choices=[
        "install", "start", "stop", "status", "bridge", "say", "listen", "help"
    ])
    parser.add_argument("subargs", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    handlers = {
        "install": cmd_install,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "bridge": cmd_bridge_start,
        "say": cmd_say,
        "listen": cmd_listen,
        "help": cmd_help,
    }

    handler = handlers[args.command]
    rc = handler(args)
    if isinstance(rc, int):
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
