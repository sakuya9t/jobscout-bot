"""Time helpers. The whole app stores timestamps as **naive UTC** so columns
compare apples-to-apples regardless of the server's local timezone (SQLite has
no tz type). Always use ``utcnow()`` rather than ``datetime.now()`` /
``datetime.utcnow()`` so this invariant holds everywhere."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (tzinfo stripped)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Unit-suffixed durations for human-friendly CLI flags (e.g. invite expiry): a run of
# <integer><unit> tokens, so "24h", "30m", "7d", "2w", and compound "1d12h" all parse.
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_RE = re.compile(r"(\d+)([smhdw])")


def parse_duration(text: str) -> timedelta:
    """Parse a duration like ``24h`` / ``30m`` / ``7d`` / ``2w`` / ``1d12h`` into a
    positive ``timedelta``. Units: s, m, h, d, w. Raises ``ValueError`` on anything that
    isn't a clean run of <number><unit> tokens, or that totals zero/negative."""
    normalized = text.strip().lower()
    matches = list(_DURATION_RE.finditer(normalized))
    # Reject stray characters: the matched tokens must cover the whole string, so
    # "24", "24hh", "24h ", and "abc" are all errors rather than silently ignored.
    if not matches or "".join(m.group(0) for m in matches) != normalized:
        raise ValueError(
            f"invalid duration {text!r}; use forms like 30m, 24h, 7d, 2w, or 1d12h"
        )
    seconds = sum(int(value) * _DURATION_UNITS[unit] for value, unit in (m.groups() for m in matches))
    if seconds <= 0:
        raise ValueError(f"duration must be positive, got {text!r}")
    return timedelta(seconds=seconds)


def to_naive_utc(value: datetime | None) -> datetime | None:
    """Normalize an aware/naive datetime to naive UTC for consistent storage."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
