"""Bounded interactive multi-URL WEB_PAGE preview and registration."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import CollectionMethod, Source, SourceBinding
from app.services.normalization import SourceURLValidationError, normalize_source_url
from app.services.source_method_service import MethodConfigError, SourceMethodService
from app.services.source_service import SourceBindingConflict, SourceService, SourceServiceError


MAX_INTERACTIVE_SCRAPE_URLS = 200


class ScrapeURLClass(str, Enum):
    NEW_SOURCE = "NEW_SOURCE"
    EXISTING_SOURCE = "EXISTING_SOURCE"
    DUPLICATE_INPUT = "DUPLICATE_INPUT"
    INVALID_URL = "INVALID_URL"


class BindingClass(str, Enum):
    NEW_BINDING = "NEW_BINDING"
    EXACT_BINDING_DUPLICATE = "EXACT_BINDING_DUPLICATE"
    CONFLICT = "CONFLICT"


class ScrapeBatchError(ValueError):
    pass


class ScrapeBatchService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _candidate_lines(urls: str) -> tuple[list[tuple[int, str]], int]:
        raw_lines = urls.splitlines()
        candidates = [(index, value.strip()) for index, value in enumerate(raw_lines, 1) if value.strip()]
        if not candidates:
            raise ScrapeBatchError("대상 URL을 한 줄에 하나 이상 입력하세요.")
        if len(candidates) > MAX_INTERACTIVE_SCRAPE_URLS:
            raise ScrapeBatchError(
                f"한 번에 최대 {MAX_INTERACTIVE_SCRAPE_URLS}개 URL을 등록할 수 있습니다. "
                "더 큰 목록이나 서로 다른 연결 대상은 엑셀 업로드를 사용하세요."
            )
        return candidates, len(raw_lines)

    def _scope(
        self, agency_id: uuid.UUID, org_unit_id: uuid.UUID | None, binding_scope: str,
    ) -> tuple[uuid.UUID | None, str]:
        if binding_scope not in {"AGENCY_WIDE", "SPECIFIC_ORG_UNIT"}:
            raise ScrapeBatchError("연결 범위를 선택하세요.")
        if binding_scope == "SPECIFIC_ORG_UNIT" and org_unit_id is None:
            raise ScrapeBatchError("특정 부서 연결에는 부서 선택이 필요합니다.")
        if binding_scope == "AGENCY_WIDE":
            org_unit_id = None
        SourceService(self.session).validate_scope(agency_id, org_unit_id)
        scope_key = f"org:{org_unit_id}" if org_unit_id else f"agency:{agency_id}"
        return org_unit_id, scope_key

    def preview(
        self, *, urls: str, agency_id: uuid.UUID, org_unit_id: uuid.UUID | None,
        binding_scope: str,
    ) -> dict[str, Any]:
        candidates, input_lines = self._candidate_lines(urls)
        org_unit_id, scope_key = self._scope(agency_id, org_unit_id, binding_scope)
        normalized_by_line: dict[int, str] = {}
        invalid_by_line: dict[int, str] = {}
        for line_number, raw_url in candidates:
            try:
                normalized_by_line[line_number] = normalize_source_url(raw_url)
            except SourceURLValidationError as error:
                invalid_by_line[line_number] = str(error)
        normalized_values = set(normalized_by_line.values())
        sources = list(self.session.scalars(select(Source).where(Source.normalized_url.in_(normalized_values)))) if normalized_values else []
        source_map = {source.normalized_url: source for source in sources}
        source_ids = [source.id for source in sources]
        binding_source_ids = set(self.session.scalars(
            select(SourceBinding.source_id).where(
                SourceBinding.source_id.in_(source_ids), SourceBinding.scope_key == scope_key,
            )
        )) if source_ids else set()
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for line_number, raw_url in candidates:
            if line_number in invalid_by_line:
                rows.append({
                    "line_number": line_number, "raw_url": raw_url, "normalized_url": None,
                    "classification": ScrapeURLClass.INVALID_URL.value,
                    "binding_classification": None, "importable": False,
                    "message": invalid_by_line[line_number],
                })
                continue
            normalized = normalized_by_line[line_number]
            if normalized in seen:
                rows.append({
                    "line_number": line_number, "raw_url": raw_url, "normalized_url": normalized,
                    "classification": ScrapeURLClass.DUPLICATE_INPUT.value,
                    "binding_classification": None, "importable": False,
                    "message": "입력 안에서 같은 canonical URL이 반복되었습니다.",
                })
                continue
            seen.add(normalized)
            source = source_map.get(normalized)
            classification = ScrapeURLClass.EXISTING_SOURCE if source else ScrapeURLClass.NEW_SOURCE
            if source and source.collection_method is not CollectionMethod.WEB_PAGE:
                binding_classification = BindingClass.CONFLICT
                message = "기존 canonical URL이 다른 수집 방식으로 등록되어 있습니다."
                importable = False
            elif source and source.id in binding_source_ids:
                binding_classification = BindingClass.EXACT_BINDING_DUPLICATE
                message = "같은 URL과 기관/부서 연결이 이미 등록되어 있습니다."
                importable = False
            else:
                binding_classification = BindingClass.NEW_BINDING
                message = "기존 Source를 재사용해 새 연결을 만듭니다." if source else "새 Source와 연결을 만듭니다."
                importable = True
            rows.append({
                "line_number": line_number, "raw_url": raw_url, "normalized_url": normalized,
                "classification": classification.value,
                "binding_classification": binding_classification.value,
                "importable": importable, "message": message,
            })
        count = lambda value: sum(row["classification"] == value for row in rows)
        binding_count = lambda value: sum(row["binding_classification"] == value for row in rows)
        summary = {
            "input_lines": input_lines,
            "input_urls": len(candidates),
            "new_sources": count(ScrapeURLClass.NEW_SOURCE.value),
            "existing_sources": count(ScrapeURLClass.EXISTING_SOURCE.value),
            "duplicate_input": count(ScrapeURLClass.DUPLICATE_INPUT.value),
            "invalid_urls": count(ScrapeURLClass.INVALID_URL.value),
            "new_bindings": binding_count(BindingClass.NEW_BINDING.value),
            "exact_binding_duplicates": binding_count(BindingClass.EXACT_BINDING_DUPLICATE.value),
            "conflicts": binding_count(BindingClass.CONFLICT.value),
            "importable": sum(row["importable"] for row in rows),
            "blank_lines_ignored": input_lines - len(candidates),
        }
        return {"summary": summary, "rows": rows, "connection": {
            "agency_id": str(agency_id),
            "org_unit_id": str(org_unit_id) if org_unit_id else None,
            "binding_scope": binding_scope,
        }}

    def confirm(
        self, *, urls: str, agency_id: uuid.UUID, org_unit_id: uuid.UUID | None,
        binding_scope: str, description: str | None, method_config: dict,
        scheduled_refresh_enabled: bool,
    ) -> dict[str, Any]:
        if not bool(method_config.get("extract_contacts", True)) and not bool(method_config.get("extract_directory", True)):
            raise MethodConfigError("연락처 또는 직원/업무 명부 추출을 하나 이상 선택하세요.")
        preview = self.preview(
            urls=urls, agency_id=agency_id, org_unit_id=org_unit_id,
            binding_scope=binding_scope,
        )
        resolved_org_id = uuid.UUID(preview["connection"]["org_unit_id"]) if preview["connection"]["org_unit_id"] else None
        counts = {
            "input": preview["summary"]["input_urls"], "created_sources": 0,
            "existing_sources_reused": 0, "created_bindings": 0,
            "duplicates_skipped": preview["summary"]["duplicate_input"] + preview["summary"]["exact_binding_duplicates"],
            "errors": preview["summary"]["invalid_urls"] + preview["summary"]["conflicts"],
        }
        source_ids: list[str] = []
        result_rows = [dict(row) for row in preview["rows"]]
        service = SourceService(self.session)
        for row in result_rows:
            if not row["importable"]:
                row["result"] = "SKIPPED"
                continue
            try:
                with self.session.begin_nested():
                    existing = self.session.scalar(select(Source).where(Source.normalized_url == row["normalized_url"]))
                    item, created_binding = service.register_binding(
                        url=row["raw_url"], agency_id=agency_id, org_unit_id=resolved_org_id,
                        description=description, collection_method=CollectionMethod.WEB_PAGE,
                        method_config=method_config, scheduled_refresh_enabled=scheduled_refresh_enabled,
                        commit=False,
                    )
                    source = self.session.get(Source, uuid.UUID(item["source_id"]))
                    SourceMethodService(self.session).configure(
                        source, CollectionMethod.WEB_PAGE, method_config, commit=False,
                    )
                    source.scheduled_refresh_enabled = scheduled_refresh_enabled
                    counts["created_sources"] += int(existing is None)
                    counts["existing_sources_reused"] += int(existing is not None)
                    counts["created_bindings"] += int(created_binding)
                    if item["source_id"] not in source_ids:
                        source_ids.append(item["source_id"])
                    row["result"] = "CREATED"
            except (SourceServiceError, SourceBindingConflict, SourceURLValidationError, MethodConfigError, ValueError) as error:
                counts["errors"] += 1
                row["result"] = "ERROR"
                row["message"] = str(error)
            except SQLAlchemyError:
                counts["errors"] += 1
                row["result"] = "ERROR"
                row["message"] = "동시 등록 충돌로 이 URL을 등록하지 못했습니다. 다시 미리보기를 실행하세요."
        self.session.commit()
        return {"summary": counts, "rows": result_rows, "source_ids": source_ids}
