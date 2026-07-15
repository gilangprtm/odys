# Selective Port: Hermes Engine Strengths → Odysseus (SAO Foundation)

> **Mode:** Rancang dulu. Belum eksekusi deep rewrite.
> **Goal:** Odysseus standalone SAO — keep Odysseus wins, transplant Hermes engine wins.
> **Non-goal:** Full replace Odysseus with Hermes. Jangan buang Chroma/UI/docs/tasks.

---

## 1. Diagnosis ringkas

| Domain | Pemenang | Keputusan |
|--------|----------|-----------|
| Memory / RAG / Chroma / FastEmbed | **Odysseus** | **KEEP** — SoT memory SAO |
| Web UI + document edit + notes/calendar/email/tasks | **Odysseus** | **KEEP** |
| Task scheduler / bg jobs | **Odysseus** | **KEEP** |
| Anti-loop, tool budget, session caps | **Hermes** (sudah di-port partial) | **KEEP + harden** |
| Native function-calling as primary | **Hermes** | **PORT** |
| Context compression | **Hermes** | **PORT** |
| Skill progressive disclosure + discipline | **Hermes** (Odysseus sudah punya SKILL.md) | **ALIGN + bridge** |
| Discord standalone messaging | **Hermes** | **PORT adapter only** |

**Prinsip:** UI + memory = Odysseus DNA. Agent efficiency + messaging = Hermes DNA.

---

## 2. Target architecture (SAO v0 on Odysseus)

```
┌─────────────────────────────────────────────────────────┐
│  Surfaces                                                │
│  Web UI (existing)  │  Discord adapter (NEW, native)     │
└───────────┬─────────────────────┬───────────────────────┘
            │                     │
            ▼                     ▼
┌─────────────────────────────────────────────────────────┐
│  Agent Core (hybrid Hermes discipline)                   │
│  1. Prompt builder (Odysseus + Hermes rules)             │
│  2. Native tools primary → fenced fallback               │
│  3. Tool budget / loop-breaker / session caps            │
│  4. Context compression (NEW)                            │
│  5. Skill index + view (Odysseus + optional Hermes RO)   │
└───────────┬─────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────┐
│  Memory & State (KEEP Odysseus)                          │
│  Chroma + manage_memory + sessions SQLite + tasks        │
└─────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────┐
│  LLM                                                     │
│  host.docker.internal:20128/v1  (fusion, etc.)           │
└─────────────────────────────────────────────────────────┘
```

**Standalone definition:** process Odysseus alone can chat (web), remember (Chroma), act (tools), and answer Discord — **no Hermes process required**.

---

## 3. What is already done (Phase 0)

- [x] Per-round tool cap + early duplicate break
- [x] Session caps (web_fetch / web_search / bash|python)
- [x] Loop-breaker tighter (stuck=2, runaway=5)
- [x] MAX_AGENT_ROUNDS 50→20; settings max_rounds/tools
- [x] Hermes discipline rules in `_AGENT_RULES`
- [x] GitHub `/contents` anti-spider
- [x] Thinking UI Option B (anti-freeze)
- [x] Hermes skills **read-only index** bridge + docker volume `/hermes-skills`
- [ ] Native FC primary path
- [ ] Context compression
- [ ] Skill full parity / optional write-sync
- [ ] Discord native adapter

---

## 4. Gap analysis (honest status of the 4 asks)

| Feature | Status now | Gap |
|---------|------------|-----|
| Native function-calling preference | Dual path exists; **fenced still dominant** for local/custom endpoints | Prefer native when endpoint supports tools; keep fenced fallback |
| Context compression | **Missing** | Need compress module + hook before each LLM round |
| Full skill_view/manage ↔ Hermes | Odysseus `manage_skills` OK; Hermes dir = index RO only | Optional: view Hermes SKILL.md via path; **no** full two-way CRUD required for SAO standalone |
| Discord | **None** | Port adapter pattern (discord.py / gateway-style) into Odysseus process |

---

## 5. Phased plan (selective, not full rewrite)

### Phase A — Native tool path first (highest agent quality)

**Why first:** Same model feels “smarter” when tools are structured JSON, not 3000 markdown fences.

**Keep:** All tool handlers (`src/agent_tools/*`, `src/tools/*`), Chroma, UI.

**Change:**
1. Detect provider capability: `tools` param accepted by endpoint (probe or config flag `prefer_native_tools`).
2. Build OpenAI-style tool schemas from existing `FUNCTION_TOOL_SCHEMAS` / TOOL_TAGS (already partial).
3. In `stream_agent_loop`:
   - Prefer `native_tool_calls` path when `prefer_native_tools=true` or API model.
   - Fenced path only as fallback (weak models / no tools API).
4. Cap native tools same as fenced (session caps + early duplicate).
5. Settings: `agent_prefer_native_tools`, `agent_allow_fenced_fallback`.

**Files (likely):**
- `src/agent_loop.py` — stream + resolve path priority
- `src/llm_core.py` — pass `tools=` / parse stream tool_calls
- `src/agent_tools/__init__.py` — schema export completeness
- `data/settings.json` — flags

**Verify:**
- Log: `native_tool_calls=N` with `tools_sent>0` for fusion@20128
- Repo analysis: <2 min, no 1000+ fence dumps
- Fenced still works if native disabled

**Est:** 3–7 hari fokus

---

### Phase B — Context compression (Hermes-style)

**Why:** Even with discipline, history + tool dumps grow → 83k prompt_tokens.

