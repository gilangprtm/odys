"""Odys Vault bootstrap — create Odys-Vault on install.

Default path: ~/Documents/Odys-Vault
Config: ~/.odys/config.json  (vault_path)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def default_vault_path() -> Path:
    """C:/Users/<user>/Documents/Odys-Vault (Windows) or ~/Documents/Odys-Vault."""
    home = Path.home()
    docs = home / "Documents"
    if not docs.is_dir():
        docs = home / "documents"
    if not docs.is_dir():
        docs = home
    return docs / "Odys-Vault"


def config_path() -> Path:
    return Path.home() / ".odys" / "config.json"


def load_config() -> dict[str, Any]:
    p = config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(cfg: dict[str, Any]) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def get_vault_path() -> Path:
    """Resolve vault path: env > config > default."""
    env = os.environ.get("ODY_VAULT_PATH") or os.environ.get("ODYS_VAULT_PATH")
    if env:
        return Path(env).expanduser()
    cfg = load_config()
    if cfg.get("vault_path"):
        return Path(cfg["vault_path"]).expanduser()
    return default_vault_path()


# ── Seed file contents ───────────────────────────────────

_AGENTS_MD = """---
title: "AGENTS.md — Odys Agent Instructions"
date: "{date}"
status: canonical
---

# Agent Instructions

> Odys vault rules. Keep short so small models don't truncate.

## Who

**Odys** — local AI operating layer. Permanent memory lives in this vault.

## Vault

**Path:** `{vault_path}`

Layout:
- `wiki/` — compiled knowledge
- `wiki/journal/` — daily digests
- `Sessions/` — session summaries
- `raw/` — unprocessed sources
- `ingested/` — processed archive
- `graphify-out/` — graph index (do not hand-edit)
- `_templates/` — note templates
- `Philosophy/` — core principles

## Hard rules

1. **Pre-check memory** before answering old topics.
2. **Evidence first** — terminal proof before claiming done.
3. **Prefer update** over create-duplicate notes.
4. Every `wiki/` page needs YAML frontmatter.
5. Use `[[wikilinks]]` between notes.
6. Journal: `wiki/journal/YYYY-MM-DD.md`
7. Drop new sources into `raw/`; after processing → `ingested/`.

## Related

- [[SCHEMA]] — folder map
- [[wiki/index]] — knowledge index
"""

_SCHEMA_MD = """---
title: "SCHEMA — Odys Vault Operating Instructions"
date: "{date}"
status: canonical
---

# SCHEMA

## Folder Map

| Path | Purpose |
|------|---------|
| `Philosophy/` | Core principles (keep) |
| `wiki/` | Compiled knowledge base |
| `wiki/journal/` | Daily digests |
| `raw/` | Incoming unprocessed sources |
| `ingested/` | Processed source archive |
| `Sessions/` | Session summaries |
| `graphify-out/` | Graph index output |
| `_templates/` | Note / PRD templates |
| `AGENTS.md` | Agent rules |

## Rules

1. Every `wiki/` page needs YAML frontmatter
2. Use `[[wikilinks]]` between notes
3. Prefer update over create-duplicate
4. Journal files: `wiki/journal/YYYY-MM-DD.md`
5. Drop new sources into `raw/`; after processing move to `ingested/`
6. Do not hand-edit `graphify-out/` — regenerate via graphify
"""

_WIKI_INDEX = """---
title: "Odys Wiki Index"
date: "{date}"
status: canonical
---

# Odys Wiki

Knowledge base for Odys. Start here.

## Sections

- [[journal]] — daily digests
- Projects tracked via Odys Projects

## How to add notes

1. Create markdown under `wiki/` with YAML frontmatter
2. Link with `[[wikilinks]]`
3. Prefer updating existing pages
"""

_JOURNAL_README = """---
title: "Journal"
date: "{date}"
status: canonical
---

# Journal

Daily digests live here as `YYYY-MM-DD.md`.

Created by Odys install on {date}.
"""

_PHILOSOPHY = """---
title: "Odys Philosophy"
date: "{date}"
status: canonical
---

# Odys Philosophy

## Principles

1. **Local-first** — data and control stay on the machine
2. **Evidence over claims** — prove with terminal output
3. **Vault is memory** — this folder is the durable brain
4. **Desktop-aware** — bridge, tray, voice, projects
5. **KISS / DRY / YAGNI** — small modules, no bloat

## Identity

- Brand: Odys (Δ)
- Accent: `#00b3a0`
- CLI: `odys install | doctor | start | stop | tray | say | listen`
"""

_TEMPLATE_NOTE = """---
title: "{{title}}"
date: "{{date}}"
status: draft
tags: []
---

# {{title}}

## Summary

## Notes

## Links

-
"""

_GRAPHIFY_GITKEEP = ""


def ensure_odys_vault(vault: Path | None = None, force_seed: bool = False) -> dict[str, Any]:
    """Create Odys-Vault structure if missing. Idempotent.

    Returns dict: {ok, path, created: [paths], existed: bool, config_path}
    """
    vault = (vault or get_vault_path()).expanduser().resolve()
    created: list[str] = []
    existed = vault.is_dir()

    dirs = [
        vault,
        vault / "wiki",
        vault / "wiki" / "journal",
        vault / "Sessions",
        vault / "Philosophy",
        vault / "raw",
        vault / "ingested",
        vault / "graphify-out",
        vault / "_templates",
    ]
    for d in dirs:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d.relative_to(vault) if d != vault else "."))

    date = time.strftime("%Y-%m-%d")
    vault_str = str(vault).replace("\\", "/")

    seeds: list[tuple[Path, str]] = [
        (vault / "AGENTS.md", _AGENTS_MD.format(date=date, vault_path=vault_str)),
        (vault / "SCHEMA.md", _SCHEMA_MD.format(date=date)),
        (vault / "wiki" / "index.md", _WIKI_INDEX.format(date=date)),
        (vault / "wiki" / "journal" / "README.md", _JOURNAL_README.format(date=date)),
        (vault / "Philosophy" / "Odys.md", _PHILOSOPHY.format(date=date)),
        (vault / "_templates" / "note.md", _TEMPLATE_NOTE),
        (vault / "graphify-out" / ".gitkeep", _GRAPHIFY_GITKEEP),
        (vault / "Sessions" / ".gitkeep", _GRAPHIFY_GITKEEP),
        (vault / "raw" / ".gitkeep", _GRAPHIFY_GITKEEP),
        (vault / "ingested" / ".gitkeep", _GRAPHIFY_GITKEEP),
    ]

    for path, content in seeds:
        if force_seed or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            try:
                rel = str(path.relative_to(vault))
            except ValueError:
                rel = str(path)
            if rel not in created:
                created.append(rel)

    # Persist config
    cfg = load_config()
    cfg["vault_path"] = str(vault)
    cfg["vault_created_at"] = cfg.get("vault_created_at") or time.strftime("%Y-%m-%dT%H:%M:%S")
    cfg["vault_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_config(cfg)

    return {
        "ok": True,
        "path": str(vault),
        "created": created,
        "existed": existed,
        "config_path": str(config_path()),
    }
