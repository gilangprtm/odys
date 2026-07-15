# Port Hermes Discipline → Odysseus Core

> **Goal:** Odysseus agent loop no longer infinity-loops; adopts Hermes tool discipline, skill system, and Discord access.

**Status:** Phase 1 done (code). Phase 2–3 remapped.

---

## What's been patched today

### Phase 1: Hermes discipline di agent loop ✅

| Patch | File | Effect |
|-------|------|--------|
| Per-round tool cap = 15 | `src/agent_loop.py` | Model dump >15 tool block → di-break, sisanya drop. Mencegah 3724 block |
| Early duplicate detection | `src/agent_loop.py` | Signature identik (tool_type+content[:120]) → break instan, tidak nunggu akhir round |
| Global settings budget | `data/settings.json` | `agent_max_tool_calls=30`, `agent_max_rounds=12` |
| GitHub contents guard | `src/agent_tools/web_tools.py` | `/api.github.com/.../contents` → parse JSON, cap 40 entries, sisip policy "max 5 file fetch" |
| System prompt dicipline | `src/agent_loop.py` | Rule: "DO NOT recursively fetch every folder under /contents" + prefer key files |

### Phase 2: Hermes skills bridge (dipetakan ulang)

**Pendekatan baru** — lebih ringan dari full port:
- Hermes skill dir `~/.hermes/skills/` tetap di Hermes
- Odysseus cukup baca via `manage_skills` → inject ke system prompt (ringkas)
- Tidak perlu port engine skill-view/skill-manage ke Python Odysseus

### Phase 3: Discord (dipetakan ulang)

**Rekomendasi:** Hermes Gateway lebih cepat daripada port adapter native.
- Odysseus web UI tetap jalan di `localhost:7000`
- Discord chats diarahkan ke Hermes Gateway (yang sudah support Discord)
- Hermes bisa akses vault/memory Tuan langsung

---

## Status container

```
odysseus-odysseus-1   Up (build latest...)
odysseus-chromadb-1   Up
```

---

## Langkah selanjutnya (opsional)

1. **Test** — "Baca repo https://github.com/gilangprtm/sao" → harus <2 menit, tidak freeze
2. **Skill bridge** — inject ~/.hermes/skills headlines ke system prompt Odysseus (ringan)
3. **Discord** — setup Hermes Gateway + token bot
