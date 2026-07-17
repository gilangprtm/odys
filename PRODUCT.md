# Odys

## Product
Local-first AI operating layer. Chat-first workspace with tools, skills, memory, neurons, and desktop bridge. Runs on the user's machine; data stays local.

## Register
product

## Platform
web

## Surface
App UI / dashboard / tool — not marketing brand site.

## Audience
Builder and power user on Windows (primary). Values speed, clarity, dense ops UI without clutter. Runs Docker or native `odys start`.

## Voice
Terse, precise, no fluff. Agent persona addresses user formally ("Tuan"). UI chrome is neutral and technical.

## Brand lane
Command-center: deep teal canvas, warm cream text, gold primary actions. Monospace-friendly. Dense, high contrast on dark. Identity mark: Δ.

## Anti-references
- Purple-to-blue SaaS gradients
- Nested cards everywhere
- Inter-only generic landing look
- Bounce / elastic easing
- Gray text on colored backgrounds
- Glassmorphism for its own sake
- Cream/sand light body as default "warmth"

## Architecture
- Backend: Python Odys (uvicorn) on `:7000` — API, auth, agent loop, skills, memory
- Frontend (new): Vite + React + TypeScript + Tailwind v4 + shadcn/ui in `frontend/`
- Frontend (legacy): monolit `static/` until feature parity
- Dev proxy: `/api` and `/static` → `http://127.0.0.1:7000`
- Design architect: Impeccable; components: shadcn only for base primitives

## Success
- Chat shell usable from React SPA with command theme
- Same cookies / API as legacy UI
- Impeccable detect clean of major slop rules on `frontend/`
- No Hermes branding in product surface
