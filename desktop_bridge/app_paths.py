"""Backward-compatible shim — delegates to app_registry."""

from __future__ import annotations

from app_registry import resolve_all_apps, resolve_app

__all__ = ["resolve_app", "resolve_all_apps", "FRIENDLY_ERRORS"]

FRIENDLY_ERRORS = {
    "cursor": "Cursor was not found. Set ODY_CURSOR_PATH to your Cursor.exe path.",
    "chrome": "Chrome was not found. Set ODY_CHROME_PATH to your chrome.exe path.",
    "brave": "Brave was not found. Set ODY_BRAVE_PATH to your brave.exe path.",
}
