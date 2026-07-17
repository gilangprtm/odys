"""cli/start_stop.py — odys start / stop."""

import sys

from cli.bridge import cmd_bridge_start, cmd_bridge_stop
from cli.server import cmd_server_start, cmd_server_stop
from cli.utils import PID_FILE, ROOT


def cmd_start(args):
    cmd_bridge_start(args)
    cmd_server_start(args)
    # Natural brain warm-up (silent): vault sync / project seed / decay
    try:
        try:
            from services.odys_neuron_hooks import natural_boot
        except ImportError:
            sys.path.insert(0, str(ROOT))
            from services.odys_neuron_hooks import natural_boot

        boot = natural_boot()
        acts = boot.get("actions") or []
        if acts:
            vs = boot.get("vault_sync") or {}
            ps = boot.get("project_seed") or {}
            bits = []
            if "vault_sync" in acts:
                bits.append(f"vault {vs.get('upserted', 0)} notes")
            if "project_seed" in acts:
                bits.append(f"projects {ps.get('seeded', 0)}")
            if "decay" in acts:
                d = boot.get("decay") or {}
                bits.append(f"decay -{d.get('dropped_edges', 0)}e")
            print(f"  🧠 Brain: {' · '.join(bits) if bits else ', '.join(acts)}")
        else:
            print("  🧠 Brain: warm (no maintenance needed)")
    except Exception as exc:
        print(f"  🧠 Brain: skipped ({exc})")


def cmd_stop(args):
    cmd_bridge_stop()
    cmd_server_stop()
    print("Odys berhenti.")
    if PID_FILE.exists():
        PID_FILE.unlink()
