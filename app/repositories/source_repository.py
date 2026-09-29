"""Canonical Source and contextual binding queries."""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Agency, OrgUnit, Source, SourceBinding


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
    ) -> list[SourceBinding]:
        statement = (
            select(SourceBinding)
            .join(SourceBinding.source)
            .join(SourceBinding.agency)
            .outerjoin(SourceBinding.org_unit)
            .options(joinedload(SourceBinding.source), joinedload(SourceBinding.agency), joinedload(SourceBinding.org_unit))
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
        if status == "excluded":
            statement = statement.where(SourceBinding.active.is_(False))
        elif status == "active":
            statement = statement.where(SourceBinding.active.is_(True))
        return list(self.session.scalars(statement).unique())

    def add_source(self, source: Source) -> None:
        self.session.add(source)

    def add_binding(self, binding: SourceBinding) -> None:
        self.session.add(binding)
