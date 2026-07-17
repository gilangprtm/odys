#!/usr/bin/env python3
"""odys — CLI untuk manage Odys.

Thin entry shim. Implementation lives in cli/ package:

    cli/utils.py       shared constants + process helpers
    cli/bridge.py      bridge start/stop
    cli/server.py      server start/stop
    cli/start_stop.py  start / stop orchestration
    cli/status.py      status
    cli/audio.py       say / listen
    cli/doctor.py      doctor / decay
    cli/install.py     install
    cli/tray.py        system tray
    cli/help_cmd.py    help text
    cli/entry.py       argparse + main()

Pemakaian:
    odys install     Cek prerequisite + install dependensi + tambah PATH
    odys doctor      Diagnostic (Python, PATH, bridge, token, server)
    odys start       Jalankan bridge + server utama
    odys stop        Matikan semua proses
    odys status      Status bridge & server
    odys bridge      Jalankan bridge aja (tanpa server utama)
    odys say <teks>  TTS via Desktop Bridge (Windows SAPI)
    odys listen      Rekam mic → STT server (/api/stt/transcribe)
    odys tray        System tray icon Δ (background agent)
"""

from cli.entry import main

if __name__ == "__main__":
    main()
