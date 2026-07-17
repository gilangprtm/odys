"""cli/audio.py — odys say / listen (TTS + STT)."""

import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from cli.utils import BRIDGE_URL, SERVER_URL, bridge_token, find_process_on_port, wait_port


def http_json(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict | None = None,
    timeout: int = 60,
) -> dict:
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

    token = bridge_token()
    if not token:
        print("❌ Bridge token tidak ditemukan.")
        print("   💡 Jalankan: odys bridge")
        print("   💡 Atau set:  ODY_BRIDGE_TOKEN=... (harus sama dengan proses bridge)")
        print("   💡 Cek:       odys doctor")
        return 1

    if not find_process_on_port(8765) and not wait_port(8765, timeout=1):
        print("❌ Desktop Bridge tidak berjalan di port 8765.")
        print("   💡 Jalankan: odys bridge   atau   odys start")
        print("   💡 Cek:      odys doctor")
        return 1

    print(f'🔊 Mengucapkan: "{text}"')
    try:
        out = http_json(
            "POST",
            f"{BRIDGE_URL.rstrip('/')}/tts",
            payload={"text": text},
            headers={"X-Odys-Bridge-Token": token},
            timeout=120,
        )
    except RuntimeError as exc:
        msg = str(exc)
        print(f"❌ TTS gagal: {msg}")
        if "401" in msg or "Invalid bridge token" in msg:
            print("   💡 Token mismatch — bridge pakai token beda dari CLI.")
            print("   💡 Perbaiki: odys stop && odys bridge")
            print("   💡 Atau set ODY_BRIDGE_TOKEN sama di kedua sisi, lalu restart.")
        elif "503" in msg or "not configured" in msg:
            print("   💡 Bridge token kosong di host. Restart: odys bridge")
        elif "Tidak bisa hubungi" in msg or "Connection" in msg:
            print("   💡 Bridge down. Jalankan: odys bridge")
        print("   💡 Cek: odys doctor")
        return 1

    if out.get("ok"):
        engine = out.get("engine") or "?"
        print(f"  ✅ {out.get('message') or 'OK'} (engine: {engine})")
        return 0
    print(f"  ❌ {out.get('message') or out}")
    return 1


