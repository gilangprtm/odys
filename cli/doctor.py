"""cli/doctor.py — odys doctor / decay."""

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from cli.utils import ROOT, bridge_token, find_process_on_port


def cmd_decay(args):
    """Run neuron edge decay once (forget weak links)."""
    print("═══ Odys Neuron Decay ═══")
    print()
    try:
        try:
            from services.odys_neuron_service import decay, status
        except ImportError:
            sys.path.insert(0, str(ROOT))
            from services.odys_neuron_service import decay, status

        before = status().get("stats") or {}
        print(f"  Before : {before.get('node_count', 0)} nodes · {before.get('edge_count', 0)} edges")
        r = decay()
        after = status().get("stats") or {}
        print(f"  Result : {r.get('message')}")
        print(f"  After  : {after.get('node_count', 0)} nodes · {after.get('edge_count', 0)} edges")
        print(f"  Dropped: {r.get('dropped_edges', 0)} edges · archived {r.get('archived_nodes', 0)} nodes")
        print()
        print("═══ ✅ Decay selesai ═══")
        return 0
    except Exception as exc:
        print(f"  ❌ {exc}")
        return 1


def cmd_doctor(args):
    """Diagnostic: Python, PATH, deps, bridge, token, server, Docker, vault, neurons."""
    if any(a in ("--decay", "decay") for a in (getattr(args, "subargs", None) or [])):
        cmd_decay(args)
        print()

    print("═══ Odys Doctor ═══")
    print()
    ok = True

    # Python
    py = sys.version_info
    print(f"  Python   : {py.major}.{py.minor}.{py.micro} ", end="")
    if py.major >= 3 and py.minor >= 11:
        print("✅")
    else:
        print("❌ (butuh >= 3.11)")
        ok = False

    # pip
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        print("  pip      : ✅")
    except Exception:
        print("  pip      : ❌")
        ok = False

    # PATH
    odys_dir = str(ROOT).lower()
    path_env = os.environ.get("PATH", "").lower()
    if odys_dir in path_env or any(odys_dir in p for p in path_env.split(";")):
        print("  PATH     : ✅ (folder project ada di PATH)")
    else:
        print("  PATH     : ⚠️  (folder project belum di PATH)")
        print("           💡 Jalankan: odys install")
        print(f"           💡 Atau tambah manual: {ROOT}")

    # Critical imports
    print()
    print("  Deps     :")
    for mod, label in (
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("httpx", "httpx"),
        ("pyotp", "pyotp"),
    ):
        try:
            __import__(mod)
            print(f"    {label:12} ✅")
        except ImportError:
            print(f"    {label:12} ❌  → pip install {label}")
            ok = False
    try:
        import win32com.client  # noqa: F401
        print(f"    {'pywin32':12} ✅ (TTS SAPI)")
    except ImportError:
        print(f"    {'pywin32':12} ⚠️  (TTS fallback PowerShell)")

    # Bridge
    print()
    print("  Bridge   :")
    bridge_pid = find_process_on_port(8765)
    if bridge_pid:
        print(f"    Port 8765 : ✅ listening (PID {bridge_pid})")
    else:
        print("    Port 8765 : ❌ tidak ada proses")
        print("               💡 Jalankan: odys bridge   atau   odys start")
        ok = False

    token = (bridge_token() or os.environ.get("ODY_BRIDGE_TOKEN") or "").strip()
    if token:
        print(f"    Token     : ✅ ({token[:10]}...)")
    else:
        print("    Token     : ❌ kosong")
        print("               💡 odys bridge (auto-generate) atau set ODY_BRIDGE_TOKEN")
        ok = False

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8765/health",
            headers={"User-Agent": "odys-doctor"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            print(
                f"    Health    : ✅ {data.get('service')} · "
                f"{data.get('app_count', '?')} apps"
            )
            if token:
                try:
                    auth_req = urllib.request.Request(
                        "http://127.0.0.1:8765/command",
                        data=json.dumps(
                            {"command": "open_app", "args": {"app": "__doctor_probe__"}}
                        ).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Odys-Bridge-Token": token,
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(auth_req, timeout=5) as ar:
                        ar.read()
                    print("    Auth      : ✅ token diterima")
                except urllib.error.HTTPError as he:
                    if he.code == 401:
                        print("    Auth      : ❌ token mismatch (ODY_BRIDGE_TOKEN ≠ bridge)")
                        print("               💡 odys stop && odys bridge  (token baru)")
                        ok = False
                    else:
                        print(f"    Auth      : ✅ (HTTP {he.code} — token valid)")
                except Exception as ae:
                    print(f"    Auth      : ⚠️  {ae}")
    except Exception as exc:
        print(f"    Health    : ❌ {exc}")
        ok = False

    # Server
    print()
    print("  Server   :")
    srv_pid = find_process_on_port(7000)
    if srv_pid:
        print(f"    Port 7000 : ✅ listening (PID {srv_pid})")
    else:
        print("    Port 7000 : ❌ tidak ada proses")
        print("               💡 Jalankan: odys start")
        ok = False

    # Docker optional
    print()
    docker = shutil.which("docker") or shutil.which("docker.exe")
    if docker:
        print(f"  Docker   : ✅ ({docker})")
    else:
        print("  Docker   : ⚠️  (opsional — container mode)")

    # Vault
    print()
    print("  Vault    :")
    try:
        try:
            from services.odys_vault import get_vault_path
        except ImportError:
            sys.path.insert(0, str(ROOT))
            from services.odys_vault import get_vault_path

        vault_path = get_vault_path()
        if vault_path.is_dir():
            items = sum(1 for _ in vault_path.rglob("*") if _.is_file())
            print(f"    Path      : ✅ ({vault_path}) — {items} files")
        else:
            print(f"    Path      : ❌ ({vault_path}) — belum ada")
            print("               💡 odys install")
    except Exception as exc:
        print(f"    Path      : ⚠️  {exc}")

    # Neurons
    print()
    print("  Neurons  :")
    try:
        try:
            from services.odys_neuron_service import status as neuron_status
        except ImportError:
            sys.path.insert(0, str(ROOT))
            from services.odys_neuron_service import status as neuron_status

        st = neuron_status()
        stats = st.get("stats") or {}
        cold = " (cold start)" if stats.get("cold_start") else ""
        print(
            f"    Graph     : ✅ {stats.get('node_count', 0)} nodes · "
            f"{stats.get('edge_count', 0)} edges{cold}"
        )
        by = stats.get("by_type") or {}
        if by:
            print(f"    Types     : {', '.join(f'{k}={v}' for k, v in by.items())}")
        if not by.get("vault_note"):
            print("    Vault sync: 💡 POST /api/odys/neurons/sync-vault  (index Odys-Vault notes)")
    except Exception as exc:
        print(f"    Graph     : ⚠️  {exc}")

    # Tray
    print()
    print("  Tray     :")
    tray_script = ROOT / "desktop_tray" / "tray_agent.py"
    if tray_script.is_file():
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401

            print("    Agent     : ✅ pystray + Pillow ready")
            print(f"    Script    : ✅ {tray_script.name}")
            print("               💡 odys tray  |  odys tray --autostart")
        except ImportError:
            print("    Agent     : ⚠️  pip install pystray Pillow")
    else:
        print("    Agent     : ❌ desktop_tray/tray_agent.py missing")

    # Mic tools
    print()
    print("  Listen   :")
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if ffmpeg:
        print(f"    ffmpeg    : ✅ ({ffmpeg})")
    else:
        print("    ffmpeg    : ⚠️  (opsional — fallback MCI/sounddevice)")
    try:
        import sounddevice  # noqa: F401

        print("    sounddevice: ✅")
    except ImportError:
        print("    sounddevice: ⚠️  (pip install sounddevice — rekam mic lebih andal)")

    print()
    if ok:
        print("═══ ✅ Critical checks passed ═══")
        return 0
    print("═══ ⚠️  Ada issue — perbaiki item ❌ di atas ═══")
    return 1
