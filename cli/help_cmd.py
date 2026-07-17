"""cli/help_cmd.py — odys help."""


def cmd_help(args):
    print("""Odys — CLI manager
Pemakaian:
    odys install     Cek prerequisite + install dependensi + tambah PATH + buat vault
    odys doctor      Diagnostic (Python, PATH, deps, bridge, token, server, vault, neurons, mic)
    odys doctor --decay   Diagnostic + run neuron decay
    odys decay       Run neuron edge decay (forget weak links)
    odys start       Jalankan bridge + server utama
    odys stop        Matikan semua proses
    odys status      Status bridge & server
    odys bridge      Jalankan bridge aja (tanpa server utama)
    odys say <teks>  TTS via Desktop Bridge (Windows SAPI)
    odys listen      Rekam mic → STT server (/api/stt/transcribe)
    odys tray        System tray icon Δ (background agent)
        --autostart: Register Windows startup + launch tray
    odys help        Tampilkan ini""")
