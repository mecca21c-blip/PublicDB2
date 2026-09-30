"""Confirmed ContactPoint list/detail projection."""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ContactHistory, ContactPoint, ContactType, EntityType, Observation,
    PersonAssignment, Source, SourceOccurrence,
)


class ContactService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_page(
        self, *, search: str | None = None, agency_id: uuid.UUID | None = None,
        org_unit_id: uuid.UUID | None = None, contact_type: ContactType | None = None,
    ) -> dict:
        contacts = list(self.session.scalars(
            select(ContactPoint)
            .options(
                selectinload(ContactPoint.agency),
                selectinload(ContactPoint.org_unit),
                selectinload(ContactPoint.duty),
                selectinload(ContactPoint.person_assignment).selectinload(PersonAssignment.person),
            )
            .where(ContactPoint.active.is_(True))
            .order_by(ContactPoint.created_at, ContactPoint.id)
        ))
        if agency_id:
            contacts = [item for item in contacts if item.agency_id == agency_id]
        if org_unit_id:
            contacts = [item for item in contacts if item.org_unit_id == org_unit_id]
        if contact_type:
            contacts = [item for item in contacts if item.contact_type is contact_type]
        term = (search or "").strip().casefold()
        if term:
            contacts = [item for item in contacts if term in " ".join((
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
            source_urls = sorted({url for item in values for url in sources.get(item.id, ())})
            history_rows = [history for item in values for history in histories.get(item.id, ())]
            history_rows.sort(key=lambda item: item.valid_from, reverse=True)
            items.append(row)
            details.append({
                **row, "fax": ", ".join(faxes) or "-",
                "source": ", ".join(source_urls) or "-",
                "verified": max(
                    (item.verified_at for item in values if item.verified_at), default=None
                ).strftime("%Y-%m-%d %H:%M") if any(item.verified_at for item in values) else "-",
                "history": tuple(
                    f"{item.valid_from:%Y-%m-%d %H:%M} / {item.value} / {'active' if item.active else 'closed'}"
                    for item in history_rows[:10]
                ) or ("-",),
            })
        items.sort(key=lambda item: (item["agency"], item["department"], item["task"], item["id"]))
        detail_map = {item["id"]: item for item in details}
        return {"items": tuple(items), "details": tuple(detail_map[item["id"]] for item in items)}

    def _sources(self, contact_ids):
        result = defaultdict(set)
        if not contact_ids:
            return result
        rows = self.session.execute(
            select(SourceOccurrence.entity_id, Source.url)
            .join(Observation, Observation.id == SourceOccurrence.observation_id)
            .join(Source, Source.id == Observation.source_id)
            .where(
                SourceOccurrence.entity_type == EntityType.CONTACT_POINT,
                SourceOccurrence.entity_id.in_(contact_ids),
            )
        )
        for entity_id, url in rows:
            result[entity_id].add(url)
        return result

    def _histories(self, contact_ids):
        result = defaultdict(list)
        if contact_ids:
            for item in self.session.scalars(
                select(ContactHistory).where(ContactHistory.contact_id.in_(contact_ids))
            ):
                result[item.contact_id].append(item)
        return result
