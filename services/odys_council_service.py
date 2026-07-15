"""Odys Council Service — multi-agent pipeline (report-only).

5 agents: research, business, architect, developer, marketing.
Reports stored in data/odys_council/reports.json.
Run action builds structured report from project context (no external LLM required).
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from services.odys_projects_service import list_projects, get_project_detail

DATA_DIR = Path("data/odys_council")
REPORTS_FILE = DATA_DIR / "reports.json"

AGENTS: list[dict[str, Any]] = [
    {
        "id": "research",
        "name": "Research",
        "icon": "◎",
        "short": "R&D",
        "department": "Research",
        "status": "ready",
        "description": "Market opportunity, competitor scan, problem framing",
        "actions": [
            {"id": "market_opportunity", "label": "Market Opportunity"},
            {"id": "problem_frame", "label": "Problem Frame"},
        ],
    },
    {
        "id": "business",
        "name": "Business",
        "icon": "◆",
        "short": "BIZ",
        "department": "Business",
        "status": "ready",
        "description": "Monetisation, positioning, go-to-market",
        "actions": [
            {"id": "monetisation", "label": "Monetisation Report"},
            {"id": "positioning", "label": "Positioning"},
        ],
    },
    {
        "id": "architect",
        "name": "Architect",
        "icon": "⬡",
        "short": "ARC",
        "department": "Architecture",
        "status": "ready",
        "description": "System design, stack review, risks",
        "actions": [
            {"id": "architecture_review", "label": "Architecture Review"},
            {"id": "stack_audit", "label": "Stack Audit"},
        ],
    },
    {
        "id": "developer",
        "name": "Developer",
        "icon": "▣",
        "short": "DEV",
        "department": "Engineering",
        "status": "ready",
        "description": "Codebase review, next implementation steps",
        "actions": [
            {"id": "codebase_review", "label": "Codebase Review"},
            {"id": "next_steps", "label": "Next Steps"},
        ],
    },
    {
        "id": "marketing",
        "name": "Marketing",
        "icon": "△",
        "short": "MKT",
        "department": "Marketing",
        "status": "ready",
        "description": "Launch strategy, messaging, channels",
        "actions": [
            {"id": "launch_strategy", "label": "Launch Strategy"},
            {"id": "messaging", "label": "Messaging"},
        ],
    },
]

PIPELINE_ORDER = ["research", "business", "architect", "developer", "marketing"]


def _load_reports() -> list[dict]:
    if REPORTS_FILE.exists():
        try:
            data = json.loads(REPORTS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _save_reports(reports: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_FILE.write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")


def list_agents() -> list[dict]:
    reports = _load_reports()
    out = []
    for a in AGENTS:
        agent = dict(a)
        agent_reports = [r for r in reports if r.get("agent_id") == a["id"]]
        agent["report_count"] = len(agent_reports)
        waiting = sum(1 for r in agent_reports if r.get("status") == "waiting_for_review")
        agent["waiting_count"] = waiting
        if agent_reports:
            latest = agent_reports[0]
            agent["last_report_at"] = latest.get("created_at")
            agent["current_task"] = latest.get("title")
        else:
            agent["last_report_at"] = None
            agent["current_task"] = None
        out.append(agent)
    return out


def list_reports(agent_id: str | None = None, project_id: str | None = None) -> list[dict]:
    reports = _load_reports()
    if agent_id:
        reports = [r for r in reports if r.get("agent_id") == agent_id]
    if project_id:
        reports = [r for r in reports if r.get("project_id") == project_id]
    return reports


def get_report(report_id: str) -> dict | None:
    for r in _load_reports():
        if r.get("id") == report_id:
            return r
    return None


def _project_context(project_id: str | None) -> dict:
    if not project_id:
        projects = list_projects()
        if not projects:
            return {"name": "General", "path": "", "stack": [], "type": "General", "files": 0}
        # Prefer pinned, else first recent
        pinned = [p for p in projects if p.get("pinned")]
        p = pinned[0] if pinned else projects[0]
        return {
            "id": p.get("id"),
            "name": p.get("name"),
            "path": p.get("path"),
            "stack": p.get("detected_stack") or [],
            "type": p.get("detected_type") or "Project",
            "files": p.get("file_count") or 0,
            "activity": p.get("last_activity_at"),
            "changes": p.get("recent_changes") or {},
        }
    detail = get_project_detail(project_id)
    p = detail.get("project") or {}
    return {
        "id": p.get("id") or project_id,
        "name": p.get("name") or project_id,
        "path": p.get("path"),
        "stack": p.get("detected_stack") or [],
        "type": p.get("detected_type") or "Project",
        "files": p.get("file_count") or 0,
        "activity": p.get("last_activity_at"),
        "changes": detail.get("recent_changes") or p.get("recent_changes") or {},
        "recommendation": (detail.get("recommendation") or {}).get("do_this_next"),
    }


def _build_body(agent_id: str, action: str, ctx: dict) -> str:
    name = ctx.get("name") or "Project"
    stack = ", ".join(ctx.get("stack") or []) or "unknown"
    ptype = ctx.get("type") or "Project"
    files = ctx.get("files") or 0
    path = ctx.get("path") or "—"
    changes = ctx.get("changes") or {}
    recent = changes.get("recent_files") or []
    recent_txt = "\n".join(f"- {x}" for x in recent[:8]) or "- (no recent activity tracked)"

    common = f"""# Project Context
