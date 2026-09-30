"""Deterministic, header-driven extraction of staff-directory table rows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.models.enums import DirectoryRecordType


EXTRACTOR_NAME = "staff_directory"
EXTRACTOR_VERSION = "1"
MAX_ROW_TEXT_CHARS = 4000
MAX_CELL_TEXT_CHARS = 4000
MAX_LOCATOR_CHARS = 1000
REMOVED_TAGS = ("script", "style", "noscript", "template")

SEMANTIC_ALIASES: dict[str, tuple[str, ...]] = {
    "org_unit": ("부서명", "부서", "소속", "조직"),
    "duty": ("담당업무", "주요업무", "업무"),
    "position": ("직위", "직급"),
    "person_name": ("담당자", "성명", "이름"),
    "phone": ("전화번호", "연락처", "전화"),
    "email": ("전자우편", "이메일", "e-mail", "email"),
    "fax": ("팩스", "fax"),
}
CONTACT_SEMANTICS = {"phone", "email", "fax"}
SECONDARY_SEMANTICS = {
    "position",
    "person_name",
    "phone",
    "email",
    "fax",
}


@dataclass(frozen=True)
class ExtractedDirectoryRecordValue:
    record_type: DirectoryRecordType
    org_unit_text: str | None
    duty_text: str | None
    position_text: str | None
    person_name_text: str | None
    phone_text: str | None
    email_text: str | None
    fax_text: str | None
    row_text: str
    source_locator: str
    structured_payload: dict[str, Any]


@dataclass(frozen=True)
class _GridCell:
    tag: Tag
    text: str


@dataclass(frozen=True)
class _GridRow:
    tag: Tag
    cells: tuple[_GridCell | None, ...]


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _cell_text(cell: Tag) -> str:
    return _clean_text(cell.get_text(" ", strip=True))[:MAX_CELL_TEXT_CHARS]


def _positive_span(value: object) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def _table_rows(table: Tag) -> list[Tag]:
    return [
        row
        for row in table.find_all("tr")
        if row.find_parent("table") is table
    ]


def _expand_grid(table: Tag) -> list[_GridRow]:
    active: dict[int, tuple[int, _GridCell]] = {}
    expanded: list[tuple[Tag, dict[int, _GridCell]]] = []
    maximum_width = 0

    for row in _table_rows(table):
        row_cells: dict[int, _GridCell] = {
            column: cell for column, (_remaining, cell) in active.items()
        }
        new_spans: dict[int, tuple[int, _GridCell]] = {}
        column = 0

        for cell_tag in row.find_all(["th", "td"], recursive=False):
            while column in row_cells:
                column += 1
            cell = _GridCell(tag=cell_tag, text=_cell_text(cell_tag))
            colspan = _positive_span(cell_tag.get("colspan"))
            rowspan = _positive_span(cell_tag.get("rowspan"))
            for offset in range(colspan):
                target = column + offset
                row_cells[target] = cell
                if rowspan > 1:
                    new_spans[target] = (rowspan - 1, cell)
            column += colspan

        if not row_cells:
            continue

        maximum_width = max(maximum_width, max(row_cells) + 1)
        expanded.append((row, row_cells))

        next_active: dict[int, tuple[int, _GridCell]] = {}
        for target, (remaining, cell) in active.items():
            if remaining > 1:
                next_active[target] = (remaining - 1, cell)
        next_active.update(new_spans)
        active = next_active

    return [
        _GridRow(
            tag=row,
            cells=tuple(cells.get(column) for column in range(maximum_width)),
        )
        for row, cells in expanded
    ]


def _normalized_header(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]+", "", value).casefold()


def _semantic_for_headers(headers: list[str]) -> str | None:
    normalized_headers = [_normalized_header(header) for header in headers]
    for normalized in reversed(normalized_headers):
        for semantic, aliases in SEMANTIC_ALIASES.items():
            if normalized in {_normalized_header(alias) for alias in aliases}:
                return semantic
    for normalized in reversed(normalized_headers):
        for semantic, aliases in SEMANTIC_ALIASES.items():
            ordered_aliases = sorted(aliases, key=len, reverse=True)
            if any(_normalized_header(alias) in normalized for alias in ordered_aliases):
                return semantic
    return None


def _header_row_indices(rows: list[_GridRow]) -> list[int]:
    indices: list[int] = []
    started = False
    for index, row in enumerate(rows):
        direct_cells = row.tag.find_all(["th", "td"], recursive=False)
        in_thead = row.tag.find_parent("thead") is not None
        is_header = in_thead or any(cell.name == "th" for cell in direct_cells)
        if is_header:
            indices.append(index)
            started = True
        elif started:
            break
    return indices


def _column_map(
    rows: list[_GridRow],
    header_indices: list[int],
) -> tuple[dict[str, int], dict[str, list[str]]]:
    maximum_width = max((len(row.cells) for row in rows), default=0)
    mapping: dict[str, int] = {}
    recognized: dict[str, list[str]] = {}

    for column in range(maximum_width):
        headers: list[str] = []
        for row_index in header_indices:
            cell = rows[row_index].cells[column]
            if cell is not None and cell.text and cell.text not in headers:
                headers.append(cell.text)
        semantic = _semantic_for_headers(headers)
        if semantic is not None and semantic not in mapping:
            mapping[semantic] = column
            recognized[semantic] = headers
    return mapping, recognized


def _is_directory_mapping(mapping: dict[str, int]) -> bool:
    return (
        {"org_unit", "duty"} <= mapping.keys()
        and bool(SECONDARY_SEMANTICS & mapping.keys())
    )


def _collapse_duplicate_display(value: str) -> str:
    tokens = value.split()
    if len(tokens) >= 2 and len(tokens) % 2 == 0:
        midpoint = len(tokens) // 2
        if tokens[:midpoint] == tokens[midpoint:]:
            return " ".join(tokens[:midpoint])
    return value


def _value_at(
    row: _GridRow,
    mapping: dict[str, int],
    semantic: str,
) -> str | None:
    column = mapping.get(semantic)
    if column is None or column >= len(row.cells):
        return None
    cell = row.cells[column]
    if cell is None or not cell.text:
        return None
    value = cell.text
    if semantic in CONTACT_SEMANTICS:
        value = _collapse_duplicate_display(value)
    return value or None


def _row_text(row: _GridRow, mapping: dict[str, int]) -> str:
    texts: list[str] = []
    previous_cell: _GridCell | None = None
    semantic_by_column = {column: semantic for semantic, column in mapping.items()}
    for column, cell in enumerate(row.cells):
        if cell is None or cell is previous_cell:
            continue
        value = cell.text
        semantic = semantic_by_column.get(column)
        if semantic in CONTACT_SEMANTICS:
            value = _collapse_duplicate_display(value)
        if value:
            texts.append(value)
        previous_cell = cell
    return _clean_text(" ".join(texts))[:MAX_ROW_TEXT_CHARS]


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


class StaffDirectoryExtractor:
    """Extract evidence-derived directory rows without DB or network access."""

    name = EXTRACTOR_NAME
    version = EXTRACTOR_VERSION

    def extract(
        self,
        html: bytes | str,
        *,
        declared_charset: str | None = None,
    ) -> list[ExtractedDirectoryRecordValue]:
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

        records: list[ExtractedDirectoryRecordValue] = []
        for table in soup.find_all("table"):
            rows = _expand_grid(table)
            header_indices = _header_row_indices(rows)
            if not header_indices:
                continue
            mapping, recognized = _column_map(rows, header_indices)
            if not _is_directory_mapping(mapping):
                continue

            last_header_index = max(header_indices)
            for row_index, row in enumerate(rows):
                if row_index <= last_header_index:
                    continue
                direct_cells = row.tag.find_all(["th", "td"], recursive=False)
                if any(cell.name == "th" for cell in direct_cells):
                    continue

                values = {
                    semantic: _value_at(row, mapping, semantic)
                    for semantic in SEMANTIC_ALIASES
                }
                if not values["org_unit"] or not values["duty"]:
                    continue
                if not any(values[name] for name in SECONDARY_SEMANTICS):
                    continue
                row_text = _row_text(row, mapping)
                if not row_text:
                    continue

                raw_cells = [
                    cell.text if cell is not None else None for cell in row.cells
                ]
                records.append(
                    ExtractedDirectoryRecordValue(
                        record_type=DirectoryRecordType.STAFF_DIRECTORY_ROW,
                        org_unit_text=values["org_unit"],
                        duty_text=values["duty"],
                        position_text=values["position"],
                        person_name_text=values["person_name"],
                        phone_text=values["phone"],
                        email_text=values["email"],
                        fax_text=values["fax"],
                        row_text=row_text,
                        source_locator=_dom_path(row.tag),
                        structured_payload={
                            "recognized_headers": recognized,
                            "raw_cells": raw_cells,
                            "column_map": mapping,
                        },
                    )
                )
        return records
