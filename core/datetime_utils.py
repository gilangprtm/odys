# core/datetime_utils.py
"""Central datetime utility — single source of truth for UTC helpers.

All code SHOULD call ``utcnow()`` / ``utcnow_iso()`` / ``utcnow_str()``
instead of ``datetime.utcnow()`` (deprecated, removed in Python 3.14).
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return a naive UTC datetime (tzinfo=None) — safe for both
    DB columns (which store naive timestamps) and in-memory comparisons
    where callers explicitly compare against other naive values."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_iso() -> str:
    """ISO-8601 string with 'Z' suffix, e.g. 2026-07-17T12:34:56.789Z."""
    return datetime.now(timezone.utc).isoformat() + "Z"


def utcnow_str(fmt: str = "%Y-%m-%dT%H:%M:%S") -> str:
    """Formatted UTC string. Default: ISO-ish without Z."""
    return datetime.now(timezone.utc).strftime(fmt)