**Design (port ideas, not copy-paste Hermes):**
1. Config:
   ```json
   "context_compression": {
     "enabled": true,
     "threshold_ratio": 0.55,
     "target_ratio": 0.25,
     "keep_last_rounds": 4,
     "summarize_via": "same_model_or_cheap"
   }
   ```
2. Before each LLM call in `stream_agent_loop`:
   - Estimate tokens (existing `estimate_tokens`)
   - If over threshold: summarize older tool results + mid history into one system/user “compressed context” block
   - Keep: system rules, last N rounds, active doc, recent tool results
3. Never compress current turn user message
4. Log: `[context-compress] before=X after=Y`

**Files:**
- NEW `src/context_compression.py`
- Hook in `src/agent_loop.py`
- Settings + optional UI toggle later

**Verify:**
- Long chat: prompt_tokens drops after compress
- Answer quality still OK on “what did we decide earlier?”

**Est:** 3–5 hari

---

### Phase C — Skill system align (not Hermes dependency)

**SAO needs:** skills work **inside Odysseus data** forever.

**Design:**
1. **SoT skills:** `data/skills/**/SKILL.md` (already)
2. **Hermes bridge:** keep RO mount for migration/import only
3. **Import one-shot:** tool or admin action `import_hermes_skills` → copy selected SKILL.md into `data/skills/`
4. Align prompt discipline: always Level-0 index; Level-1 via `manage_skills view` (already Hermes-like)
5. Optional later: auto-extract skill after hard wins (Odysseus already has skill_extractor)

**Non-goal:** Live two-way sync with Hermes forever (that reintroduces Hermes dependency).

**Verify:**
- Odysseus offline from Hermes skills dir still has skills after import
- `manage_skills list/view/add/patch` works

**Est:** 2–4 hari

---

### Phase D — Discord native (standalone)

**Why last among the 4:** Needs stable agent loop first (A+B), then wire surface.

**Design:**
1. NEW package e.g. `src/messaging/discord_adapter.py`
2. Stack: `discord.py` (or light gateway) inside Odysseus process / optional sidecar service same compose
3. Features v1:
   - Token from env `DISCORD_BOT_TOKEN`
   - Message Content Intent required
   - Allowlist users/guilds
   - DM + mention in channels
   - Stream or chunked reply (Discord 2000 char limit → split)
   - Same `stream_agent_loop` as web (shared brain)
4. Settings UI later; v1 env + `data/settings.json`
5. Compose: no Hermes gateway service

**Files:**
- NEW `src/messaging/*`
- `app.py` or lifespan start adapter
- `docker-compose.yml` env
- docs in repo README section only if needed

**Verify:**
- Bot online, reply to DM without Hermes process running
- Tool call from Discord works (e.g. web_search + short answer)

**Est:** 5–10 hari (auth, intents, rate limits, formatting)

---

### Phase E — SAO packaging (after A–D stable)

- Brand/config: SAO mode defaults (vault path optional, fusion endpoint)
- `sao doctor` style health (local)
- Docs: README only per user preference
- Memory: keep Chroma as SAO SoT; optional vault export later

**Est:** ongoing polish

---

## 6. Explicit KEEP list (do not break)

- ChromaDB + RAG MCP + memory vectors
- Web UI static + document streaming tools
- Task scheduler (`manage_tasks`)
- Email/calendar/notes stacks
- Fenced tools as **fallback** (local weak models)
- Existing auth + localhost bypass

## 7. Explicit DROP / avoid

- Requiring Hermes gateway process for core chat
- Replacing Chroma with Hermes-only memory
- Porting entire Hermes CLI/desktop
- SearXNG / heavy local models (already trimmed)

---

## 8. Suggested order & milestones

| Week | Milestone | Exit criteria |
|------|-----------|---------------|
| W1 | Phase A native primary | Log shows native tools; repo task fast |
| W2 | Phase B compression | Long session prompt_tokens controlled |
| W3 | Phase C skill import + polish | Skills work without Hermes dir |
| W4–5 | Phase D Discord | Bot replies standalone |
| W6 | Phase E SAO defaults | Documented standalone runbook |

Aggressive path: A+B parallel if two streams; Discord only after A works.

---

## 9. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Native tools break fusion/20128 | Feature flag + fenced fallback |
| Compression loses critical detail | keep_last_rounds + never compress user last msg |
| Discord rate limits / long streams | Chunk replies; no full thinking dump |
| Docker path skills mount Windows | Already `HERMES_SKILLS_HOST`; prefer import to data/ |
| Regression on document LoRA path | Isolate doc mode from native FC changes |

**Branch strategy (recommend):** `feature/sao-engine-port` from current lite branch; merge per phase after verify.

---

## 10. Success definition (SAO ready)

Odysseus can:
1. Run `docker compose up` alone
2. Chat in web with disciplined tools + compression
3. Remember via Chroma (not Hermes)
4. Use skills from `data/skills` only
5. Answer Discord via own adapter
6. **No** Hermes process, **no** Hermes gateway required

---

## 11. Immediate next step (after Tuan approve)

1. Freeze this plan as SoT for port work
2. Open branch `feature/sao-engine-port`
3. Start **Phase A only** (native tool preference + verify fusion@20128)
4. Do **not** start Discord until Phase A green

---

## 12. Open decisions for Tuan

1. Approve phase order A→B→C→D?
2. Branch name OK?
3. Discord v1: DM-only first or guild channels too?
4. Skills: one-shot import enough, or need live RO bridge forever?
