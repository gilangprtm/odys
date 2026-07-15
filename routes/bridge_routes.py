"""Desktop Bridge proxy routes — /api/bridge/*

Proxy ke Windows Desktop Bridge (host:8765) tanpa expose token ke client.
Admin-only. Token dari env ODY_BRIDGE_TOKEN.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bridge", tags=["bridge"])


class BridgeCommand(BaseModel):
    command: str = Field(..., min_length=1, max_length=64)
    args: Dict[str, Any] = Field(default_factory=dict)


class BridgeTTS(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


def _bridge_url() -> str:
    # Native Windows: 127.0.0.1. Docker: host.docker.internal (set via compose).
    return (os.getenv("ODY_BRIDGE_URL") or "http://127.0.0.1:8765").rstrip("/")


def _bridge_token() -> str:
    return (os.getenv("ODY_BRIDGE_TOKEN") or "").strip()


def _bridge_headers() -> Dict[str, str]:
    token = _bridge_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Desktop Bridge token not configured (set ODY_BRIDGE_TOKEN)",
        )
    return {
        "Content-Type": "application/json",
        "X-Odys-Bridge-Token": token,
    }


@router.get("/health")
async def bridge_health(request: Request) -> Dict[str, Any]:
    """Status bridge. Host /health gak butuh token."""
    require_admin(request)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{_bridge_url()}/health")
            data = r.json() if r.content else {}
            data["proxy"] = {
                "bridge_url": _bridge_url(),
                "token_configured": bool(_bridge_token()),
            }
            return data
    except Exception as exc:
        return {
            "ok": False,
            "service": "odys-desktop-bridge",
            "error": str(exc),
            "proxy": {
                "bridge_url": _bridge_url(),
                "token_configured": bool(_bridge_token()),
            },
        }


@router.get("/apps")
async def bridge_apps(request: Request) -> Dict[str, Any]:
    require_admin(request)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{_bridge_url()}/apps")
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Bridge unreachable: {exc}") from exc


@router.post("/command")
async def bridge_command(body: BridgeCommand, request: Request) -> Dict[str, Any]:
    require_admin(request)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{_bridge_url()}/command",
                json={"command": body.command, "args": body.args},
                headers=_bridge_headers(),
            )
            if r.status_code >= 400:
                try:
                    detail = r.json()
                except Exception:
                    detail = r.text
                raise HTTPException(status_code=r.status_code, detail=detail)
            return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Bridge error: {exc}") from exc


@router.post("/tts")
async def bridge_tts(body: BridgeTTS, request: Request) -> Dict[str, Any]:
    require_admin(request)
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{_bridge_url()}/tts",
                json={"text": body.text},
                headers=_bridge_headers(),
            )
            if r.status_code >= 400:
                try:
                    detail = r.json()
                except Exception:
                    detail = r.text
                raise HTTPException(status_code=r.status_code, detail=detail)
            return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Bridge TTS error: {exc}") from exc
