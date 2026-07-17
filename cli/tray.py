"""cli/tray.py — odys tray (system tray agent)."""

import subprocess
import sys

from cli.utils import ROOT


def cmd_tray(args):
    """System tray icon Δ (background agent).

    Args:
        --autostart: Register Windows startup + launch tray
    """
    tray_script = ROOT / "desktop_tray" / "tray_agent.py"
    if not tray_script.is_file():
        print("❌ desktop_tray/tray_agent.py tidak ditemukan.")
        return 1
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        print(f"❌ Butuh pystray + Pillow: pip install pystray Pillow  ({exc})")
        return 1

    subargs = getattr(args, "subargs", []) or []
    if "--autostart" in subargs:
        import winreg

        exe = sys.executable
        cmd = f'"{exe}" "{tray_script}"'
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                key_path,
                0,
                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
            ) as k:
                winreg.SetValueEx(k, "OdysTray", 0, winreg.REG_SZ, cmd)
            print("✅ Autostart registered — Odys akan start otomatis pas Windows login.")
        except Exception as e:
            print(f"❌ Gagal register autostart: {e}")
            return 1

    print("🔄 Menjalankan system tray agent...")
    print("   Ikon Δ akan muncul di taskbar (dekat jam).")
    print("   Klik kanan untuk menu. Tutup dari menu Exit.")
    print()

    subprocess.Popen(
        [sys.executable, str(tray_script)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )
    return 0
