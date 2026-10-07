"""Confirmed ContactPoint list/detail projection."""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Agency, ContactHistory, ContactPoint, ContactType, Duty, EntityType, Observation,
    OrgUnit, Person, PersonAssignment, Source, SourceOccurrence,
    CrawlRun, CollectionMethod,
)
from app.services.source_method_service import user_method_label
from app.services.pagination import page_metadata, page_values
from app.core.time_presentation import format_kst_datetime


class ContactService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_page(
        self, *, search: str | None = None, agency_id: uuid.UUID | None = None,
        org_unit_id: uuid.UUID | None = None, contact_type: ContactType | None = None,
        page: int = 1, page_size: int = 100,
    ) -> dict:
        page, page_size, offset = page_values(page, page_size)
        group_columns = (
            ContactPoint.agency_id, ContactPoint.org_unit_id,
            ContactPoint.duty_id, ContactPoint.person_assignment_id,
        )
        groups_statement = (
            select(*group_columns)
            .join(Agency, Agency.id == ContactPoint.agency_id)
            .outerjoin(OrgUnit, OrgUnit.id == ContactPoint.org_unit_id)
            .outerjoin(Duty, Duty.id == ContactPoint.duty_id)
            .outerjoin(PersonAssignment, PersonAssignment.id == ContactPoint.person_assignment_id)
            .outerjoin(Person, Person.id == PersonAssignment.person_id)
            .where(ContactPoint.active.is_(True))
        )
        if agency_id:
            groups_statement = groups_statement.where(ContactPoint.agency_id == agency_id)
        if org_unit_id:
            groups_statement = groups_statement.where(ContactPoint.org_unit_id == org_unit_id)
        if contact_type:
            groups_statement = groups_statement.where(ContactPoint.contact_type == contact_type)
        term = (search or "").strip()
        if term:
            pattern = f"%{term}%"
            groups_statement = groups_statement.where(or_(
                Agency.official_name.ilike(pattern), OrgUnit.name.ilike(pattern),
                Duty.title.ilike(pattern), Person.name.ilike(pattern),
                ContactPoint.value.ilike(pattern),
            ))
        groups_statement = groups_statement.distinct().order_by(*group_columns)
        total = self.session.scalar(
            select(func.count()).select_from(groups_statement.order_by(None).subquery())
        ) or 0
        page_keys = list(self.session.execute(groups_statement.offset(offset).limit(page_size)))
        key_predicates = [
            and_(
                ContactPoint.agency_id == key.agency_id,
                ContactPoint.org_unit_id.is_(None) if key.org_unit_id is None else ContactPoint.org_unit_id == key.org_unit_id,
                ContactPoint.duty_id.is_(None) if key.duty_id is None else ContactPoint.duty_id == key.duty_id,
                ContactPoint.person_assignment_id.is_(None) if key.person_assignment_id is None else ContactPoint.person_assignment_id == key.person_assignment_id,
            )
            for key in page_keys
        ]
        contacts = [] if not key_predicates else list(self.session.scalars(
            select(ContactPoint)
            .options(
                selectinload(ContactPoint.agency),
                selectinload(ContactPoint.org_unit),
                selectinload(ContactPoint.duty),
                selectinload(ContactPoint.person_assignment).selectinload(PersonAssignment.person),
            )
            .where(ContactPoint.active.is_(True), or_(*key_predicates))
            .order_by(ContactPoint.created_at, ContactPoint.id)
        ))
        if contact_type:
            contacts = [item for item in contacts if item.contact_type is contact_type]
        if term:
            folded = term.casefold()
            contacts = [item for item in contacts if folded in " ".join((
                item.agency.official_name,
                item.org_unit.name if item.org_unit else "",
                item.duty.title if item.duty else "",
                item.person_assignment.person.name if item.person_assignment else "",
                item.value,
            )).casefold()]
        groups = defaultdict(list)
        for item in contacts:
            groups[(item.agency_id, item.org_unit_id, item.duty_id, item.person_assignment_id)].append(item)
        all_ids = [item.id for values in groups.values() for item in values]
        sources = self._sources(all_ids)
        histories = self._histories(all_ids)
        items, details = [], []
        for key, values in groups.items():
            first = values[0]
            phones = [item.value for item in values if item.contact_type is ContactType.PHONE]
            emails = [item.value for item in values if item.contact_type is ContactType.EMAIL]
            faxes = [item.value for item in values if item.contact_type is ContactType.FAX]
            group_id = "|".join(str(value or "-") for value in key)
            person = first.person_assignment.person.name if first.person_assignment else "-"
            row = {
                "id": group_id, "agency": first.agency.official_name,
                "department": first.org_unit.name if first.org_unit else "-",
                "task": first.duty.title if first.duty else "-", "person": person,
                "phone": ", ".join(phones) or "-", "email": ", ".join(emails) or "-",
            }
            provenance = [entry for item in values for entry in sources.get(item.id, ())]
            source_urls = sorted({entry["official_url"] for entry in provenance})
            history_rows = [history for item in values for history in histories.get(item.id, ())]
            history_rows.sort(key=lambda item: item.valid_from, reverse=True)
            items.append(row)
            details.append({
                **row, "fax": ", ".join(faxes) or "-",
                "source": ", ".join(source_urls) or "-",
                "provenance": tuple(provenance),
                "verified": format_kst_datetime(max(
                    (item.verified_at for item in values if item.verified_at), default=None
                )),
                "history": tuple(
                    f"{format_kst_datetime(item.valid_from)} / {item.value} / {'active' if item.active else 'closed'}"
                    for item in history_rows[:10]
                ) or ("-",),
            })
        items.sort(key=lambda item: (item["agency"], item["department"], item["task"], item["id"]))
        detail_map = {item["id"]: item for item in details}
        return {
            "items": tuple(items),
            "details": tuple(detail_map[item["id"]] for item in items),
            "pagination": page_metadata(total, page, page_size),
        }

    def _sources(self, contact_ids):
        result = defaultdict(list)
        if not contact_ids:
            return result
        rows = self.session.execute(
            select(
                SourceOccurrence.entity_id, Source.url, Observation.final_url,
                Observation.page_url, Observation.observed_at,
                CrawlRun.collection_method_snapshot, CrawlRun.collection_kind_snapshot,
            )
            .join(Observation, Observation.id == SourceOccurrence.observation_id)
            .join(CrawlRun, CrawlRun.id == Observation.crawl_run_id)
            .join(Source, Source.id == Observation.source_id)
            .where(
                SourceOccurrence.entity_type == EntityType.CONTACT_POINT,
                SourceOccurrence.entity_id.in_(contact_ids),
            )
        )
        seen = defaultdict(set)
        for entity_id, official_url, final_url, page_url, observed_at, method, kind in rows:
            actual_url = final_url or page_url
            key = (official_url, actual_url, method.value if method else "WEB_PAGE", kind, observed_at)
            if key in seen[entity_id]:
                continue
            seen[entity_id].add(key)
            result[entity_id].append({
                "official_url": official_url,
                "actual_url": actual_url,
                "method": user_method_label(method or CollectionMethod.WEB_PAGE, kind),
                "verified": format_kst_datetime(observed_at),
                "legacy_snapshot": method is None,
            })
        return result

    def _histories(self, contact_ids):
        result = defaultdict(list)
        if contact_ids:
            for item in self.session.scalars(
                select(ContactHistory).where(ContactHistory.contact_id.in_(contact_ids))
            ):
                result[item.contact_id].append(item)
        return result
