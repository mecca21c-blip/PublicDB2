"""Explicit timestamp contracts at machine and Korean user boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")
USER_DATETIME_FORMAT = "%Y-%m-%d %H:%M"


def to_kst(value: datetime | None) -> datetime | None:
    """Convert a canonical timestamp to Asia/Seoul without using machine local time.

    SQLAlchemy's ``UTCDateTime`` returns aware UTC values.  Naive values are also
    accepted at this boundary for legacy/raw SQLite projections and are treated
    as UTC explicitly.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SEOUL)


def format_kst_datetime(value: datetime | None, *, fallback: str = "-") -> str:
    converted = to_kst(value)
    return converted.strftime(USER_DATETIME_FORMAT) if converted is not None else fallback


def serialize_utc_datetime(value: datetime | None) -> str | None:
    """Return an unambiguous ISO 8601 value for machine/API consumers."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
