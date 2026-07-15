"""Odys Projects API routes — /api/odys/projects/*

Scan D:/Project, index repo, track git activity. Native Windows filesystem.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from services.odys_projects_service import (
    list_projects,
    scan_projects,
    index_project,
    get_project_detail,
    pin_project,
)
from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user

router = APIRouter(prefix="/api/odys/projects", tags=["odys"])


def _require_admin(request: Request):
    user = get_current_user(request)
    if not owner_is_admin_or_single_user(user):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin only")


@router.get("")
def api_list_projects(request: Request):
    """List semua proyek yang diketahui."""
    _require_admin(request)
    projects = list_projects()
    return {"ok": True, "projects": projects}


@router.post("/scan")
def api_scan_projects(request: Request):
    """Scan D:/Project untuk cari proyek baru."""
    _require_admin(request)
    result = scan_projects()
    return result


@router.post("/{project_id}/index")
def api_index_project(project_id: str, request: Request):
    """Index satu proyek: git diff + file count."""
    _require_admin(request)
    result = index_project(project_id)
    return result


@router.get("/{project_id}/detail")
def api_project_detail(project_id: str, request: Request):
    """Detail satu proyek (HQ data)."""
    _require_admin(request)
    result = get_project_detail(project_id)
    return result


@router.post("/{project_id}/pin")
def api_pin_project(project_id: str, request: Request):
    """Toggle pin project."""
    _require_admin(request)
    result = pin_project(project_id)
    return result
