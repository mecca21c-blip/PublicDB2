"""Conservative normalization used as confirmed-data write authority."""

from __future__ import annotations

import re

from app.models import ContactType


_SPACE = re.compile(r"\s+")
_PHONE = re.compile(r"[^0-9+]")
_SPLIT = re.compile(r"[;,|/\n]+")


def normalize_text(value: str | None) -> str:
    return _SPACE.sub(" ", (value or "").strip()).casefold()


def split_values(value: str | None) -> tuple[str, ...]:
    values = []
    for part in _SPLIT.split(value or ""):
        cleaned = _SPACE.sub(" ", part.strip())
        if cleaned and cleaned not in values:
            values.append(cleaned)
    return tuple(values)


def normalize_contact(contact_type: ContactType, value: str) -> str:
    cleaned = value.strip()
    if contact_type is ContactType.EMAIL:
        return cleaned.casefold()
    if contact_type in (ContactType.PHONE, ContactType.FAX):
        return _PHONE.sub("", cleaned)
    return normalize_text(cleaned)
