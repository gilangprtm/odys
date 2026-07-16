"""Odys Neuron API — /api/odys/neurons/*

Phase 1: activate, strengthen, decay, status, nodes CRUD.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from services import odys_neuron_service as neurons
from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user

router = APIRouter(prefix="/api/odys/neurons", tags=["odys"])


def _require_admin(request: Request):
    user = get_current_user(request)
    if not owner_is_admin_or_single_user(user):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin only")


class AddNodeBody(BaseModel):
    type: Literal["memory", "vault_note", "project"]
    label: str
    ref: str
    base_weight: float = 0.5
    text: Optional[str] = None
    node_id: Optional[str] = None


class ActivateBody(BaseModel):
    query: str = ""
    node_ids: Optional[list[str]] = None
    top_k: int = Field(default=10, ge=1, le=50)


class StrengthenBody(BaseModel):
    ids: list[str]


@router.get("/status")
def api_status(request: Request):
    _require_admin(request)
    return neurons.status()


@router.get("/nodes")
def api_list_nodes(request: Request, include_archived: bool = False):
    _require_admin(request)
    return neurons.list_nodes(include_archived=include_archived)


@router.post("/nodes")
def api_add_node(body: AddNodeBody, request: Request):
    _require_admin(request)
    return neurons.add_node(
        type=body.type,
        label=body.label,
        ref=body.ref,
        base_weight=body.base_weight,
        text=body.text,
        node_id=body.node_id,
    )


@router.post("/activate")
def api_activate(body: ActivateBody, request: Request):
    _require_admin(request)
    return neurons.activate(
        query=body.query,
        node_ids=body.node_ids,
        top_k=body.top_k,
    )


@router.post("/strengthen")
def api_strengthen(body: StrengthenBody, request: Request):
    _require_admin(request)
    return neurons.strengthen(body.ids)


@router.post("/decay")
def api_decay(request: Request):
    _require_admin(request)
    return neurons.decay()


@router.post("/sync-vault")
def api_sync_vault(request: Request):
    """Phase 3: scan Odys-Vault markdown → vault_note nodes (+ wikilink edges)."""
    _require_admin(request)
    from services.odys_neuron_hooks import sync_vault_notes
    return sync_vault_notes()
