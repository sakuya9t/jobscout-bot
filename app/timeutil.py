"""Time helpers. The whole app stores timestamps as **naive UTC** so columns
compare apples-to-apples regardless of the server's local timezone (SQLite has
no tz type). Always use ``utcnow()`` rather than ``datetime.now()`` /
``datetime.utcnow()`` so this invariant holds everywhere."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (tzinfo stripped)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: datetime | None) -> datetime | None:
    """Normalize an aware/naive datetime to naive UTC for consistent storage."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
