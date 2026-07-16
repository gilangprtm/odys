<p align="center">
  <img src="docs/odysseus-wordmark.png" alt="Odys" width="180">
</p>

<p align="center">
  AI operating layer — local, voice-ready, desktop-aware.
</p>

<p align="center">
  <code>odys install</code> ·
  <code>odys doctor</code> ·
  <code>odys start</code> ·
  <code>odys stop</code> ·
  <code>odys status</code> ·
  <code>odys tray</code>
</p>

---

## Quick Start

```powershell
git clone https://github.com/gilangprtm/odys.git
cd odys
odys install
```

Buka terminal **baru**, lalu:

```powershell
odys start
odys tray --autostart   # opsional: tray Δ + wake word
```

Buka `http://localhost:7000`. Selesai.

`odys install` juga membuat **Odys-Vault** di `Documents/Odys-Vault` (brain).

### Prerequisite

| Tool | Minimal | Catatan |
|------|---------|---------|
| Python | 3.11 | `python --version` |
| pip | (ikut Python) | `python -m pip --version` |
| Docker | (opsional) | Hanya untuk container mode |

---

## Commands

| Command | Fungsi |
|---------|--------|
| `odys install` | Prereq, deps, PATH, buat Odys-Vault |
| `odys doctor` | Diagnostic: Python, PATH, deps, bridge, server, vault, neurons, mic |
| `odys doctor --decay` | Doctor + neuron decay sekali |
| `odys decay` | Neuron edge decay (forget weak links) |
| `odys start` | Bridge + server + **brain warm-up** (vault sync / project seed / decay) |
| `odys stop` | Matikan semua proses |
| `odys status` | Cek status (bridge & server) |
| `odys bridge` | Desktop Bridge saja |
| `odys say "teks"` | TTS speaker Windows (SAPI) |
| `odys listen` | Rekam mic → STT (butuh server) |
| `odys tray` | System tray icon Δ |
| `odys tray --autostart` | Tray + register Windows startup |
| `odys help` | Bantuan |

### Desktop Bridge

Bridge = service Windows: buka app desktop + TTS lewat speaker host.

```
odys start → bridge nyala otomatis di background
odys stop  → bridge mati
odys say "selamat pagi tuan"  → suara dari speaker
```

Aplikasi terdaftar: ZCode, Antigravity IDE, Zed, Obsidian, Chrome, Edge, Terminal, Explorer.

### Tray + wake word

```
odys tray              # icon Δ di system tray
odys tray --autostart  # + start otomatis saat login Windows
```

- Monitor health bridge (8765) + server (7000)
- Quick actions: buka UI, stop, say, wake toggle
- Wake word **"Sira..."** (Vosk offline) → rekam + STT

### Vault + Neurons

| Path | Peran |
|------|-------|
| `Documents/Odys-Vault` | Permanent brain (wiki, sessions, philosophy) |
| `data/odys_neurons/graph.json` | Activation graph (local) |
| `~/.odys/config.json` | vault_path + sync/decay timestamps |

**Natural (otomatis):**

- `odys start` → sync vault notes, seed projects, light decay
- Chat → co-activate memories + inject active thoughts (silent)
- Project scan/open → project nodes
- Tidak perlu klik Sync Vault / Decay (tombol Home = fallback)

Detail: vault note `wiki/neurons.md` (setelah install).

---

## Features

- **Chat + Agents** — tools, MCP, file, shell, skills, memory
- **Neurons** — activation layer (weight + decay) di atas memory + vault
- **Desktop Bridge** — buka app + TTS (`/api/bridge/*`)
- **Project HQ / Home / Council** — scan projects, briefing, multi-agent reports
- **Cookbook** — model recommendations
- **Deep Research** — multi-step research + report
- **Documents** — editor Markdown/HTML/CSV
- **Email** — IMAP/SMTP, summaries, drafts
- **Notes, Tasks + Calendar**
- **Voice** — STT/TTS + `odys say` + wake word tray
- **Themes** — Blueprint UI, neon themes

---

## Untuk Developer

```powershell
pip install -r requirements.txt
pip install -r desktop_bridge\requirements.txt

# Jalankan langsung tanpa CLI
python launcher.py
# atau
python -m uvicorn app:app --host 127.0.0.1 --port 7000
```

Neuron API (admin auth):

```
GET  /api/odys/neurons/status
POST /api/odys/neurons/activate
POST /api/odys/neurons/sync-vault
POST /api/odys/neurons/decay
```

---

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).

---

<p align="center">
  Odys DNA. Local-first. Evidence over claims.
</p>