def record_mic_wav(seconds: int = 5) -> Path | None:
    """Rekam mic Windows.

    Urutan:
      1. sounddevice (paling andal, pip install sounddevice)
      2. ffmpeg dshow (kalau ada)
      3. PowerShell winmm MCI waveaudio
    """
    out_path = Path(tempfile.gettempdir()) / f"odys_listen_{int(time.time())}.wav"
    errors: list[str] = []

    # 1) sounddevice
    try:
        import sounddevice as sd
        import wave

        rate = 16000
        channels = 1
        print("  🎙️  Engine: sounddevice")
        frames = sd.rec(
            int(seconds * rate),
            samplerate=rate,
            channels=channels,
            dtype="int16",
        )
        sd.wait()
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(frames.tobytes())
        if out_path.is_file() and out_path.stat().st_size > 44:
            return out_path
        errors.append("sounddevice: file kosong")
    except ImportError:
        errors.append("sounddevice: tidak terinstall")
    except Exception as exc:
        errors.append(f"sounddevice: {exc}")

    # 2) ffmpeg — list dshow devices, pick first audio
    try:
        list_out = subprocess.run(
            ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        import re

        devices = re.findall(
            r'\"([^\"]+)\"\s+\(audio\)',
            (list_out.stderr or "") + (list_out.stdout or ""),
            flags=re.I,
        )
        if not devices:
            devices = re.findall(
                r'\[dshow[^\]]*\]\s+\"([^\"]+)\"',
                list_out.stderr or "",
            )
        mic_name = devices[0] if devices else "Microphone"
        print(f"  🎙️  Engine: ffmpeg dshow ({mic_name})")
        ff = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "dshow",
                "-i",
                f"audio={mic_name}",
                "-t",
                str(seconds),
                "-ac",
                "1",
                "-ar",
                "16000",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            timeout=seconds + 20,
        )
        if ff.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 44:
            return out_path
        errors.append(f"ffmpeg: {(ff.stderr or '')[-200:]}")
    except FileNotFoundError:
        errors.append("ffmpeg: tidak ada di PATH")
    except (subprocess.TimeoutExpired, OSError) as exc:
        errors.append(f"ffmpeg: {exc}")

    # 3) PowerShell MCI (winmm)
    out_ps = str(out_path).replace("'", "''")
    ps = f"""
$ErrorActionPreference = 'Stop'
$out = '{out_ps}'
$seconds = {int(seconds)}
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class OdysMci {{
  [DllImport("winmm.dll", CharSet=CharSet.Ansi)]
  public static extern int mciSendString(string cmd, StringBuilder ret, int len, IntPtr cb);
  public static string Send(string cmd) {{
    var sb = new StringBuilder(512);
    int r = mciSendString(cmd, sb, sb.Capacity, IntPtr.Zero);
    if (r != 0) throw new Exception("MCI " + r + " for: " + cmd + " -> " + sb.ToString());
    return sb.ToString();
  }}
}}
"@
[OdysMci]::Send('open new type waveaudio alias odysmic')
try {{
  [OdysMci]::Send('set odysmic time format ms bitspersample 16 channels 1 samplespersec 16000 bytespersec 32000 alignment 2')
  [OdysMci]::Send('record odysmic')
  Start-Sleep -Seconds $seconds
  [OdysMci]::Send('stop odysmic')
  if (Test-Path $out) {{ Remove-Item -Force $out }}
  [OdysMci]::Send("save odysmic `"$out`"")
}} finally {{
  try {{ [OdysMci]::Send('close odysmic') }} catch {{}}
}}
if (-not (Test-Path $out)) {{ throw 'WAV missing after MCI save' }}
$len = (Get-Item $out).Length
if ($len -lt 100) {{ throw "WAV too small: $len bytes" }}
"""
    try:
        print("  🎙️  Engine: PowerShell MCI")
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=seconds + 25,
        )
        if completed.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 44:
            return out_path
        err = (completed.stderr or completed.stdout or "record failed").strip()
        errors.append(f"MCI: {err[:300]}")
    except (subprocess.TimeoutExpired, OSError) as exc:
        errors.append(f"MCI: {exc}")

    for e in errors:
        print(f"  ⚠️  {e}")
    print("  💡 Saran: pip install sounddevice")
    print("  💡 Atau install ffmpeg + pastikan mic default Windows aktif")
    return None


def post_multipart_stt(wav_path: Path) -> dict:
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

    if not find_process_on_port(7000) and not wait_port(7000, timeout=1):
        print("⚠️  Server utama (port 7000) sepertinya tidak jalan — STT mungkin gagal.")
        print("   💡 Jalankan: odys start")
        print("   💡 Cek:      odys doctor")

    print(f"🎤 Merekam {seconds}s... (bicara sekarang)")
    wav = record_mic_wav(seconds)
    if not wav:
        print("❌ Gagal merekam dari microphone.")
        print("   💡 Cek device mic di Windows Sound settings")
        print("   💡 Saran: pip install sounddevice")
        print("   💡 Atau install ffmpeg")
        print("   💡 Cek: odys doctor")
        return 1

    print(f"  📼 File: {wav} ({wav.stat().st_size} bytes)")
    print("  🧠 Mengirim ke STT...")
    try:
        out = post_multipart_stt(wav)
    except RuntimeError as exc:
        msg = str(exc)
        print(f"❌ STT gagal: {msg}")
        if "Tidak bisa hubungi" in msg or "Connection" in msg or "10061" in msg:
            print("   💡 Server tidak jalan atau STT endpoint tidak aktif.")
            print("   💡 Jalankan: odys start")
            print("   💡 Cek:      odys doctor")
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