- **Name:** {name}
- **Type:** {ptype}
- **Stack:** {stack}
- **Files:** {files}
- **Path:** {path}
- **Last activity:** {ctx.get('activity') or '—'}

## Recent activity
{recent_txt}
"""

    templates = {
        ("research", "market_opportunity"): f"""# Market Opportunity — {name}

{common}

## Opportunity hypothesis
{name} appears to be a **{ptype}** built on **{stack}**.

## Who might care
- Builders who need local/self-hosted AI workspace tooling
- Teams already running related stacks ({stack})
- Users with multi-repo workflows

## Competitive angle
- Differentiate via vault-centric memory + desktop bridge + tray/wake-word
- Avoid generic chat-only positioning

## Next research steps
1. Interview 3 target users about daily friction
2. Map 5 competitors and their pricing
3. Validate one wedge feature against {name}'s current strengths

## Confidence
Medium — based on repo signals only (no external market data).
""",
        ("research", "problem_frame"): f"""# Problem Frame — {name}

{common}

## Core problem
Operators juggling multiple projects need a single control surface that understands repos, voice, and desktop actions.

## Jobs to be done
1. Know which project to work on next
2. Launch tools/apps without context-switch tax
3. Capture decisions as durable reports

## Constraints
- Local-first preference
- Windows host desktop integration
- Keep chat UX intact

## Success signal
User opens Odys Home → sees clear next action → acts within 1 click.
""",
        ("business", "monetisation"): f"""# Monetisation Report — {name}

{common}

## Packaging ideas
1. **Personal OS layer** — free/self-host core
2. **Pro** — multi-agent council + project HQ automation
3. **Team** — shared reports + project pipeline

## Value props
- Saves context-switch time across {files}+ files in this repo alone
- Desktop + voice differentiates from pure web chat tools

## Risks
- Crowded AI-assistant market
- Monetisation needs clear paid wedge beyond chat

## Recommendation
Ship Project HQ + Council as the paid wedge; keep chat free.
""",
        ("business", "positioning"): f"""# Positioning — {name}

{common}

## One-liner
**Odys** — local AI operating layer for builders: chat, projects, desktop, voice.

## Not this
- Not another hosted chatbot
- Not a full IDE replacement

## Category
Personal AI OS / local mission control

## Proof points from this repo
- Stack: {stack}
- Scale: {files} tracked files
""",
        ("architect", "architecture_review"): f"""# Architecture Review — {name}

{common}

## Observed shape
- Type: {ptype}
- Stack signals: {stack}

## Strengths
- Clear modular surface (services + routes + static JS modules)
- Native Windows path for project scan (no Docker mount required)

## Risks
- Large static/app.js surface area — keep new modules isolated
- Report generation currently template-based; wire LLM later for depth

## Suggested architecture moves
1. Keep Odys modules under `/api/odys/*` + `static/js/odys*.js`
2. Persist council reports as JSON now; migrate to DB if multi-user needed
3. Bridge + tray remain host-side; UI only proxies

## Priority fix
Document module boundaries in README so new agents don't couple to chat core.
""",
        ("architect", "stack_audit"): f"""# Stack Audit — {name}

{common}

## Detected stack
{stack or 'None detected'}

## Fit assessment
- Good if stack matches team skills and deployment target
- Watch for dual frontend/backend complexity if both Node + Python present

## Recommendations
1. Pin versions for critical deps
2. Keep one canonical CLI entry (`odys`)
3. Prefer additive modules over rewrites
""",
        ("developer", "codebase_review"): f"""# Codebase Review — {name}

{common}

## Snapshot
- ~{files} files indexed
- Path: `{path}`

## What looks healthy
- Service/route split for Odys features
- Project scan produces actionable metadata

## Gaps to close
1. Add tests for `/api/odys/*` happy paths
2. Wire Council run → optional LLM enrich
3. Surface stale index warnings in UI

## Suggested next PR
- Council UI + report viewer
- Smoke test scan/index/briefing/council endpoints
""",
        ("developer", "next_steps"): f"""# Next Steps — {name}

