"""Bounded searchable lookups for large Agency and OrgUnit datasets."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.models import Agency, OrgUnit
from app.services.regions import REGIONS


MAX_LOOKUP_RESULTS = 30
MAX_LOOKUP_QUERY_LENGTH = 100
REGION_CODES = {code for code, _label in REGIONS}


class LookupError(ValueError):
    pass


class LookupService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _query(value: str | None) -> str:
        query = (value or "").strip()
        if len(query) > MAX_LOOKUP_QUERY_LENGTH:
            raise LookupError("검색어는 100자 이하여야 합니다.")
        return query

    @staticmethod
    def _limit(value: int) -> int:
        return max(1, min(value, MAX_LOOKUP_RESULTS))

    def agencies(self, *, q: str | None, region_code: str | None, limit: int = 20) -> list[dict]:
        query = self._query(q)
        region = (region_code or "").strip() or None
        if region and region not in REGION_CODES:
            raise LookupError("지원하지 않는 지역 코드입니다.")
        # Empty lookup never becomes a disguised full-table dropdown.
        if not query:
            return []
        statement = select(Agency).where(Agency.active.is_(True))
        if region:
            statement = statement.where(Agency.region_code == region)
        statement = statement.where(Agency.official_name.contains(query, autoescape=True))
        agencies = self.session.scalars(
            statement.order_by(Agency.official_name, Agency.id).limit(self._limit(limit))
        )
        return [
            {
                "id": str(agency.id), "name": agency.official_name,
                "region_code": agency.region_code,
                "label": agency.official_name,
            }
            for agency in agencies
        ]

    def org_units(self, *, agency_id: uuid.UUID, q: str | None, limit: int = 20) -> list[dict]:
        agency = self.session.get(Agency, agency_id)
        if agency is None or not agency.active:
            raise LookupError("기관을 찾을 수 없습니다.")
        query = self._query(q)
        parent = aliased(OrgUnit)
        statement = (
            select(OrgUnit, parent.name)
            .outerjoin(parent, parent.id == OrgUnit.parent_org_unit_id)
            .where(OrgUnit.active.is_(True), OrgUnit.agency_id == agency_id)
        )
        if query:
            statement = statement.where(OrgUnit.name.contains(query, autoescape=True))
        rows = self.session.execute(
            statement.order_by(OrgUnit.name, OrgUnit.id).limit(self._limit(limit))
        )
        return [
            {
                "id": str(unit.id), "name": unit.name,
                "agency_id": str(unit.agency_id), "parent_name": parent_name,
                "label": f"{parent_name} > {unit.name}" if parent_name else unit.name,
            }
            for unit, parent_name in rows
        ]
