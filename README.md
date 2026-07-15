<p align="center">
  <img src="docs/odysseus-wordmark.png" alt="Odys" width="180">
</p>

<p align="center">
  AI operating layer — local, voice-ready, desktop-aware.
</p>

<p align="center">
  <code>odys install</code> ·
  <code>odys start</code> ·
  <code>odys stop</code> ·
  <code>odys status</code>
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
```

Buka `http://localhost:7000`. Selesai.

### Prerequisite

| Tool | Minimal | Catatan |
|------|---------|---------|
| Python | 3.11 | `python --version` |
| pip | (ikut Python) | `python -m pip --version` |
| Docker | (opsional) | Hanya untuk container mode |

`odys install` cek semuanya otomatis + install dependensi + tambah PATH.

---

## Commands

| Command | Fungsi |
|---------|--------|
| `odys install` | Cek prerequisite, install dependensi, tambah PATH |
| `odys start` | Jalankan Desktop Bridge + server utama |
| `odys stop` | Matikan semua proses |
| `odys status` | Cek status (bridge & server) |
| `odys bridge` | Jalankan Desktop Bridge aja (tanpa server) |
| `odys help` | Bantuan |

### Desktop Bridge

Bridge adalah service Windows yg meluncurkan **whitelisted** aplikasi desktop dari chat/perintah:

```
odys start → bridge nyala otomatis di background
odys stop  → bridge mati
```

Aplikasi yg terdaftar: ZCode, Antigravity IDE, Zed, Obsidian, Chrome, Edge, Terminal, Explorer.

---

## Features

- **Chat + Agents** — AI chat dengan tools, MCP, file, shell, skills, dan memory
- **Desktop Bridge** — Buka aplikasi desktop dari chat (ZCode, Obsidian, Chrome, dll)
- **Cookbook** — Model AI recommendations, download, serving
- **Deep Research** — Multi-step web research + report generation
- **Documents** — AI-powered editor (Markdown, HTML, CSV)
- **Email** — IMAP/SMTP inbox, summaries, reminders, reply drafts
- **Notes, Tasks + Calendar** — reminders, todos, CalDAV sync
- **Voice** — STT/TTS ready (aktifkan di Settings)
- **Themes** — Blueprint UI, multiple neon themes

---

## Untuk Developer

```powershell
pip install -r requirements.txt
pip install -r desktop_bridge\requirements.txt

# Jalankan langsung tanpa CLI
python launcher.py
```

---

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).

---

<p align="center">
  Dibangun dari <a href="https://github.com/pewdiepie-archdaemon/odysseus">Odysseus</a>.
  DNA sendiri. Semua kode yg masuk adalah milik kita.
</p>
