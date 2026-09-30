"""Canonical Source and contextual binding queries."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Agency, CrawlRun, OrgUnit, RunStatus, Source, SourceBinding, StageStatus


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_source_by_normalized_url(self, normalized_url: str) -> Source | None:
        return self.session.scalar(select(Source).where(Source.normalized_url == normalized_url))

    def get_binding(self, binding_id: uuid.UUID) -> SourceBinding | None:
        return self.session.scalar(
            select(SourceBinding)
            .where(SourceBinding.id == binding_id)
            .options(joinedload(SourceBinding.source), joinedload(SourceBinding.agency), joinedload(SourceBinding.org_unit))
        )

    def get_binding_by_scope(self, source_id: uuid.UUID, scope_key: str) -> SourceBinding | None:
        return self.session.scalar(
            select(SourceBinding).where(SourceBinding.source_id == source_id, SourceBinding.scope_key == scope_key)
        )

    def list_bindings(
        self,
        *,
        search: str | None = None,
        agency_id: uuid.UUID | None = None,
        org_unit_id: uuid.UUID | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[SourceBinding]:
        statement = (
            select(SourceBinding)
            .join(SourceBinding.source)
            .join(SourceBinding.agency)
            .outerjoin(SourceBinding.org_unit)
            .options(
                joinedload(SourceBinding.source).joinedload(Source.scrape_config),
                joinedload(SourceBinding.source).joinedload(Source.crawl_config),
                joinedload(SourceBinding.source).joinedload(Source.api_config),
                joinedload(SourceBinding.agency),
                joinedload(SourceBinding.org_unit),
            )
            .order_by(Agency.official_name, Source.normalized_url)
        )
        if search:
            pattern = f"%{search.strip()}%"
            statement = statement.where(
                or_(
                    Agency.official_name.ilike(pattern),
                    OrgUnit.name.ilike(pattern),
                    Source.url.ilike(pattern),
                    SourceBinding.description.ilike(pattern),
                )
            )
        if agency_id:
            statement = statement.where(SourceBinding.agency_id == agency_id)
        if org_unit_id:
            statement = statement.where(SourceBinding.org_unit_id == org_unit_id)
        latest_run_id = (
            select(CrawlRun.id)
            .where(
                CrawlRun.source_id == SourceBinding.source_id,
                CrawlRun.status.in_((RunStatus.SUCCESS, RunStatus.PARTIAL, RunStatus.FAILED)),
            )
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
            .limit(1)
            .correlate(SourceBinding)
            .scalar_subquery()
        )
        if status == "excluded":
            statement = statement.where(SourceBinding.active.is_(False))
        elif status == "active":
            statement = statement.where(SourceBinding.active.is_(True))
        elif status == "unchecked":
            statement = statement.where(SourceBinding.active.is_(True), latest_run_id.is_(None))
        elif status in {"success", "empty", "error"}:
            statement = statement.join(CrawlRun, CrawlRun.id == latest_run_id).where(SourceBinding.active.is_(True))
            if status == "error":
                statement = statement.where(CrawlRun.status.in_((RunStatus.PARTIAL, RunStatus.FAILED)))
            elif status == "empty":
                statement = statement.where(
                    CrawlRun.status == RunStatus.SUCCESS,
                    CrawlRun.records_observed == 0,
                    CrawlRun.connection_status == StageStatus.SUCCESS,
                    CrawlRun.raw_status == StageStatus.SUCCESS,
                    CrawlRun.extraction_status == StageStatus.SUCCESS,
                )
            else:
                statement = statement.where(
                    CrawlRun.status == RunStatus.SUCCESS,
                    ~(
                        (CrawlRun.records_observed == 0)
                        & (CrawlRun.connection_status == StageStatus.SUCCESS)
                        & (CrawlRun.raw_status == StageStatus.SUCCESS)
                        & (CrawlRun.extraction_status == StageStatus.SUCCESS)
                    ),
                )
        return list(self.session.scalars(statement.offset(offset).limit(limit)).unique())

    def count_bindings(self, **filters) -> int:
        statement = self.list_bindings_statement(**filters).order_by(None).subquery()
        return self.session.scalar(select(func.count()).select_from(statement)) or 0

    def list_bindings_statement(
        self, *, search=None, agency_id=None, org_unit_id=None, status=None
    ):
        statement = (
            select(SourceBinding.id)
            .join(SourceBinding.source)
            .join(SourceBinding.agency)
            .outerjoin(SourceBinding.org_unit)
        )
        if search:
            pattern = f"%{search.strip()}%"
            statement = statement.where(or_(
                Agency.official_name.ilike(pattern), OrgUnit.name.ilike(pattern),
                Source.url.ilike(pattern), SourceBinding.description.ilike(pattern),
            ))
        if agency_id:
            statement = statement.where(SourceBinding.agency_id == agency_id)
        if org_unit_id:
            statement = statement.where(SourceBinding.org_unit_id == org_unit_id)
        latest_run_id = (
            select(CrawlRun.id)
            .where(
                CrawlRun.source_id == SourceBinding.source_id,
                CrawlRun.status.in_((RunStatus.SUCCESS, RunStatus.PARTIAL, RunStatus.FAILED)),
            )
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
            .limit(1).correlate(SourceBinding).scalar_subquery()
        )
        if status == 'excluded':
            statement = statement.where(SourceBinding.active.is_(False))
        elif status == 'active':
            statement = statement.where(SourceBinding.active.is_(True))
        elif status == 'unchecked':
            statement = statement.where(SourceBinding.active.is_(True), latest_run_id.is_(None))
        elif status in {'success', 'empty', 'error'}:
            statement = statement.join(CrawlRun, CrawlRun.id == latest_run_id).where(SourceBinding.active.is_(True))
            if status == 'error':
                statement = statement.where(CrawlRun.status.in_((RunStatus.PARTIAL, RunStatus.FAILED)))
            elif status == 'empty':
                statement = statement.where(
                    CrawlRun.status == RunStatus.SUCCESS, CrawlRun.records_observed == 0,
                    CrawlRun.connection_status == StageStatus.SUCCESS,
                    CrawlRun.raw_status == StageStatus.SUCCESS,
                    CrawlRun.extraction_status == StageStatus.SUCCESS,
                )
            else:
                statement = statement.where(
                    CrawlRun.status == RunStatus.SUCCESS,
                    ~(
                        (CrawlRun.records_observed == 0)
                        & (CrawlRun.connection_status == StageStatus.SUCCESS)
                        & (CrawlRun.raw_status == StageStatus.SUCCESS)
                        & (CrawlRun.extraction_status == StageStatus.SUCCESS)
                    ),
                )
        return statement

    def add_source(self, source: Source) -> None:
        self.session.add(source)

    def add_binding(self, binding: SourceBinding) -> None:
        self.session.add(binding)
