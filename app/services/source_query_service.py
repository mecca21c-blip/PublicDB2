"""Canonical, binding-safe Source facet queries and target resolution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import (
    Agency, CollectionMethod, CrawlRun, OrgUnit, RunStatus, Source,
    SourceBinding, StageStatus,
)
from app.services.pagination import page_metadata, page_values
from app.services.regions import REGIONS
from app.services.source_service import SourceService
from app.services.source_status import source_run_status_condition


FILTER_METHODS = (
    CollectionMethod.WEB_PAGE,
    CollectionMethod.WEB_CRAWL,
    CollectionMethod.API,
)
FILTER_STATUSES = {"active", "unchecked", "success", "empty", "error", "excluded"}
SCHEDULED_FACETS = {"all", "included", "excluded"}
REGION_CODES = {code for code, _label in REGIONS}


class SourceFilterError(ValueError):
    pass


@dataclass(frozen=True)
class SourceFilterSpec:
    search: str = ""
    region_codes: tuple[str, ...] = ()
    agency_id: uuid.UUID | None = None
    org_unit_id: uuid.UUID | None = None
    methods: tuple[CollectionMethod, ...] | None = None
    status: str | None = None
    scheduled: str = "all"

    @classmethod
    def build(
        cls,
        *,
        search: str | None = None,
        region_codes: list[str] | tuple[str, ...] | None = None,
        agency_id: uuid.UUID | None = None,
        org_unit_id: uuid.UUID | None = None,
        methods: list[CollectionMethod | str] | tuple[CollectionMethod | str, ...] | None = None,
        status: str | None = None,
        scheduled: str | None = None,
    ) -> "SourceFilterSpec":
        query = (search or "").strip()
        if len(query) > 100:
            raise SourceFilterError("검색어는 100자 이하여야 합니다.")
        regions = tuple(dict.fromkeys(value.strip() for value in (region_codes or ()) if value.strip()))
        invalid_regions = set(regions) - REGION_CODES
        if invalid_regions:
            raise SourceFilterError("지원하지 않는 지역 코드입니다.")
        parsed_methods = None if methods is None else tuple(dict.fromkeys(CollectionMethod(value) for value in methods))
        if parsed_methods is not None and any(value not in FILTER_METHODS for value in parsed_methods):
            raise SourceFilterError("지원하지 않는 수집 방식 필터입니다.")
        status_value = (status or "").strip() or None
        if status_value not in FILTER_STATUSES | {None}:
            raise SourceFilterError("지원하지 않는 소스 상태 필터입니다.")
        scheduled_value = (scheduled or "all").strip().lower()
        if scheduled_value not in SCHEDULED_FACETS:
            raise SourceFilterError("지원하지 않는 자동 전체 수집 필터입니다.")
        return cls(
            search=query, region_codes=regions, agency_id=agency_id,
            org_unit_id=org_unit_id, methods=parsed_methods,
            status=status_value, scheduled=scheduled_value,
        )

    def public_dict(self) -> dict:
        return {
            "search": self.search,
            "region_codes": list(self.region_codes),
            "agency_id": str(self.agency_id) if self.agency_id else None,
            "org_unit_id": str(self.org_unit_id) if self.org_unit_id else None,
            "methods": [value.value for value in self.methods] if self.methods is not None else None,
            "status": self.status,
            "scheduled": self.scheduled,
        }


class SourceQueryService:
    """Owns every Source facet predicate used by pages, counts, and jobs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def validate_spec(self, spec: SourceFilterSpec) -> tuple[Agency | None, OrgUnit | None]:
        agency = self.session.get(Agency, spec.agency_id) if spec.agency_id else None
        if spec.agency_id and (agency is None or not agency.active):
            raise SourceFilterError("기관을 찾을 수 없습니다.")
        unit = self.session.get(OrgUnit, spec.org_unit_id) if spec.org_unit_id else None
        if spec.org_unit_id and (unit is None or not unit.active):
            raise SourceFilterError("부서를 찾을 수 없습니다.")
        if unit and not spec.agency_id:
            raise SourceFilterError("부서 필터에는 기관을 먼저 선택해야 합니다.")
        if unit and unit.agency_id != spec.agency_id:
            raise SourceFilterError("부서는 선택한 기관에 속해야 합니다.")
        return agency, unit

    def source_ids_statement(self, spec: SourceFilterSpec, *, eligible_only: bool = False):
        self.validate_spec(spec)
        statement = select(Source.id).where(Source.active.is_(True))
        binding_filters = []
        if spec.status == "excluded":
            binding_filters.append(SourceBinding.active.is_(False))
        elif eligible_only or spec.status is not None:
            binding_filters.append(SourceBinding.active.is_(True))
        if spec.region_codes:
            binding_filters.append(Agency.region_code.in_(spec.region_codes))
        if spec.agency_id:
            binding_filters.append(SourceBinding.agency_id == spec.agency_id)
        if spec.org_unit_id:
            binding_filters.append(SourceBinding.org_unit_id == spec.org_unit_id)
        business_sources = (
            select(SourceBinding.source_id)
            .join(Agency, Agency.id == SourceBinding.agency_id)
            .where(*binding_filters)
            .distinct()
        )
        statement = statement.where(Source.id.in_(business_sources))
        if eligible_only and spec.status == "excluded":
            statement = statement.where(False)
        if spec.methods is not None:
            if not spec.methods:
                statement = statement.where(False)
            else:
                statement = statement.where(Source.collection_method.in_(spec.methods))
        if spec.scheduled == "included":
            statement = statement.where(Source.scheduled_refresh_enabled.is_(True))
        elif spec.scheduled == "excluded":
            statement = statement.where(Source.scheduled_refresh_enabled.is_(False))
        if spec.search:
            pattern = f"%{spec.search}%"
            binding_text = (
                select(SourceBinding.id)
                .join(Agency, Agency.id == SourceBinding.agency_id)
                .outerjoin(OrgUnit, OrgUnit.id == SourceBinding.org_unit_id)
                .where(
                    SourceBinding.source_id == Source.id,
                    or_(
                        Agency.official_name.ilike(pattern),
                        OrgUnit.name.ilike(pattern),
                        SourceBinding.description.ilike(pattern),
                    ),
                )
            )
            statement = statement.where(or_(
                Source.url.ilike(pattern), Source.title.ilike(pattern), exists(binding_text),
            ))
        if spec.status == "active":
            pass
        elif spec.status == "unchecked":
            statement = statement.where(source_run_status_condition("unchecked", Source.id, Source))
        elif spec.status in {"success", "empty", "error"}:
            statement = statement.where(source_run_status_condition(spec.status, Source.id, Source))
        return statement.order_by(None)

    def count(self, spec: SourceFilterSpec, *, eligible_only: bool = False) -> int:
        ids = self.source_ids_statement(spec, eligible_only=eligible_only).subquery()
        return self.session.scalar(select(func.count()).select_from(ids)) or 0

    def resolve_sources(self, spec: SourceFilterSpec, *, eligible_only: bool = True) -> list[Source]:
        ids = self.source_ids_statement(spec, eligible_only=eligible_only).subquery()
        return list(self.session.scalars(
            select(Source).join(ids, ids.c.id == Source.id)
            .order_by(Source.normalized_url, Source.id)
        ))

    def method_counts(self, spec: SourceFilterSpec, *, eligible_only: bool = True) -> dict[str, int]:
        ids = self.source_ids_statement(spec, eligible_only=eligible_only).subquery()
        rows = self.session.execute(
            select(Source.collection_method, func.count())
            .join(ids, ids.c.id == Source.id)
            .group_by(Source.collection_method)
        )
        return {method.value: count for method, count in rows}

    def snapshot(self, spec: SourceFilterSpec, *, resolved_count: int) -> dict:
        agency, unit = self.validate_spec(spec)
        value = spec.public_dict()
        value.update({
            "agency_name": agency.official_name if agency else None,
            "org_unit_name": unit.name if unit else None,
            "resolved_count": resolved_count,
        })
        return value

    def list_page(self, spec: SourceFilterSpec, *, page: int = 1, page_size: int = 100) -> dict:
        page, page_size, offset = page_values(page, min(page_size, 100))
        total = self.count(spec)
        ids = self.source_ids_statement(spec).subquery()
        sources = list(self.session.scalars(
            select(Source)
            .join(ids, ids.c.id == Source.id)
            .options(
                selectinload(Source.bindings).joinedload(SourceBinding.agency),
                selectinload(Source.bindings).joinedload(SourceBinding.org_unit),
                joinedload(Source.scrape_config), joinedload(Source.crawl_config),
                joinedload(Source.api_config),
            )
            .order_by(Source.normalized_url, Source.id)
            .offset(offset).limit(page_size)
        ).unique())
        source_service = SourceService(self.session)
        latest = source_service._latest_runs({source.id for source in sources})
        projected = []
        for source in sources:
            binding = self._display_binding(source, spec)
            item = source_service.binding_projection(binding, run=latest.get(source.id))
            item["binding_count"] = len(source.bindings)
            item["agency_summary"] = self._agency_summary(source.bindings)
            projected.append(item)
        eligible_total = self.count(spec, eligible_only=True)
        return {
            "items": tuple(projected), "details": tuple(projected),
            "pagination": page_metadata(total, page, page_size),
            "total": total, "eligible_total": eligible_total,
            "method_counts": self.method_counts(spec),
        }

    @staticmethod
    def _agency_summary(bindings: list[SourceBinding]) -> str:
        names = list(dict.fromkeys(binding.agency.official_name for binding in bindings))
        return names[0] if len(names) == 1 else f"{names[0]} 외 {len(names) - 1}개 기관"

    @staticmethod
    def _display_binding(source: Source, spec: SourceFilterSpec) -> SourceBinding:
        candidates = []
        for binding in source.bindings:
            if spec.status == "excluded" and binding.active:
                continue
            if spec.status != "excluded" and not binding.active:
                continue
            if spec.region_codes and binding.agency.region_code not in spec.region_codes:
                continue
            if spec.agency_id and binding.agency_id != spec.agency_id:
                continue
            if spec.org_unit_id and binding.org_unit_id != spec.org_unit_id:
                continue
            candidates.append(binding)
        values = candidates or source.bindings
        return sorted(values, key=lambda value: (value.agency.official_name, value.org_unit.name if value.org_unit else "", str(value.id)))[0]
