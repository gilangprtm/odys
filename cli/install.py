"""cli/install.py — odys install."""

import os
import shutil
import subprocess as sp
import sys

from cli.utils import BRIDGE_DIR, ROOT


def cmd_install(args):
    """Cek prerequisite, install dependensi, tambah PATH."""
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
        sp.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        print("  pip      : ✅")
    except Exception:
        print("  pip      : ❌ (tidak ditemukan)")
        ok = False

    # 3. Docker (optional)
    docker_found = shutil.which("docker") or shutil.which("docker.exe")
    if docker_found:
        print(f"  Docker   : ✅ ({docker_found})")
    else:
        print("  Docker   : ⚠️  (opsional — hanya untuk container mode)")

    # 4. Install dependencies
    print()
    print("Menginstall dependensi...")

    req_file = ROOT / "requirements.txt"
    if req_file.exists():
        print("  📦 pip install -r requirements.txt ...")
        r = sp.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if r.returncode == 0:
            print("  ✅ Dependensi server terinstall")
        else:
            print(f"  ❌ Gagal: {r.stderr[-200:]}" if r.stderr else "  ❌ Gagal")
            ok = False

    bridge_req = BRIDGE_DIR / "requirements.txt"
    if bridge_req.exists():
        print("  📦 pip install -r desktop_bridge/requirements.txt ...")
        r = sp.run(
            [sys.executable, "-m", "pip", "install", "-r", str(bridge_req)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode == 0:
            print("  ✅ Dependensi bridge terinstall")
        else:
            print(f"  ❌ Gagal: {r.stderr[-200:]}" if r.stderr else "  ❌ Gagal")
            ok = False

    # 5. PATH
    print()
    print("Menambahkan PATH...")
    odys_dir = str(ROOT)
    try:
        current_path = os.environ.get("PATH", "")
        entries = [p.strip().lower() for p in current_path.split(";") if p.strip()]
        if odys_dir.lower() not in entries:
            sp.run(
                [
                    "powershell",
                    "-Command",
                    f"[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';{odys_dir}', 'User')",
                ],
                capture_output=True,
                timeout=10,
                check=True,
            )
            print(f"  ✅ PATH ditambahkan: {odys_dir}")
            print("  💡 Buka terminal BARU agar PATH efektif")
        else:
            print("  ℹ️  PATH sudah ada")
    except Exception as exc:
        print(f"  ⚠️  Gagal tambah PATH otomatis: {exc}")
        print("  💡 Tambah manual: Tambahkan folder berikut ke PATH user:")
        print(f"      {odys_dir}")

    # 6. Odys-Vault
    print()
    print("Membuat Odys-Vault...")
    try:
        try:
            from services.odys_vault import ensure_odys_vault
        except ImportError:
            sys.path.insert(0, str(ROOT))
            from services.odys_vault import ensure_odys_vault

        result = ensure_odys_vault()
        print(f"  📁 {result['path']}")
        if result.get("existed") and not result.get("created"):
            print("  ℹ️  Vault sudah ada")
        else:
            n = len(result.get("created") or [])
            print(f"  ✅ Vault dibuat ({n} item baru)")
            for item in (result.get("created") or [])[:12]:
                print(f"     · {item}")
        print(f"  ⚙️  Config: {result.get('config_path')}")
    except Exception as exc:
        print(f"  ❌ Gagal buat vault: {exc}")
        ok = False

    # 7. Done
    print()
    if ok:
        print("═══ ✅ Install selesai ═══")
        print("Buka CMD/terminal BARU lalu ketik:")
        print("    odys start")
        print("    odys tray --autostart")
    else:
        print("═══ ⚠️  Install selesai dengan error ═══")
        print("Cek pesan error di atas, perbaiki, lalu jalankan ulang:")
        print("    odys install")
