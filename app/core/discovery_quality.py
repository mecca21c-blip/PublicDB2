"""Shared deterministic quality rules for extracted discovery evidence."""

from __future__ import annotations

import re
from enum import Enum
from typing import Iterable, TypeVar


class ContactScope(str, Enum):
    BUSINESS = "BUSINESS"
    SITE_WIDE = "SITE_WIDE"
    UNKNOWN = "UNKNOWN"


CONTACT_SCOPE_LABELS = {
    ContactScope.BUSINESS: "업무/본문 연락처",
    ContactScope.SITE_WIDE: "사이트 공통 연락처",
    ContactScope.UNKNOWN: "분류 미확인",
}

_NO_DATA_PHRASES = {
    "등록된데이터가없습니다",
    "등록된자료가없습니다",
    "검색결과가없습니다",
    "조회결과가없습니다",
    "자료가없습니다",
    "데이터가없습니다",
    "해당정보가없습니다",
}
_SITE_LOCATOR = re.compile(
    r"(?:^|[\s>.#:_-])(?:footer|header|site-footer|site-header|footer-wrap)(?:$|[\s>.#:_-])",
    re.IGNORECASE,
)
_BUSINESS_LOCATOR = re.compile(
    r"(?:^|[\s>.#:_-])(?:main|article|table|tbody|thead|tr|content|contents)(?:$|[\s>.#:_-])",
    re.IGNORECASE,
)
_SITE_CONTEXT = re.compile(
    r"(?:대표\s*(?:전화|번호|팩스)|당직|copyright|all rights reserved)",
    re.IGNORECASE,
)

T = TypeVar("T")


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def normalize_placeholder_text(value: str | None) -> str:
    """Normalize only punctuation and whitespace for conservative exact matching."""
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", value or "").casefold()


def is_no_data_placeholder(value: str | None) -> bool:
    normalized = normalize_placeholder_text(value)
    return bool(normalized) and normalized in _NO_DATA_PHRASES


def classify_contact_scope(
    source_locator: str | None,
    context_text: str | None,
) -> ContactScope:
    """Classify evidence from structure first, then strong contextual evidence."""
    locator = source_locator or ""
    if _SITE_LOCATOR.search(locator):
        return ContactScope.SITE_WIDE
    if _BUSINESS_LOCATOR.search(locator):
        return ContactScope.BUSINESS
    if _SITE_CONTEXT.search(context_text or ""):
        return ContactScope.SITE_WIDE
    return ContactScope.UNKNOWN


def contact_semantic_key(item: object) -> tuple[str, str]:
    return (
        _enum_value(getattr(item, "candidate_type", "")),
        str(getattr(item, "normalized_value", "") or ""),
    )


def _contact_preference(item: object) -> tuple[int, int, int, int, str, str]:
    locator = str(getattr(item, "source_locator", "") or "")
    context = " ".join(str(getattr(item, "context_text", "") or "").split())
    scope = classify_contact_scope(locator, context)
    scope_rank = {
        ContactScope.BUSINESS: 2,
        ContactScope.SITE_WIDE: 1,
        ContactScope.UNKNOWN: 0,
    }[scope]
    method = _enum_value(getattr(item, "detection_method", ""))
    method_rank = 1 if method in {"TEL_LINK", "MAILTO"} else 0
    depth = locator.count(" > ") + (1 if locator else 0)
    useful_context = 1 if context else 0
    # The final two values make the winner independent of input order.
    return (scope_rank, depth, method_rank, useful_context, locator, context)


def preferred_contact(current: T, new: T) -> T:
    return new if _contact_preference(new) > _contact_preference(current) else current


def deduplicate_contacts(items: Iterable[T]) -> list[T]:
    """Keep one deterministic candidate per type + normalized value."""
    selected: dict[tuple[str, str], T] = {}
    for item in items:
        key = contact_semantic_key(item)
        if not key[0] or not key[1]:
            continue
        selected[key] = item if key not in selected else preferred_contact(selected[key], item)
    return [selected[key] for key in sorted(selected)]


def meaningful_directory_values(
    *,
    row_text: str | None,
    org_unit_text: str | None,
    duty_text: str | None,
    position_text: str | None,
    person_name_text: str | None,
    phone_text: str | None,
    email_text: str | None,
    fax_text: str | None,
) -> bool:
    if is_no_data_placeholder(row_text):
        return False
    values = (
        org_unit_text,
        duty_text,
        position_text,
        person_name_text,
        phone_text,
        email_text,
        fax_text,
    )
    populated = [value for value in values if (value or "").strip()]
    if not populated:
        return False
    if all(is_no_data_placeholder(value) for value in populated):
        return False
    return any(not is_no_data_placeholder(value) for value in populated)
