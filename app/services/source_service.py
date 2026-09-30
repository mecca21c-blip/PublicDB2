"""Canonical source registration and contextual binding operations."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agency, CrawlRun, OrgUnit, RunStatus, Source, SourceBinding, StageStatus
from app.models.common import utc_now
from app.repositories.source_repository import SourceRepository
from app.services.normalization import collapse_whitespace, normalize_source_url


class SourceServiceError(ValueError):
    pass


class SourceBindingConflict(SourceServiceError):
    pass


def _scope_key(agency_id: uuid.UUID, org_unit_id: uuid.UUID | None) -> str:
    return f"org:{org_unit_id}" if org_unit_id else f"agency:{agency_id}"


class SourceService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = SourceRepository(session)

    def _validate_scope(self, agency_id: uuid.UUID, org_unit_id: uuid.UUID | None) -> tuple[Agency, OrgUnit | None]:
        agency = self.session.get(Agency, agency_id)
        if agency is None or not agency.active:
            raise SourceServiceError("기관을 찾을 수 없습니다.")
        unit = self.session.get(OrgUnit, org_unit_id) if org_unit_id else None
        if org_unit_id and (unit is None or not unit.active or unit.agency_id != agency_id):
            raise SourceServiceError("부서는 지정한 기관에 속해야 합니다.")
        return agency, unit

    def register_binding(
        self,
        *,
        url: str,
        agency_id: uuid.UUID,
        org_unit_id: uuid.UUID | None = None,
        description: str | None = None,
        commit: bool = True,
    ) -> tuple[dict, bool]:
        try:
            agency, unit = self._validate_scope(agency_id, org_unit_id)
            normalized = normalize_source_url(url)
            source = self.repository.get_source_by_normalized_url(normalized)
            if source is None:
                source = Source(url=url.strip(), normalized_url=normalized)
                self.repository.add_source(source)
                self.session.flush()
            scope = _scope_key(agency_id, org_unit_id)
            existing = self.repository.get_binding_by_scope(source.id, scope)
            if existing is not None:
                return self.binding_projection(existing), False
            binding = SourceBinding(
                source=source,
                agency=agency,
                org_unit=unit,
                scope_key=scope,
                description=collapse_whitespace(description or "") or None,
            )
            self.repository.add_binding(binding)
            self.session.commit() if commit else self.session.flush()
            return self.binding_projection(binding), True
        except Exception:
            if commit:
                self.session.rollback()
            raise

    def get_binding(self, binding_id: uuid.UUID) -> dict:
        binding = self.repository.get_binding(binding_id)
        if binding is None:
            raise SourceServiceError("수집 소스 연결을 찾을 수 없습니다.")
        return self.binding_projection(binding)

    def update_binding(
        self,
        binding_id: uuid.UUID,
        *,
        url: str | None = None,
        agency_id: uuid.UUID | None = None,
        org_unit_id: uuid.UUID | None | object = ...,
        description: str | None | object = ...,
    ) -> dict:
        try:
            binding = self.repository.get_binding(binding_id)
            if binding is None:
                raise SourceServiceError("수집 소스 연결을 찾을 수 없습니다.")
            target_agency_id = agency_id or binding.agency_id
            target_org_unit_id = binding.org_unit_id if org_unit_id is ... else org_unit_id
            agency, unit = self._validate_scope(target_agency_id, target_org_unit_id)
            source = binding.source
            if url is not None:
                normalized = normalize_source_url(url)
                source = self.repository.get_source_by_normalized_url(normalized)
                if source is None:
                    source = Source(url=url.strip(), normalized_url=normalized)
                    self.repository.add_source(source)
                    self.session.flush()
            scope = _scope_key(target_agency_id, target_org_unit_id)
            duplicate = self.repository.get_binding_by_scope(source.id, scope)
            if duplicate is not None and duplicate.id != binding.id:
                raise SourceBindingConflict("같은 URL과 기관/부서 연결이 이미 등록되어 있습니다.")
            binding.source = source
            binding.agency = agency
            binding.org_unit = unit
            binding.scope_key = scope
            if description is not ...:
                binding.description = collapse_whitespace(str(description or "")) or None
            self.session.commit()
            return self.binding_projection(binding)
        except Exception:
            self.session.rollback()
            raise

    def exclude_binding(self, binding_id: uuid.UUID, reason: str | None = None) -> dict:
        binding = self.repository.get_binding(binding_id)
        if binding is None:
            raise SourceServiceError("수집 소스 연결을 찾을 수 없습니다.")
        if binding.active:
            binding.active = False
            binding.exclusion_reason = collapse_whitespace(reason or "") or None
            binding.excluded_at = utc_now()
            self.session.commit()
        return self.binding_projection(binding)

    def reactivate_binding(self, binding_id: uuid.UUID) -> dict:
        binding = self.repository.get_binding(binding_id)
        if binding is None:
            raise SourceServiceError("수집 소스 연결을 찾을 수 없습니다.")
        if not binding.active:
            binding.active = True
            binding.exclusion_reason = None
            binding.excluded_at = None
            self.session.commit()
        return self.binding_projection(binding)

    def list_page(
        self,
        *,
        search: str | None = None,
        agency_id: uuid.UUID | None = None,
        org_unit_id: uuid.UUID | None = None,
        status: str | None = None,
    ) -> dict:
        bindings = self.repository.list_bindings(search=search, agency_id=agency_id, org_unit_id=org_unit_id)
        projected = [self.binding_projection(binding) for binding in bindings]
        if status:
            projected = [item for item in projected if item["status_code"] == status]
        return {"items": tuple(projected), "details": tuple(projected)}

    def _latest_run(self, source_id: uuid.UUID) -> CrawlRun | None:
        return self.session.scalar(
            select(CrawlRun)
            .where(
                CrawlRun.source_id == source_id,
                CrawlRun.status.in_((RunStatus.SUCCESS, RunStatus.PARTIAL, RunStatus.FAILED)),
            )
            .order_by(CrawlRun.started_at.desc())
            .limit(1)
        )

    def binding_projection(self, binding: SourceBinding) -> dict:
        run = self._latest_run(binding.source_id)
        if not binding.active:
            status_code, status, tone = "excluded", "제외", "warning"
        elif run is None:
            status_code, status, tone = "unchecked", "미확인", "neutral"
        elif run.status == RunStatus.SUCCESS and run.records_observed == 0 and all(
            value == StageStatus.SUCCESS for value in (run.connection_status, run.raw_status, run.extraction_status)
        ):
            status_code, status, tone = "empty", "자료없음", "info"
        elif run.status == RunStatus.SUCCESS:
            status_code, status, tone = "success", "정상", "success"
        else:
            status_code, status, tone = "error", "오류", "danger"
        checked = run.finished_at or run.started_at if run else None
        return {
            "id": str(binding.id),
            "binding_id": str(binding.id),
            "source_id": str(binding.source_id),
            "agency_id": str(binding.agency_id),
            "org_unit_id": str(binding.org_unit_id) if binding.org_unit_id else None,
            "agency": binding.agency.official_name,
            "department": binding.org_unit.name if binding.org_unit else "기관 공통",
            "url": binding.source.url,
            "normalized_url": binding.source.normalized_url,
            "coverage_mode": binding.source.coverage_mode.value,
            "description": binding.description or "-",
            "checked": checked.strftime("%Y-%m-%d %H:%M") if checked else "미확인",
            "found": run.records_observed if run else "-",
            "result": status,
            "status": status,
            "status_code": status_code,
            "tone": tone,
            "error": (run.error_summary if run else None) or binding.exclusion_reason or "-",
            "active": binding.active,
            "collection_supported": binding.source.collection_method.value == "WEB_PAGE",
            "collection_enabled": (
                binding.active
                and binding.source.active
                and binding.source.collection_method.value == "WEB_PAGE"
            ),
            "collection_disabled_reason": (
                "제외된 연결입니다."
                if not binding.active
                else "지원하지 않는 수집 방식입니다."
                if binding.source.collection_method.value != "WEB_PAGE"
                else "비활성 소스입니다."
                if not binding.source.active
                else None
            ),
            "excluded_at": binding.excluded_at.isoformat() if binding.excluded_at else None,
        }
