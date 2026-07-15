"""Odys Council API — /api/odys/council/*

Agents, reports, pipeline, run actions.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Optional

from services.odys_council_service import (
    list_agents,
    list_reports,
    get_report,
    run_agent,
    set_report_status,
    get_pipeline,
    get_queue,
)
from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user

router = APIRouter(prefix="/api/odys/council", tags=["odys"])


def _require_admin(request: Request):
    user = get_current_user(request)
    if not owner_is_admin_or_single_user(user):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin only")


class RunBody(BaseModel):
    agent_id: str
    action: str
    project_id: Optional[str] = None


class StatusBody(BaseModel):
    status: str


@router.get("/agents")
def api_agents(request: Request):
    _require_admin(request)
    return {"ok": True, "agents": list_agents()}


@router.get("/reports")
def api_reports(request: Request, agent_id: Optional[str] = None, project_id: Optional[str] = None):
    _require_admin(request)
    reports = list_reports(agent_id=agent_id, project_id=project_id)
    return {"ok": True, "reports": reports, "queue": get_queue()}


@router.get("/reports/{report_id}")
def api_report(report_id: str, request: Request):
    _require_admin(request)
    r = get_report(report_id)
    if not r:
        return {"ok": False, "message": "Not found"}
    return {"ok": True, "report": r}


@router.post("/run")
def api_run(body: RunBody, request: Request):
    _require_admin(request)
    return run_agent(body.agent_id, body.action, body.project_id)


@router.post("/reports/{report_id}/status")
def api_status(report_id: str, body: StatusBody, request: Request):
    _require_admin(request)
    return set_report_status(report_id, body.status)


@router.get("/pipeline")
def api_pipeline(request: Request, project_id: Optional[str] = None):
    _require_admin(request)
    return get_pipeline(project_id)