{common}

## Immediate (today)
1. Open Odys Home → confirm briefing
2. Scan + Index projects
3. Run one Council agent on active project

## This week
1. Connect Council report body to chat for rewrite/expand
2. Add pin/filter on project list
3. Tray wake-word end-to-end test

## Later
1. Vault SAO as brain
2. Always-listening mode polish
""",
        ("marketing", "launch_strategy"): f"""# Launch Strategy — {name}

{common}

## Audience
Indie builders + power users on Windows who want local AI control.

## Launch narrative
"Odys is the Δ on your desktop — chat, projects, voice, and apps in one local OS layer."

## Channels
1. GitHub README + short demo GIF (tray + Project HQ)
2. Dev Twitter/X thread: problem → demo → install
3. Discord community for feedback loops

## Launch checklist
- [ ] `odys doctor` green on clean machine
- [ ] Project HQ scan works on D:/Project
- [ ] Council produces first report
- [ ] Accent/brand Δ consistent

## CTA
`odys install` → `odys start` → `odys tray --autostart`
""",
        ("marketing", "messaging"): f"""# Messaging — {name}

{common}

## Tagline options
1. Local AI OS for builders
2. Chat is the interface. Odys is the operating layer.
3. Δ — always on the tray

## Do say
- Local-first, desktop-aware, project-aware
- Voice + bridge + HQ

## Don't say
- "Just another ChatGPT wrapper"
- Enterprise claims without proof
""",
    }

    key = (agent_id, action)
    if key in templates:
        return templates[key]

    # Fallback generic
    return f"""# {agent_id.title()} / {action} — {name}

{common}

## Notes
Structured report generated from project signals.
Action `{action}` by agent `{agent_id}`.

## Follow-up
Expand this report in chat or re-run after indexing more context.
"""


def run_agent(
    agent_id: str,
    action: str,
    project_id: str | None = None,
) -> dict:
    agent = next((a for a in AGENTS if a["id"] == agent_id), None)
    if not agent:
        return {"ok": False, "message": f"Unknown agent: {agent_id}"}

    valid_actions = {a["id"] for a in agent.get("actions") or []}
    if action not in valid_actions:
        # allow any action string but prefer known
        if not action:
            return {"ok": False, "message": "action required"}

    ctx = _project_context(project_id)
    action_label = next(
        (a["label"] for a in (agent.get("actions") or []) if a["id"] == action),
        action.replace("_", " ").title(),
    )
    title = f"{agent['name']}: {action_label} — {ctx.get('name') or 'General'}"
    body = _build_body(agent_id, action, ctx)

    report = {
        "id": str(uuid.uuid4())[:12],
        "agent_id": agent_id,
        "agent_name": agent["name"],
        "action": action,
        "title": title,
        "body": body,
        "status": "completed",
        "project_id": ctx.get("id") or project_id,
        "project_name": ctx.get("name"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    reports = _load_reports()
    reports.insert(0, report)
    # Cap history
    reports = reports[:200]
    _save_reports(reports)

    return {
        "ok": True,
        "message": f"Report ready: {title}",
        "report": report,
        "agents": list_agents(),
    }


def set_report_status(report_id: str, status: str) -> dict:
    allowed = {"completed", "waiting_for_review", "approved", "archived"}
    if status not in allowed:
        return {"ok": False, "message": f"Invalid status: {status}"}
    reports = _load_reports()
    for r in reports:
        if r.get("id") == report_id:
            r["status"] = status
            _save_reports(reports)
            return {"ok": True, "report": r}
    return {"ok": False, "message": "Report not found"}


def get_pipeline(project_id: str | None = None) -> dict:
    reports = list_reports(project_id=project_id)
    stages = []
    for aid in PIPELINE_ORDER:
        agent = next((a for a in AGENTS if a["id"] == aid), None)
        if not agent:
            continue
        agent_reports = [r for r in reports if r.get("agent_id") == aid]
        latest = agent_reports[0] if agent_reports else None
        stages.append({
            "agent_id": aid,
            "label": agent["name"],
            "status": "done" if latest else "not_started",
            "latest_report": {
                "id": latest["id"],
                "title": latest["title"],
                "status": latest["status"],
            } if latest else None,
        })
    return {"ok": True, "stages": stages, "project_id": project_id}


def get_queue() -> dict:
    reports = _load_reports()
    waiting = [r for r in reports if r.get("status") == "waiting_for_review"]
    completed_today = [
        r for r in reports
        if (r.get("created_at") or "").startswith(time.strftime("%Y-%m-%d"))
    ]
    return {
        "pending": [],
        "waiting_for_approval": waiting[:20],
        "completed_today": completed_today[:20],
    }
