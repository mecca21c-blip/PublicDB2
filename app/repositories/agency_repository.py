"""Agency hierarchy persistence queries."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Agency, OrgUnit


class AgencyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _filtered(self, *, search: str | None = None, agency_type: str | None = None):
        statement = select(Agency).where(Agency.active.is_(True)).order_by(Agency.official_name)
        if agency_type:
            statement = statement.where(Agency.agency_type == agency_type)
        if search:
            pattern = f"%{search.strip()}%"
            unit_agencies = select(OrgUnit.agency_id).where(OrgUnit.name.ilike(pattern), OrgUnit.active.is_(True))
            statement = statement.where(or_(Agency.official_name.ilike(pattern), Agency.id.in_(unit_agencies)))
        return statement

    def list(
        self, *, search: str | None = None, agency_type: str | None = None,
        offset: int = 0, limit: int = 100,
    ) -> list[Agency]:
        statement = self._filtered(search=search, agency_type=agency_type).offset(offset).limit(limit)
        return list(self.session.scalars(statement))

    def count(self, *, search: str | None = None, agency_type: str | None = None) -> int:
        statement = self._filtered(search=search, agency_type=agency_type).order_by(None).subquery()
        return self.session.scalar(select(func.count()).select_from(statement)) or 0

    def get(self, agency_id: uuid.UUID) -> Agency | None:
        return self.session.scalar(
            select(Agency)
            .where(Agency.id == agency_id, Agency.active.is_(True))
            .options(selectinload(Agency.org_units))
        )

    def get_by_normalized_name(self, normalized_name: str) -> list[Agency]:
        return list(self.session.scalars(select(Agency).where(Agency.normalized_name == normalized_name, Agency.active.is_(True))))

    def add(self, agency: Agency) -> None:
        self.session.add(agency)
