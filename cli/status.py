"""cli/status.py — odys status."""

import os

from cli.utils import ROOT, find_process_on_port, load_pids, process_exists


def cmd_status(args):
    pids = load_pids()
    print("Odys Status:")
    print()

    bridge_pid = pids.get("bridge")
    if bridge_pid and process_exists(bridge_pid):
        print(f"  🔵 Bridge    ✅ Aktif   (PID {bridge_pid}, port 8765)")
    else:
        pid = find_process_on_port(8765)
        if pid:
            print(f"  🔵 Bridge    ✅ Aktif   (PID {pid}, port 8765) — via deteksi port")
        else:
            print("  🔵 Bridge    ⚪ Tidak berjalan")

    server_pid = pids.get("server")
    if server_pid and process_exists(server_pid):
        print(f"  🟠 Server    ✅ Aktif   (PID {server_pid}, port {pids.get('server_port', '?')})")
    else:
        pid = find_process_on_port(7000)
        if pid:
            print(f"  🟠 Server    ✅ Aktif   (PID {pid}, port 7000) — via deteksi port")
        else:
            print("  🟠 Server    ⚪ Tidak berjalan")

    print()
    print(f"  📁 Project : {ROOT}")
    token_display = os.environ.get("ODY_BRIDGE_TOKEN") or pids.get("bridge_token", "(auto)")
    print(f"  🔑 Token   : {str(token_display)[:16]}...")
