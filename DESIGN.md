# Odys DESIGN.md

## Intent
Dense local AI workspace. Command center, not marketing site. Design **serves** the product (register: product).

## Color strategy
**Restrained:** tinted teal neutrals + one gold accent ≤10% of surface (actions, focus, logo).

| Role | Token | Value | Notes |
|------|-------|-------|--------|
| Canvas | `--background` | `#0b201f` | deep teal body |
| Panel / card | `--card` | `#0f2625` | elevated surface |
| Text | `--foreground` | `#dccbb5` | warm cream ink |
| Border | `--border` | `#2d4038` | soft teal edge |
| Primary | `--primary` | `#ffac02` | gold actions |
| Primary fg | `--primary-foreground` | `#0b201f` | ink on gold |
| Secondary | `--secondary` | `#132e2c` | AI bubble / quiet fill |
| Muted text | `--muted-foreground` | `#b5a894` | ≥4.5:1 on canvas |
| Sage | chart / success | `#7c945c` | secondary accent |
| Sidebar | `--sidebar` | `#0a1c1b` | slightly deeper than canvas |

Dark is the product default (`class="dark"` on `<html>`). Light mode is optional later — not the brand default.

No pure black/gray: always tint toward teal. No cream body bg.

## Typography
- UI: Geist Variable
- Code / dense ops: JetBrains Mono or Fira Code
- Body line length ~65–75ch in prose regions
- Display letter-spacing ≥ -0.04em

## Layout
- App shell: collapsible sidebar + inset content
- Chat: message stream + sticky composer
- Hairline borders over heavy shadows
- Radius modest (`--radius: 0.5rem`); no 32px+ cards
- Cards only when needed — never nested cards

## Components
**Base:** shadcn/ui (base-nova) under `frontend/src/components/ui/` only.

**Domain (compose primitives):** ChatThread, ChatComposer, ToolCall, ThinkingBlock, SessionList.

Never reinvent Button/Input/Dialog/Sidebar.

## Motion
150–200ms opacity/transform. Ease-out. No bounce/elastic. Respect `prefers-reduced-motion`.

## Anti-patterns (absolute)
Side-stripe accent borders · gradient text · decorative glass · purple gradients · nested cards · hero-metric SaaS cliché · ghost-card (1px border + wide soft shadow) · over-rounded 32px+ cards.

## Implementation
- Tokens: `frontend/src/index.css`
- PRODUCT.md at repo root (this design system pairs with it)
- Impeccable: `npx impeccable detect frontend/src`
- Live iteration: `/impeccable live` against `npm run dev` (:5173)
