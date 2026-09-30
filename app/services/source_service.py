"""Canonical source registration and contextual binding operations."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Agency, CollectionMethod, CrawlRun, OrgUnit, RunStatus, Source, SourceBinding, StageStatus
from app.models.common import utc_now
from app.repositories.source_repository import SourceRepository
from app.services.normalization import collapse_whitespace, normalize_source_url
from app.services.collection_recovery_service import source_claim_key
from app.services.operation_claim_service import OperationClaimService
from app.services.pagination import page_metadata, page_values
from app.services.source_method_service import SourceMethodService, user_method_label


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
        collection_method: CollectionMethod | str = CollectionMethod.WEB_PAGE,
        method_config: dict | None = None,
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
                SourceMethodService(self.session).configure(
                    source, collection_method, method_config, commit=False
                )
            elif CollectionMethod(collection_method) is not source.collection_method:
                raise SourceBindingConflict(
                    "같은 canonical URL이 다른 수집 방식으로 이미 등록되어 있습니다. 기존 소스를 명시적으로 수정하세요."
                )
            else:
                SourceMethodService(self.session).ensure_default(source)
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
        collection_method: CollectionMethod | str | None = None,
        method_config: dict | None = None,
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
                    SourceMethodService(self.session).configure(
                        source, collection_method or CollectionMethod.WEB_PAGE, method_config, commit=False
                    )
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
            if collection_method is not None:
                acquired = OperationClaimService(self.session).acquire(
                    source_claim_key(source.id), 'METHOD_EDIT'
                )
                if not acquired.acquired:
                    raise SourceBindingConflict("수집 실행 중에는 수집 방식을 변경할 수 없습니다.")
                SourceMethodService(self.session).configure(
                    source, collection_method, method_config, commit=False
                )
                self.session.delete(acquired.claim)
            else:
                SourceMethodService(self.session).ensure_default(source)
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
        page: int = 1,
        page_size: int = 100,
    ) -> dict:
        page, page_size, offset = page_values(page, page_size)
        filters = {
            "search": search, "agency_id": agency_id,
            "org_unit_id": org_unit_id, "status": status,
        }
        total = self.repository.count_bindings(**filters)
        bindings = self.repository.list_bindings(
            **filters, offset=offset, limit=page_size
        )
        latest = self._latest_runs({binding.source_id for binding in bindings})
        projected = [
            self.binding_projection(binding, run=latest.get(binding.source_id))
            for binding in bindings
        ]
        return {
            "items": tuple(projected),
            "details": tuple(projected),
            "pagination": page_metadata(total, page, page_size),
        }

    def _latest_runs(self, source_ids: set[uuid.UUID]) -> dict[uuid.UUID, CrawlRun]:
        if not source_ids:
            return {}
        ranked = (
            select(
                CrawlRun.id.label('run_id'),
                func.row_number().over(
                    partition_by=CrawlRun.source_id,
                    order_by=(CrawlRun.started_at.desc(), CrawlRun.id.desc()),
                ).label('row_number'),
            )
            .where(
                CrawlRun.source_id.in_(source_ids),
                CrawlRun.status.in_((RunStatus.SUCCESS, RunStatus.PARTIAL, RunStatus.FAILED)),
            )
            .subquery()
        )
        runs = self.session.scalars(
            select(CrawlRun).join(ranked, CrawlRun.id == ranked.c.run_id)
            .where(ranked.c.row_number == 1)
        )
        return {run.source_id: run for run in runs}

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

    def binding_projection(self, binding: SourceBinding, run: CrawlRun | None | object = ...) -> dict:
        if run is ...:
            run = self._latest_run(binding.source_id)
        source = binding.source
        configured = (
            source.collection_method is CollectionMethod.WEB_PAGE
            or source.collection_method is CollectionMethod.WEB_CRAWL and source.crawl_config is not None
            or source.collection_method is CollectionMethod.API and source.api_config is not None
        )
        credential_warning = (
            "API 자격증명이 만료되어 수집할 수 없습니다."
            if source.api_config is not None
            and source.api_config.credential_expires_on is not None
            and source.api_config.credential_expires_on < date.today()
            else None
        )
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
            "collection_method": binding.source.collection_method.value,
            "collection_method_label": user_method_label(
                binding.source.collection_method,
                binding.source.api_config.kind if binding.source.api_config else None,
            ),
            "collection_method_long_label": user_method_label(
                binding.source.collection_method,
                binding.source.api_config.kind if binding.source.api_config else None,
                long=True,
            ),
            "method_config": self._method_projection(binding.source),
            "description": binding.description or "-",
            "checked": checked.strftime("%Y-%m-%d %H:%M") if checked else "미확인",
            "found": run.records_observed if run else "-",
            "result": status,
            "status": status,
            "status_code": status_code,
            "tone": tone,
            "error": (run.error_summary if run else None) or binding.exclusion_reason or credential_warning or "-",
            "credential_warning": credential_warning,
            "active": binding.active,
            "collection_supported": configured,
            "collection_enabled": (
                binding.active
                and binding.source.active
                and configured
            ),
            "collection_disabled_reason": (
                "제외된 연결입니다."
                if not binding.active
                else "지원하지 않는 수집 방식입니다."
                if not configured
                else "비활성 소스입니다."
                if not binding.source.active
                else None
            ),
            "excluded_at": binding.excluded_at.isoformat() if binding.excluded_at else None,
        }

    @staticmethod
    def _method_projection(source: Source) -> dict:
        if source.collection_method is CollectionMethod.WEB_PAGE:
            config = source.scrape_config
            return {
                "extract_contacts": config.extract_contacts if config else True,
                "extract_directory": config.extract_directory if config else True,
            }
        if source.collection_method is CollectionMethod.WEB_CRAWL and source.crawl_config:
            config = source.crawl_config
            return {
                "scope": config.scope.value, "allowed_path": config.allowed_path,
                "max_depth": config.max_depth, "max_pages": config.max_pages,
                "request_delay_ms": config.request_delay_ms,
                "extract_contacts": config.extract_contacts,
                "extract_directory": config.extract_directory, "robots_txt": True,
            }
        if source.collection_method is CollectionMethod.API and source.api_config:
            config = source.api_config
            return {
                "kind": config.kind.value, "response_format": config.response_format.value,
                "record_path": config.record_path, "field_mapping": config.field_mapping,
                "static_params": config.static_params, "discovery_only": config.discovery_only,
                "pagination_mode": config.pagination_mode.value,
                "page_parameter": config.page_parameter,
                "page_size_parameter": config.page_size_parameter,
                "page_size": config.page_size, "start_page": config.start_page,
                "max_pages": config.max_pages, "request_delay_ms": config.request_delay_ms,
                "auth_mode": config.auth_mode.value, "credential_ref": config.credential_ref,
                "credential_name": config.credential_name,
                "credential_expires_on": config.credential_expires_on.isoformat() if config.credential_expires_on else None,
                "credential_configured": bool(config.credential_ref),
                "catalog_id": config.catalog_id, "catalog_version": config.catalog_version,
            }
        return {}
