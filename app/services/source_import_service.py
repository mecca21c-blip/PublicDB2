"""Two-phase XLSX/CSV source import with DB revalidation on confirm."""

from __future__ import annotations

import csv
import hashlib
import io
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import RuntimePaths, ensure_runtime_directories, runtime_paths
from app.models import Agency, AgencyType, CollectionMethod, OrgUnit, OrgUnitType, Source, SourceBinding, SourceImportLog
from app.models.common import utc_now
from app.services.agency_service import AgencyService
from app.services.normalization import (
    SourceURLValidationError,
    collapse_whitespace,
    normalize_agency_name,
    normalize_org_unit_name,
    normalize_source_url,
)
from app.services.source_service import SourceService


MAX_IMPORT_ROWS = 10_000
MAX_IMPORT_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".xlsx", ".csv"}
HEADER_ALIASES = {
    "agency": {"기관명", "기관", "agency", "agencyname", "agency_name"},
    "org_unit": {"부서명", "부서", "조직명", "department", "orgunit", "org_unit"},
    "url": {"url", "주소", "sourceurl", "source_url", "수집url"},
    "description": {"소스설명", "설명", "description", "source_description"},
    "collection_method": {"수집방식", "collectionmethod", "collection_method", "방식"},
}
IMPORT_METHODS = {
    "": CollectionMethod.WEB_PAGE,
    "스크래핑": CollectionMethod.WEB_PAGE,
    "개별url·스크래핑": CollectionMethod.WEB_PAGE,
    "web_page": CollectionMethod.WEB_PAGE,
    "크롤링": CollectionMethod.WEB_CRAWL,
    "indexurl·크롤링": CollectionMethod.WEB_CRAWL,
    "web_crawl": CollectionMethod.WEB_CRAWL,
}


class ImportCategory(str, Enum):
    READY = "READY"
    NEW_AGENCY = "NEW_AGENCY"
    NEW_ORG_UNIT = "NEW_ORG_UNIT"
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    EXISTING_SOURCE_NEW_BINDING = "EXISTING_SOURCE_NEW_BINDING"
    CONFLICT = "CONFLICT"
    INVALID = "INVALID"


class SourceImportError(ValueError):
    pass


@dataclass(frozen=True)
class PreviewFile:
    token: str
    original_filename: str
    temp_path: Path
    sha256: str


class PreviewStore:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or runtime_paths()
        self._entries: dict[str, PreviewFile] = {}
        self._lock = threading.Lock()

    def save(self, filename: str, content: bytes) -> PreviewFile:
        self.cleanup_expired()
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise SourceImportError("지원 형식은 .xlsx와 .csv입니다.")
        ensure_runtime_directories(self.paths)
        token = uuid.uuid4().hex
        path = self.paths.temp_root / f"source-import-{token}{suffix}"
        path.write_bytes(content)
        entry = PreviewFile(token, Path(filename).name[:500], path, hashlib.sha256(content).hexdigest())
        with self._lock:
            self._entries[token] = entry
        return entry

    def get(self, token: str) -> PreviewFile:
        with self._lock:
            entry = self._entries.get(token)
        if entry is None or not entry.temp_path.is_file():
            raise SourceImportError("미리보기가 없거나 만료되었습니다. 파일을 다시 선택하세요.")
        return entry

    def discard(self, token: str) -> None:
        with self._lock:
            entry = self._entries.pop(token, None)
        if entry is not None:
            entry.temp_path.unlink(missing_ok=True)

    def cleanup_expired(self, max_age_seconds: int = 3600) -> None:
        cutoff = time.time() - max_age_seconds
        with self._lock:
            expired = [token for token, entry in self._entries.items() if not entry.temp_path.exists() or entry.temp_path.stat().st_mtime < cutoff]
        for token in expired:
            self.discard(token)


def _header_key(value: Any) -> str:
    return collapse_whitespace(str(value or "")).lower().replace(" ", "")


def _column_map(headers: list[Any]) -> dict[str, int]:
    mapped: dict[str, int] = {}
    for index, header in enumerate(headers):
        key = _header_key(header)
        for field, aliases in HEADER_ALIASES.items():
            if key in aliases and field not in mapped:
                mapped[field] = index
    missing = [name for name in ("agency", "url") if name not in mapped]
    if missing:
        labels = {"agency": "기관명", "url": "URL"}
        raise SourceImportError("필수 컬럼이 없습니다: " + ", ".join(labels[name] for name in missing))
    return mapped


