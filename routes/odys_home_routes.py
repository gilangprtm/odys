"""Odys Home API routes — /api/odys/home/*

Briefing, status, activity for Home Dashboard.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from services.odys_home_service import get_briefing
from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user

router = APIRouter(prefix="/api/odys/home", tags=["odys"])


def _require_admin(request: Request):
    user = get_current_user(request)
    if not owner_is_admin_or_single_user(user):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin only")


@router.get("/briefing")
def api_briefing(request: Request):
    """Home briefing: greeting, priorities, status, recommendation."""
    _require_admin(request)
    return get_briefing()
