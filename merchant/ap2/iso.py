"""Shared AP2 datetime helpers — DB-free and ORM-agnostic.

Single source of truth for datetime canonicalization used across AP2 payload
construction and verification. All functions are pure and deterministic so
signatures produced with these helpers verify identically in the standalone
verifier.
"""
from datetime import datetime, timezone
from typing import Any


def _iso(dt: Any) -> str:
    """Serialize a datetime to the exact ISO-8601 UTC string used at signing."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt.isoformat(timespec="seconds")
    return str(dt)


def utcnow() -> datetime:
    """Return the current UTC time with tzinfo stripped (naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_dt(value: Any) -> datetime | None:
    """Parse a value into a naive-UTC datetime, or return None on failure."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
