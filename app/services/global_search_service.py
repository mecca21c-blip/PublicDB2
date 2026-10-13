"""Bounded, read-only navigation search across current operational data."""

from __future__ import annotations

from urllib.parse import urlencode

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import (
    Agency,
    ContactPoint,
    Duty,
    OrgUnit,
    Person,
    PersonAssignment,
    Source,
    SourceBinding,
)


DEFAULT_CATEGORY_LIMIT = 10


class GlobalSearchService:
    """Own the global search query and its bounded navigation projection."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def search(self, query: str | None, *, limit: int = DEFAULT_CATEGORY_LIMIT) -> dict:
        term = (query or "").strip()
        category_limit = max(1, min(limit, DEFAULT_CATEGORY_LIMIT))
        if not term:
            return self._empty(term, category_limit)

        pattern = f"%{term}%"
        suffix = urlencode({"search": term})
        organizations = self._organizations(pattern, suffix, category_limit)
        sources = self._sources(pattern, suffix, category_limit)
        contacts = self._contacts(pattern, suffix, category_limit)
        return {
            "query": term,
            "limit": category_limit,
            "total": organizations["total"] + sources["total"] + contacts["total"],
            "organizations": organizations,
            "sources": sources,
            "contacts": contacts,
        }

    @staticmethod
    def _empty(term: str, limit: int) -> dict:
        group = {"items": (), "total": 0, "has_more": False}
        return {
            "query": term,
            "limit": limit,
            "total": 0,
            "organizations": dict(group),
            "sources": dict(group),
            "contacts": dict(group),
        }

    def _organizations(self, pattern: str, suffix: str, limit: int) -> dict:
        agency_filter = or_(
            Agency.official_name.ilike(pattern),
            Agency.external_identifier.ilike(pattern),
            Agency.address.ilike(pattern),
        )
        agency_base = select(Agency).where(Agency.active.is_(True), agency_filter)
        agency_total = self.session.scalar(
            select(func.count()).select_from(agency_base.order_by(None).subquery())
        ) or 0
        agencies = list(self.session.scalars(
            agency_base.order_by(Agency.official_name, Agency.id).limit(limit)
        ))

        unit_base = (
            select(OrgUnit)
            .join(Agency, Agency.id == OrgUnit.agency_id)
            .options(joinedload(OrgUnit.agency))
            .where(
                OrgUnit.active.is_(True), Agency.active.is_(True),
                OrgUnit.name.ilike(pattern),
            )
        )
        unit_total = self.session.scalar(
            select(func.count()).select_from(unit_base.order_by(None).subquery())
        ) or 0
        units = list(self.session.scalars(
            unit_base.order_by(Agency.official_name, OrgUnit.name, OrgUnit.id).limit(limit)
        ))

        duty_base = (
            select(Duty)
            .join(Agency, Agency.id == Duty.agency_id)
            .outerjoin(OrgUnit, OrgUnit.id == Duty.org_unit_id)
            .options(joinedload(Duty.agency), joinedload(Duty.org_unit))
            .where(
                Duty.active.is_(True), Agency.active.is_(True),
                Duty.title.ilike(pattern),
            )
        )
        duty_total = self.session.scalar(
            select(func.count()).select_from(duty_base.order_by(None).subquery())
        ) or 0
        duties = list(self.session.scalars(
            duty_base.order_by(Agency.official_name, Duty.title, Duty.id).limit(limit)
        ))

        items = []
        for agency in agencies:
            items.append({
                "kind": "기관", "title": agency.official_name,
                "context": " · ".join(value for value in (
                    agency.external_identifier, agency.address,
                ) if value) or "기관 정보",
                "href": f"/agencies?{suffix}",
            })
        for unit in units:
            items.append({
                "kind": "부서", "title": unit.name,
                "context": unit.agency.official_name,
                "href": f"/contacts?{suffix}",
            })
        for duty in duties:
            items.append({
                "kind": "업무", "title": duty.title,
                "context": " · ".join(value for value in (
                    duty.agency.official_name,
                    duty.org_unit.name if duty.org_unit else None,
                ) if value),
                "href": f"/contacts?{suffix}",
            })
        total = agency_total + unit_total + duty_total
        return {
            "items": tuple(items[:limit]), "total": total,
            "has_more": total > limit,
        }

    def _sources(self, pattern: str, suffix: str, limit: int) -> dict:
        binding_match = (
            select(SourceBinding.id)
            .join(Agency, Agency.id == SourceBinding.agency_id)
            .outerjoin(OrgUnit, OrgUnit.id == SourceBinding.org_unit_id)
            .where(
                SourceBinding.source_id == Source.id,
                SourceBinding.active.is_(True),
                Agency.active.is_(True),
                or_(
                    SourceBinding.description.ilike(pattern),
                    Agency.official_name.ilike(pattern),
                    OrgUnit.name.ilike(pattern),
                ),
            )
        )
        source_base = select(Source).where(
            Source.active.is_(True),
            or_(
                Source.url.ilike(pattern), Source.title.ilike(pattern),
                exists(binding_match),
            ),
        )
        total = self.session.scalar(
            select(func.count()).select_from(source_base.order_by(None).subquery())
        ) or 0
        rows = list(self.session.scalars(
            source_base.options(
                selectinload(Source.bindings).joinedload(SourceBinding.agency),
                selectinload(Source.bindings).joinedload(SourceBinding.org_unit),
            ).order_by(Source.normalized_url, Source.id).limit(limit)
        ).unique())
        items = []
        for source in rows:
            agencies = list(dict.fromkeys(
                binding.agency.official_name for binding in source.bindings
                if binding.active and binding.agency.active
            ))
            context = " · ".join(agencies[:2])
            if len(agencies) > 2:
                context += f" 외 {len(agencies) - 2}개 기관"
            items.append({
                "kind": "수집 소스", "title": source.title or source.url,
                "context": context or "연결 기관 없음", "detail": source.url,
                "href": f"/sources?{suffix}",
            })
        return {"items": tuple(items), "total": total, "has_more": total > limit}

    def _contacts(self, pattern: str, suffix: str, limit: int) -> dict:
        contact_base = (
            select(ContactPoint)
            .join(Agency, Agency.id == ContactPoint.agency_id)
            .outerjoin(OrgUnit, OrgUnit.id == ContactPoint.org_unit_id)
            .outerjoin(Duty, Duty.id == ContactPoint.duty_id)
            .outerjoin(
                PersonAssignment,
                PersonAssignment.id == ContactPoint.person_assignment_id,
            )
            .outerjoin(Person, Person.id == PersonAssignment.person_id)
            .where(
                ContactPoint.active.is_(True), Agency.active.is_(True),
                or_(
                    Agency.official_name.ilike(pattern),
                    OrgUnit.name.ilike(pattern),
                    Duty.title.ilike(pattern),
                    Person.name.ilike(pattern),
                    ContactPoint.value.ilike(pattern),
                ),
            )
        )
        total = self.session.scalar(
            select(func.count()).select_from(contact_base.order_by(None).subquery())
        ) or 0
        rows = list(self.session.scalars(
            contact_base.options(
                joinedload(ContactPoint.agency), joinedload(ContactPoint.org_unit),
                joinedload(ContactPoint.duty),
                joinedload(ContactPoint.person_assignment).joinedload(PersonAssignment.person),
            ).order_by(Agency.official_name, ContactPoint.value, ContactPoint.id).limit(limit)
        ).unique())
        items = []
        for contact in rows:
            person = (
                contact.person_assignment.person.name
                if contact.person_assignment else None
            )
            title = person or (contact.duty.title if contact.duty else contact.value)
            context = " · ".join(value for value in (
                contact.agency.official_name,
                contact.org_unit.name if contact.org_unit else None,
                contact.duty.title if contact.duty else None,
            ) if value)
            items.append({
                "kind": "연락처", "title": title, "context": context,
                "detail": contact.value, "href": f"/contacts?{suffix}",
            })
        return {"items": tuple(items), "total": total, "has_more": total > limit}
