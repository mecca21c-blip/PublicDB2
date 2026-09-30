"""Deterministic HTML-only email, phone, and fax candidate extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from bs4 import BeautifulSoup, NavigableString, Tag

from app.models.enums import CandidateType, DetectionMethod


EXTRACTOR_NAME = "html_contact"
EXTRACTOR_VERSION = "2"
MAX_CONTEXT_CHARS = 1000
MAX_LOCATOR_CHARS = 1000
MAX_VALUE_CHARS = 500

EMAIL_PATTERN = re.compile(
    r"(?<![A-Z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"(?P<email>[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+)"
    r"(?![A-Z0-9_-])",
    re.IGNORECASE,
)

PHONE_PATTERN = re.compile(
    r"(?<!\d)(?P<phone>"
    r"(?:02|0(?:10|31|32|33|41|42|43|44|51|52|53|54|55|61|62|63|64|70|80))"
    r"[\s-]*\d{3,4}[\s-]*\d{4}"
    r"|1[568]\d{2}[\s-]*\d{4}"
    r")(?!\d)"
)

FAX_LABEL_PATTERN = re.compile(r"(?:\bfax\b|\uD329\uC2A4)", re.IGNORECASE)
EXAMPLE_LABEL_PATTERN = re.compile(
    r"(?:\uC608\uC2DC|\uC608|\uC0D8\uD50C|\bexample\b|\bsample\b)",
    re.IGNORECASE,
)
BLOCK_TAGS = {
    "address",
    "article",
    "body",
    "dd",
    "div",
    "dl",
    "dt",
    "li",
    "p",
    "section",
    "td",
    "th",
    "tr",
}
REMOVED_TAGS = ("script", "style", "noscript", "template")


@dataclass(frozen=True)
class ExtractedCandidateValue:
    candidate_type: CandidateType
    raw_value: str
    normalized_value: str
    context_text: str | None
    source_locator: str | None
    detection_method: DetectionMethod


def normalize_email(value: str) -> str:
    """Preserve the local part and lowercase only the email domain."""
    trimmed = value.strip().strip(".,;:()[]{}<>\"'")
    if not EMAIL_PATTERN.fullmatch(trimmed):
        raise ValueError("value is not a supported email address")
    local_part, domain = trimmed.rsplit("@", 1)
    return f"{local_part}@{domain.lower()}"


def normalize_phone(value: str) -> str:
    """Return digits only without inferring country codes or display formatting."""
    return "".join(character for character in value if character.isdigit())


def _is_supported_tel_value(value: str) -> bool:
    trimmed = value.strip()
    if PHONE_PATTERN.fullmatch(trimmed):
        return True
    if trimmed.startswith("+82"):
        digits = normalize_phone(trimmed)
        return 10 <= len(digits) <= 12
    return False


def _bounded_context(element: Tag) -> str | None:
    cell = (
        element
        if element.name in {"td", "th"}
        else element.find_parent(["td", "th"])
    )
    if cell is not None:
        context_element = cell.find_parent("tr") or cell
    elif element.name in BLOCK_TAGS:
        context_element = element
    else:
        context_element = element
        block = element.find_parent(BLOCK_TAGS)
        if block is not None:
            context_element = block

    text = " ".join(context_element.get_text(" ", strip=True).split())
    return text[:MAX_CONTEXT_CHARS] if text else None


def _safe_identity(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:100]


def _dom_path(element: Tag) -> str:
    segments: list[str] = []
    current: Tag | None = element
    while current is not None and current.name != "[document]":
        segment = current.name
        element_id = current.get("id")
        if isinstance(element_id, str) and element_id:
            safe_id = _safe_identity(element_id)
            if safe_id:
                segment += f"#{safe_id}"
        else:
            position = 1
            sibling = current.previous_sibling
            while sibling is not None:
                if isinstance(sibling, Tag) and sibling.name == current.name:
                    position += 1
                sibling = sibling.previous_sibling
            segment += f":nth-of-type({position})"
        segments.append(segment)
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return " > ".join(reversed(segments))[-MAX_LOCATOR_CHARS:]


def _fax_label_precedes(text: str, start: int) -> bool:
    prefix = text[max(0, start - 30) : start]
    return FAX_LABEL_PATTERN.search(prefix) is not None


def _canonical_context(context: str | None) -> str | None:
    canonical = " ".join((context or "").split()).strip()
    return canonical or None


def _context_position(context: str | None, raw_value: str) -> int:
    return (context or "").find(raw_value)


def _is_fax_occurrence(
    local_text: str,
    local_start: int,
    raw_value: str,
    context: str | None,
) -> bool:
    if _fax_label_precedes(local_text, local_start):
        return True
    context_start = _context_position(context, raw_value)
    return context_start >= 0 and _fax_label_precedes(context or "", context_start)


def _placeholder_local_digits(normalized_value: str) -> str | None:
    if normalized_value.startswith("02"):
        return normalized_value[2:]
    if normalized_value.startswith("0") and len(normalized_value) >= 10:
        return normalized_value[3:]
    return None


def _is_placeholder_phone(
    raw_value: str,
    normalized_value: str,
    local_text: str,
    local_start: int,
    context: str | None,
) -> bool:
    local_digits = _placeholder_local_digits(normalized_value)
    if not local_digits or set(local_digits) != {"0"}:
        return False

    prefix = local_text[max(0, local_start - 40) : local_start]
    if EXAMPLE_LABEL_PATTERN.search(prefix):
        return True
    context_start = _context_position(context, raw_value)
    if context_start < 0:
        return False
    context_prefix = (context or "")[max(0, context_start - 40) : context_start]
    return EXAMPLE_LABEL_PATTERN.search(context_prefix) is not None


METHOD_PRIORITY = {
    DetectionMethod.TEXT_PATTERN: 0,
    DetectionMethod.MAILTO: 1,
    DetectionMethod.TEL_LINK: 1,
}


def _semantic_key(
    candidate: ExtractedCandidateValue,
) -> tuple[CandidateType, str, str, str]:
    canonical_context = _canonical_context(candidate.context_text)
    if canonical_context is not None:
        return (
            candidate.candidate_type,
            candidate.normalized_value,
            "context",
            canonical_context,
        )
    return (
        candidate.candidate_type,
        candidate.normalized_value,
        "locator",
        candidate.source_locator or "",
    )


def _preferred_candidate(
    current: ExtractedCandidateValue,
    new: ExtractedCandidateValue,
) -> ExtractedCandidateValue:
    current_priority = METHOD_PRIORITY[current.detection_method]
    new_priority = METHOD_PRIORITY[new.detection_method]
    if new_priority != current_priority:
        return new if new_priority > current_priority else current
    current_locator = current.source_locator or ""
    new_locator = new.source_locator or ""
    return new if new_locator < current_locator else current


class HTMLContactExtractor:
    """Parse supplied HTML bytes only; no DB, network, or master-data writes."""

    name = EXTRACTOR_NAME
    version = EXTRACTOR_VERSION

    def extract(
        self,
        html: bytes | str,
        *,
        declared_charset: str | None = None,
    ) -> list[ExtractedCandidateValue]:
        if isinstance(html, bytes):
            encoding = declared_charset or "utf-8"
            try:
                document = html.decode(encoding, errors="replace")
            except LookupError:
                document = html.decode("utf-8", errors="replace")
        else:
            document = html

        soup = BeautifulSoup(document, "html.parser")
        for tag in soup.find_all(REMOVED_TAGS):
            tag.decompose()

        candidates: dict[
            tuple[CandidateType, str, str, str], ExtractedCandidateValue
        ] = {}

        def add(candidate: ExtractedCandidateValue) -> None:
            if (
                not candidate.raw_value
                or len(candidate.raw_value) > MAX_VALUE_CHARS
                or len(candidate.normalized_value) > MAX_VALUE_CHARS
            ):
                return
            key = _semantic_key(candidate)
            existing = candidates.get(key)
            candidates[key] = (
                candidate
                if existing is None
                else _preferred_candidate(existing, candidate)
            )

        for anchor in soup.find_all("a", href=True):
            href_value = anchor.get("href")
            if not isinstance(href_value, str):
                continue
            href = href_value.strip()
            locator = _dom_path(anchor)
            context = _bounded_context(anchor)
            lowered_href = href.lower()

            if lowered_href.startswith("mailto:"):
                address_part = unquote(href[7:].split("?", 1)[0])
                for match in EMAIL_PATTERN.finditer(address_part):
                    raw_value = match.group("email")
                    try:
                        normalized = normalize_email(raw_value)
                    except ValueError:
                        continue
                    add(
                        ExtractedCandidateValue(
                            candidate_type=CandidateType.EMAIL,
                            raw_value=raw_value,
                            normalized_value=normalized,
                            context_text=context,
                            source_locator=locator,
                            detection_method=DetectionMethod.MAILTO,
                        )
                    )

            if lowered_href.startswith("tel:"):
                raw_value = unquote(href[4:].split("?", 1)[0]).strip()
                if _is_supported_tel_value(raw_value):
                    anchor_text = anchor.get_text(" ", strip=True)
                    search_text = anchor_text or raw_value
                    raw_position = search_text.find(raw_value)
                    normalized = normalize_phone(raw_value)
                    if _is_placeholder_phone(
                        raw_value,
                        normalized,
                        search_text,
                        max(raw_position, 0),
                        context,
                    ):
                        continue
                    candidate_type = (
                        CandidateType.FAX
                        if _is_fax_occurrence(
                            search_text,
                            max(raw_position, 0),
                            raw_value,
                            context,
                        )
                        else CandidateType.PHONE
                    )
                    add(
                        ExtractedCandidateValue(
                            candidate_type=candidate_type,
                            raw_value=raw_value,
                            normalized_value=normalized,
                            context_text=context,
                            source_locator=locator,
                            detection_method=DetectionMethod.TEL_LINK,
                        )
                    )

        for text_node in soup.find_all(string=True):
            if not isinstance(text_node, NavigableString):
                continue
            text = str(text_node)
            if not text.strip() or not isinstance(text_node.parent, Tag):
                continue
            element = text_node.parent
            locator = _dom_path(element)
            context = _bounded_context(element)

            for match in EMAIL_PATTERN.finditer(text):
                raw_value = match.group("email")
                try:
                    normalized = normalize_email(raw_value)
                except ValueError:
                    continue
                add(
                    ExtractedCandidateValue(
                        candidate_type=CandidateType.EMAIL,
                        raw_value=raw_value,
                        normalized_value=normalized,
                        context_text=context,
                        source_locator=locator,
                        detection_method=DetectionMethod.TEXT_PATTERN,
                    )
                )

            for match in PHONE_PATTERN.finditer(text):
                raw_value = match.group("phone")
                normalized = normalize_phone(raw_value)
                if _is_placeholder_phone(
                    raw_value,
                    normalized,
                    text,
                    match.start("phone"),
                    context,
                ):
                    continue
                candidate_type = (
                    CandidateType.FAX
                    if _is_fax_occurrence(
                        text,
                        match.start("phone"),
                        raw_value,
                        context,
                    )
                    else CandidateType.PHONE
                )
                add(
                    ExtractedCandidateValue(
                        candidate_type=candidate_type,
                        raw_value=raw_value,
                        normalized_value=normalized,
                        context_text=context,
                        source_locator=locator,
                        detection_method=DetectionMethod.TEXT_PATTERN,
                    )
                )

        return list(candidates.values())
