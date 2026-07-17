"""cli/entry.py — CLI entry point (argparse + handler map)."""

import argparse

from cli.audio import cmd_listen, cmd_say
from cli.bridge import cmd_bridge_start
from cli.doctor import cmd_decay, cmd_doctor
from cli.help_cmd import cmd_help
from cli.install import cmd_install
from cli.start_stop import cmd_start, cmd_stop
from cli.status import cmd_status
from cli.tray import cmd_tray


def main():
    """
    Entry point. Dipanggil dari `odys.py` atau `pip install -e .` entry point.
    """
    parser = argparse.ArgumentParser(
        description="Odys — CLI manager",
        usage="odys <command> [args]",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="help",
        choices=[
            "install",
            "doctor",
            "start",
            "stop",
            "status",
            "bridge",
            "say",
            "listen",
            "tray",
            "decay",
            "help",
        ],
    )
    parser.add_argument("subargs", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    handlers = {
        "install": cmd_install,
        "doctor": cmd_doctor,
        "tray": cmd_tray,
        "decay": cmd_decay,
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