def _cell(values: list[Any], index: int | None) -> str:
    if index is None or index >= len(values) or values[index] is None:
        return ""
    return collapse_whitespace(str(values[index]))


def parse_import_file(filename: str, content: bytes, row_limit: int = MAX_IMPORT_ROWS) -> list[dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise SourceImportError("지원 형식은 .xlsx와 .csv입니다.")
    if not content:
        raise SourceImportError("빈 파일은 가져올 수 없습니다.")
    if len(content) > MAX_IMPORT_BYTES:
        raise SourceImportError("파일 크기는 20MB를 초과할 수 없습니다.")
    if suffix == ".xlsx":
        try:
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            sheet = workbook.active
            iterator = sheet.iter_rows(values_only=True)
            headers = list(next(iterator))
            source_rows = ((number, list(values)) for number, values in enumerate(iterator, start=2))
        except (StopIteration, OSError, ValueError, KeyError) as error:
            raise SourceImportError("XLSX 파일을 읽을 수 없습니다.") from error
    else:
        try:
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = content.decode("cp949")
            iterator = csv.reader(io.StringIO(text))
            headers = next(iterator)
            source_rows = ((number, list(values)) for number, values in enumerate(iterator, start=2))
        except (StopIteration, UnicodeDecodeError, csv.Error) as error:
            raise SourceImportError("CSV 파일을 읽을 수 없습니다.") from error
    columns = _column_map(headers)
    rows: list[dict[str, Any]] = []
    for row_number, values in source_rows:
        if not any(collapse_whitespace(str(value or "")) for value in values):
            continue
        if len(rows) >= row_limit:
            raise SourceImportError(f"가져오기 행은 최대 {row_limit:,}개입니다.")
        rows.append({
            "row_number": row_number,
            "raw_agency": _cell(values, columns["agency"]),
            "raw_org_unit": _cell(values, columns.get("org_unit")),
            "raw_url": _cell(values, columns["url"]),
            "description": _cell(values, columns.get("description")),
            "raw_collection_method": _cell(values, columns.get("collection_method")),
        })
    if not rows:
        raise SourceImportError("가져올 데이터 행이 없습니다.")
    return rows


class SourceImportService:
    def __init__(self, session: Session, paths: RuntimePaths | None = None) -> None:
        self.session = session
        self.paths = paths or runtime_paths()

    def preview(self, filename: str, content: bytes) -> dict[str, Any]:
        return self.classify(parse_import_file(filename, content))

    def classify(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        agencies = list(self.session.scalars(select(Agency).where(Agency.active.is_(True))))
        units = list(self.session.scalars(select(OrgUnit).where(OrgUnit.active.is_(True))))
        sources = list(self.session.scalars(select(Source)))
        bindings = list(self.session.scalars(select(SourceBinding)))
        agency_map: dict[str, list[Agency]] = {}
        for agency in agencies:
            agency_map.setdefault(agency.normalized_name, []).append(agency)
        unit_map: dict[tuple[uuid.UUID, str], list[OrgUnit]] = {}
        for unit in units:
            unit_map.setdefault((unit.agency_id, unit.normalized_name), []).append(unit)
        source_map = {source.normalized_url: source for source in sources}
        binding_keys = {(binding.source_id, binding.scope_key) for binding in bindings}
        seen: dict[tuple[str, str, str], CollectionMethod] = {}
        projected: list[dict[str, Any]] = []

        for raw in rows:
            item = dict(raw)
            item.update({"normalized_url": None, "resolved_agency": None, "resolved_org_unit": None, "agency_id": None, "org_unit_id": None, "will_create_agency": False, "will_create_org_unit": False})
            category, message, importable = self._classify_row(item, agency_map, unit_map, source_map, binding_keys, seen)
            item.update({"classification": category.value, "message": message, "importable": importable})
            projected.append(item)

        category_counts = {category.value: sum(item["classification"] == category.value for item in projected) for category in ImportCategory}
        summary = {
            "total": len(projected),
            "importable": sum(item["importable"] for item in projected),
            "new_agencies": sum(item["will_create_agency"] for item in projected if item["importable"]),
            "new_org_units": sum(item["will_create_org_unit"] for item in projected if item["importable"]),
            "existing_source_new_bindings": category_counts[ImportCategory.EXISTING_SOURCE_NEW_BINDING.value],
            "duplicates": category_counts[ImportCategory.EXACT_DUPLICATE.value],
            "conflicts": category_counts[ImportCategory.CONFLICT.value],
            "invalid": category_counts[ImportCategory.INVALID.value],
            "categories": category_counts,
        }
        return {"rows": projected, "summary": summary}

    def _classify_row(self, item: dict[str, Any], agency_map: dict[str, list[Agency]], unit_map: dict[tuple[uuid.UUID, str], list[OrgUnit]], source_map: dict[str, Source], binding_keys: set[tuple[uuid.UUID, str]], seen: dict[tuple[str, str, str], CollectionMethod]) -> tuple[ImportCategory, str, bool]:
        agency_name = normalize_agency_name(item["raw_agency"])
        if not agency_name:
            return ImportCategory.INVALID, "기관명은 필수입니다.", False
        if not item["raw_url"]:
            return ImportCategory.INVALID, "URL은 필수입니다.", False
        try:
            normalized_url = normalize_source_url(item["raw_url"])
        except SourceURLValidationError as error:
            return ImportCategory.INVALID, str(error), False
        item["normalized_url"] = normalized_url
        method_key = item.get("raw_collection_method", "").replace(" ", "").casefold()
        method = IMPORT_METHODS.get(method_key)
        if method is None:
            return ImportCategory.INVALID, "Excel에서는 스크래핑 또는 크롤링만 가져올 수 있습니다. API/RSS는 전용 화면을 사용하세요.", False
        item["collection_method"] = method.value
        source_method_key = (normalized_url, "__canonical_method__", "__canonical_method__")
        if source_method_key in seen and seen[source_method_key] is not method:
            return ImportCategory.CONFLICT, "파일 내부에서 같은 canonical URL의 수집 방식이 충돌합니다.", False
        seen[source_method_key] = method
        agency_matches = agency_map.get(agency_name, [])
        if len(agency_matches) > 1:
            return ImportCategory.CONFLICT, "정규화 기관명이 여러 기존 기관과 충돌합니다.", False
        agency = agency_matches[0] if agency_matches else None
        item["resolved_agency"] = agency.official_name if agency else agency_name
        item["agency_id"] = str(agency.id) if agency else None
        item["will_create_agency"] = agency is None

        org_name = normalize_org_unit_name(item["raw_org_unit"])
        unit = None
        if org_name and agency:
            matches = unit_map.get((agency.id, org_name), [])
            if len(matches) > 1:
                return ImportCategory.CONFLICT, "기관 내 정규화 부서명이 여러 부서와 충돌합니다.", False
            unit = matches[0] if matches else None
        item["resolved_org_unit"] = unit.name if unit else (org_name or None)
        item["org_unit_id"] = str(unit.id) if unit else None
        item["will_create_org_unit"] = bool(org_name and unit is None)

        agency_identity = f"id:{agency.id}" if agency else f"new:{agency_name}"
        org_identity = f"id:{unit.id}" if unit else (f"new:{org_name}" if org_name else "agency")
        semantic_key = (normalized_url, agency_identity, org_identity)
        if semantic_key in seen:
            if seen[semantic_key] is not method:
                return ImportCategory.CONFLICT, "파일 내부에서 같은 URL과 기관/부서의 수집 방식이 충돌합니다.", False
            return ImportCategory.EXACT_DUPLICATE, "파일 내부에서 같은 URL과 기관/부서가 중복되었습니다.", False
        seen[semantic_key] = method

        source = source_map.get(normalized_url)
        if source and source.collection_method is not method:
            return ImportCategory.CONFLICT, "기존 canonical URL의 수집 방식과 충돌합니다.", False
        if source and agency and (source.id, f"org:{unit.id}" if unit else f"agency:{agency.id}") in binding_keys and not item["will_create_org_unit"]:
            return ImportCategory.EXACT_DUPLICATE, "같은 URL과 기관/부서 연결이 이미 등록되어 있습니다.", False
        if agency is None:
            return ImportCategory.NEW_AGENCY, "기관을 새로 만들고 URL을 연결합니다.", True
        if org_name and unit is None:
            return ImportCategory.NEW_ORG_UNIT, "기관 아래 부서를 새로 만들고 URL을 연결합니다.", True
        if source:
            return ImportCategory.EXISTING_SOURCE_NEW_BINDING, "기존 canonical URL에 새 기관/부서 연결을 만듭니다.", True
        return ImportCategory.READY, "새 URL과 연결을 등록합니다.", True

    def confirm(self, entry: PreviewFile) -> dict[str, Any]:
        content = entry.temp_path.read_bytes()
        preview = self.preview(entry.original_filename, content)
        preexisting_urls = set(self.session.scalars(select(Source.normalized_url)))
        retained_path = self._retain_file(entry, content)
        counts = {
            "input_rows": preview["summary"]["total"], "created_agencies": 0, "created_org_units": 0,
            "created_sources": 0, "created_bindings": 0, "existing_source_new_bindings": 0,
            "duplicates_skipped": 0, "invalid_rows": 0, "conflicts": 0, "unexpected_failures": 0,
        }
        counts["duplicates_skipped"] = preview["summary"]["duplicates"]
        counts["invalid_rows"] = preview["summary"]["invalid"]
        counts["conflicts"] = preview["summary"]["conflicts"]
        self.session.rollback()
        try:
            with self.session.begin():
                agency_service = AgencyService(self.session)
                source_service = SourceService(self.session)
                for row in preview["rows"]:
                    if not row["importable"]:
                        continue
                    try:
                        with self.session.begin_nested():
                            created_agency = False
                            created_org_unit = False
                            agency_id = uuid.UUID(row["agency_id"]) if row["agency_id"] else None
                            if agency_id is None:
                                agency_data, created_agency = agency_service.create_agency(official_name=row["resolved_agency"], agency_type=AgencyType.OTHER, commit=False)
                                agency_id = uuid.UUID(agency_data["id"])
                            org_unit_id = uuid.UUID(row["org_unit_id"]) if row["org_unit_id"] else None
                            if row["will_create_org_unit"]:
                                normalized_unit = normalize_org_unit_name(row["resolved_org_unit"])
                                created_org_unit = self.session.scalar(select(OrgUnit.id).where(OrgUnit.agency_id == agency_id, OrgUnit.normalized_name == normalized_unit, OrgUnit.active.is_(True))) is None
                                unit = agency_service.create_org_unit(agency_id=agency_id, name=row["resolved_org_unit"], unit_type=OrgUnitType.DEPARTMENT, commit=False)
                                org_unit_id = uuid.UUID(unit["id"])
                            source_existed = self.session.scalar(select(Source.id).where(Source.normalized_url == row["normalized_url"])) is not None
                            _binding, created_binding = source_service.register_binding(
                                url=row["raw_url"], agency_id=agency_id, org_unit_id=org_unit_id,
                                description=row["description"], collection_method=row["collection_method"],
                                method_config={}, commit=False,
                            )
                            counts["created_agencies"] += int(created_agency)
                            counts["created_org_units"] += int(created_org_unit)
                            counts["created_sources"] += int(not source_existed)
                            counts["created_bindings"] += int(created_binding)
                            counts["existing_source_new_bindings"] += int(row["normalized_url"] in preexisting_urls and created_binding)
                    except Exception:
                        counts["unexpected_failures"] += 1
                relative = retained_path.relative_to(self.paths.project_root).as_posix()
                self.session.add(SourceImportLog(original_filename=entry.original_filename, stored_path=relative, sha256=entry.sha256, confirmed_at=utc_now(), input_rows=counts["input_rows"], result_counts=counts))
        except Exception:
            retained_path.unlink(missing_ok=True)
            raise
        return {"summary": counts, "rows": preview["rows"], "file": {"original_filename": entry.original_filename, "stored_path": retained_path.relative_to(self.paths.project_root).as_posix(), "sha256": entry.sha256, "confirmed_at": utc_now().isoformat()}}

    def _retain_file(self, entry: PreviewFile, content: bytes) -> Path:
        now = datetime.now().astimezone()
        destination = self.paths.import_root / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
        destination.mkdir(parents=True, exist_ok=True)
        suffix = Path(entry.original_filename).suffix.lower()
        target = destination / f"{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex}{suffix}"
        target.write_bytes(content)
        return target

