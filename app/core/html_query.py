"""HTML workspace query parsing with browser-safe optional enum semantics."""

from __future__ import annotations

from enum import Enum
from typing import TypeVar


EnumValue = TypeVar("EnumValue", bound=Enum)


class HTMLFilterValueError(ValueError):
    """Raised for a non-blank invalid HTML workspace filter value."""


def parse_optional_enum(
    value: str | None, enum_type: type[EnumValue],
) -> EnumValue | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return enum_type(text)
    except ValueError as error:
        raise HTMLFilterValueError(
            f"Invalid {enum_type.__name__} workspace filter."
        ) from error
