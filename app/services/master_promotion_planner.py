"""Deterministic, read-only planning for staff-directory baseline promotion."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ContactPoint, ContactType, Duty, ExtractionRun, ExtractionStatus, OrgUnit,
    SourceBinding,
)
from app.collectors.html_contact_extractor import is_supported_phone, normalize_email
from app.core.discovery_quality import ContactScope, is_no_data_placeholder, meaningful_directory_values
from app.services.master_normalization import normalize_contact, normalize_text, split_values
from app.services.promotion_plan import PlannedContact, PlannedRow


class PromotionError(ValueError):
    pass


class AgencyContextRequired(PromotionError):
    pass


@dataclass
class PromotionPlan:
    extraction_run: ExtractionRun
    source_id: uuid.UUID
    agency_id: uuid.UUID
    rows: list[PlannedRow]

    def summary(self) -> dict[str, int]:
        create_org = {
            normalize_text(row.org_name)
            for row in self.rows
            if row.safe and row.org_name and row.org_id is None
        }
        match_org = {row.org_id for row in self.rows if row.safe and row.org_id is not None}
        create_duty = {
            (str(row.org_id) if row.org_id else normalize_text(row.org_name), normalize_text(row.duty_title))
            for row in self.rows if row.safe and row.duty_id is None
        }
        match_duty = {row.duty_id for row in self.rows if row.safe and row.duty_id is not None}
        create_contacts = {
            (
                str(row.org_id) if row.org_id else normalize_text(row.org_name),
                normalize_text(row.duty_title), contact.contact_type.value,
                contact.normalized_value,
            )
            for row in self.rows for contact in row.contacts if row.safe and contact.existing_id is None
        }
        match_contacts = {
            contact.existing_id for row in self.rows for contact in row.contacts
            if row.safe and contact.existing_id is not None
        }
        return {
            "directory_records": len(self.rows),
            "org_units_to_create": len(create_org),
            "org_units_matched": len(match_org),
            "duties_to_create": len(create_duty),
            "duties_matched": len(match_duty),
            "contacts_to_create": len(create_contacts),
            "contacts_matched": len(match_contacts),
            "rows_requiring_review": sum(not row.safe for row in self.rows),
            "person_names_detected": sum(bool((row.record.person_name_text or "").strip()) for row in self.rows),
        }


class MasterPromotionPlanner:
    def __init__(self, session: Session) -> None:
        self.session = session

    def agency_choices(self, source_id: uuid.UUID) -> tuple[dict, ...]:
        bindings = list(self.session.scalars(
            select(SourceBinding)
            .options(selectinload(SourceBinding.agency))
            .where(SourceBinding.source_id == source_id, SourceBinding.active.is_(True))
        ))
        choices = {}
        for binding in bindings:
            choices[binding.agency_id] = {
                "id": str(binding.agency_id),
                "name": binding.agency.official_name,
            }
        return tuple(sorted(choices.values(), key=lambda item: item["name"]))

    def resolve_agency(self, source_id: uuid.UUID, agency_id: uuid.UUID | None = None) -> uuid.UUID:
        choices = self.agency_choices(source_id)
        ids = {uuid.UUID(item["id"]) for item in choices}
        if agency_id is not None:
            if agency_id not in ids:
                raise AgencyContextRequired("The selected Agency is not an active Source context.")
            return agency_id
        if len(ids) == 1:
            return next(iter(ids))
        if not ids:
            raise AgencyContextRequired("This Source has no active Agency context.")
        raise AgencyContextRequired("Multiple Agency contexts exist; select the target Agency.")

    def active_bindings(self, source_id: uuid.UUID, agency_id: uuid.UUID) -> tuple[SourceBinding, ...]:
        return tuple(self.session.scalars(
            select(SourceBinding).where(
                SourceBinding.source_id == source_id,
                SourceBinding.agency_id == agency_id,
                SourceBinding.active.is_(True),
            ).order_by(SourceBinding.created_at, SourceBinding.id)
        ))

    def directory_fallback_org_id(self, source_id: uuid.UUID, agency_id: uuid.UUID) -> uuid.UUID | None:
        """Use an OrgUnit only when one active binding proves that exact context."""
        bindings = self.active_bindings(source_id, agency_id)
        return bindings[0].org_unit_id if len(bindings) == 1 else None

    def standalone_org_id(
        self, source_id: uuid.UUID, agency_id: uuid.UUID, scope: ContactScope,
    ) -> uuid.UUID | None:
        if scope is ContactScope.SITE_WIDE:
            return None
        return self.directory_fallback_org_id(source_id, agency_id)

    def plan(self, extraction_run_id: uuid.UUID, agency_id: uuid.UUID | None = None) -> PromotionPlan:
        extraction = self.session.scalar(
            select(ExtractionRun)
            .options(
                selectinload(ExtractionRun.observation),
                selectinload(ExtractionRun.directory_records),
            )
            .where(ExtractionRun.id == extraction_run_id)
        )
        if (
            extraction is None
            or extraction.extractor_name != "staff_directory"
            or extraction.status is not ExtractionStatus.SUCCESS
        ):
            raise PromotionError("A successful staff-directory extraction is required.")
        source_id = extraction.observation.source_id
        resolved_agency = self.resolve_agency(source_id, agency_id)
        orgs = list(self.session.scalars(
            select(OrgUnit).where(OrgUnit.agency_id == resolved_agency, OrgUnit.active.is_(True))
        ))
        duties = list(self.session.scalars(
            select(Duty).where(Duty.agency_id == resolved_agency, Duty.active.is_(True))
        ))
        contacts = list(self.session.scalars(
            select(ContactPoint).where(ContactPoint.agency_id == resolved_agency, ContactPoint.active.is_(True))
        ))
        fallback_org_id = self.directory_fallback_org_id(source_id, resolved_agency)
        rows: list[PlannedRow] = []
        for record in extraction.directory_records:
            if not meaningful_directory_values(
                row_text=record.row_text,
                org_unit_text=record.org_unit_text,
                duty_text=record.duty_text,
                position_text=record.position_text,
                person_name_text=record.person_name_text,
                phone_text=record.phone_text,
                email_text=record.email_text,
                fax_text=record.fax_text,
            ):
                continue
            org_name = (record.org_unit_text or "").strip()
            duty_title = (record.duty_text or "").strip()
            row = PlannedRow(
                record=record, org_name=org_name, duty_title=duty_title,
                org_id=fallback_org_id if not org_name else None, duty_id=None,
            )
            if not duty_title:
                row.blockers.append("DUTY_REQUIRED")
            org_matches = [item for item in orgs if normalize_text(item.name) == normalize_text(org_name)] if org_name else []
            if len(org_matches) > 1:
                row.blockers.append("ORG_UNIT_AMBIGUOUS")
            elif org_matches:
                row.org_id = org_matches[0].id
            if duty_title and (row.org_id is not None or not org_name):
                duty_matches = [
                    item for item in duties
                    if item.org_unit_id == row.org_id and normalize_text(item.title) == normalize_text(duty_title)
                ]
                if len(duty_matches) > 1:
                    row.blockers.append("DUTY_AMBIGUOUS")
                elif duty_matches:
                    row.duty_id = duty_matches[0].id
            for contact_type, raw in (
                (ContactType.PHONE, record.phone_text),
                (ContactType.EMAIL, record.email_text),
                (ContactType.FAX, record.fax_text),
            ):
                for value in split_values(raw):
                    if is_no_data_placeholder(value):
                        row.blockers.append("CONTACT_INVALID")
                        continue
                    if contact_type in (ContactType.PHONE, ContactType.FAX) and not is_supported_phone(value):
                        row.blockers.append("CONTACT_INVALID")
                        continue
                    if contact_type is ContactType.EMAIL:
                        try:
                            normalize_email(value)
                        except ValueError:
                            row.blockers.append("CONTACT_INVALID")
                            continue
                    normalized = normalize_contact(contact_type, value)
                    if not normalized:
                        row.blockers.append("CONTACT_INVALID")
                        continue
                    matches = [
                        item for item in contacts
                        if item.org_unit_id == row.org_id
                        and item.duty_id == row.duty_id
                        and item.person_assignment_id is None
                        and item.contact_type is contact_type
                        and item.normalized_value == normalized
                    ] if row.duty_id else []
                    if len(matches) > 1:
                        row.blockers.append("CONTACT_AMBIGUOUS")
                    else:
                        row.contacts.append(PlannedContact(
                            contact_type=contact_type,
                            value=value,
                            normalized_value=normalized,
                            existing_id=matches[0].id if matches else None,
                        ))
            rows.append(row)
        return PromotionPlan(extraction, source_id, resolved_agency, rows)
