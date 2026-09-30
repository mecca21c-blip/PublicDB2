"""Atomic baseline writer for confirmed organization, duty, and contact data."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChangeEvent, ChangeEventType, ContactHistory, ContactPoint, Duty, EntityType,
    OrgUnit, OrgUnitType, ReviewStatus, SourceOccurrence,
)
from app.models.common import utc_now
from app.services.master_normalization import normalize_text
from app.services.master_promotion_planner import MasterPromotionPlanner, PromotionError
from app.services.operation_claim_service import OperationClaimService


class MasterPromotionApplyService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.planner = MasterPromotionPlanner(session)

    def preview(self, extraction_run_id: uuid.UUID, agency_id: uuid.UUID | None = None) -> dict:
        plan = self.planner.plan(extraction_run_id, agency_id)
        return {**plan.summary(), "agency_id": str(plan.agency_id), "extraction_run_id": str(extraction_run_id)}

    def apply(self, extraction_run_id: uuid.UUID, agency_id: uuid.UUID | None = None) -> dict:
        try:
            plan = self.planner.plan(extraction_run_id, agency_id)
            claims = OperationClaimService(self.session)
            acquired = claims.acquire(
                f'baseline:{extraction_run_id}:{plan.agency_id}', 'BASELINE_APPLY'
            )
            if not acquired.acquired:
                if acquired.claim.status == 'COMPLETED' and acquired.claim.result_payload:
                    result = {**acquired.claim.result_payload, 'reused': True}
                    for key in (
                        'org_units_created', 'duties_created', 'contacts_created',
                        'contacts_matched', 'rows_skipped',
                    ):
                        result[key] = 0
                    return result
                raise PromotionError('Baseline apply is already in progress.')
            observation = plan.extraction_run.observation
            actual = {"org_units_created": 0, "duties_created": 0, "contacts_created": 0, "contacts_matched": 0, "rows_skipped": 0}
            org_cache: dict[str, OrgUnit] = {}
            duty_cache: dict[tuple[uuid.UUID, str], Duty] = {}
            for row in plan.rows:
                if not row.safe:
                    actual["rows_skipped"] += 1
                    continue
                org_key = normalize_text(row.org_name)
                org = org_cache.get(org_key)
                if org is None:
                    org = self.session.get(OrgUnit, row.org_id) if row.org_id else self.session.scalar(
                        select(OrgUnit).where(
                            OrgUnit.agency_id == plan.agency_id,
                            OrgUnit.normalized_name == org_key,
                            OrgUnit.active.is_(True),
                        ).limit(1)
                    )
                if org is None:
                    org = OrgUnit(
                        agency_id=plan.agency_id, name=row.org_name,
                        normalized_name=org_key, unit_type=OrgUnitType.OTHER,
                    )
                    self.session.add(org)
                    self.session.flush()
                    actual["org_units_created"] += 1
                    self._event(EntityType.ORG_UNIT, org.id, ChangeEventType.ENTITY_ADDED, None, {"name": org.name}, observation.observed_at)
                org_cache[org_key] = org
                self._occurrence(observation.id, EntityType.ORG_UNIT, org.id, "name", row.org_name, row)

                duty_key = (org.id, normalize_text(row.duty_title))
                duty = duty_cache.get(duty_key)
                if duty is None:
                    duty = self.session.get(Duty, row.duty_id) if row.duty_id else next(iter(self.session.scalars(
                        select(Duty).where(
                            Duty.agency_id == plan.agency_id,
                            Duty.org_unit_id == org.id,
                            Duty.active.is_(True),
                        )
                    )), None)
                    if duty is not None and normalize_text(duty.title) != duty_key[1]:
                        duty = next((candidate for candidate in self.session.scalars(
                            select(Duty).where(
                                Duty.agency_id == plan.agency_id,
                                Duty.org_unit_id == org.id,
                                Duty.active.is_(True),
                            )
                        ) if normalize_text(candidate.title) == duty_key[1]), None)
                if duty is None:
                    duty = Duty(agency_id=plan.agency_id, org_unit_id=org.id, title=row.duty_title)
                    self.session.add(duty)
                    self.session.flush()
                    actual["duties_created"] += 1
                    self._event(EntityType.DUTY, duty.id, ChangeEventType.ENTITY_ADDED, None, {"title": duty.title}, observation.observed_at)
                duty_cache[duty_key] = duty
                self._occurrence(observation.id, EntityType.DUTY, duty.id, "title", row.duty_title, row)

                for planned in row.contacts:
                    contact = self.session.get(ContactPoint, planned.existing_id) if planned.existing_id else self.session.scalar(
                        select(ContactPoint).where(
                            ContactPoint.agency_id == plan.agency_id,
                            ContactPoint.org_unit_id == org.id,
                            ContactPoint.duty_id == duty.id,
                            ContactPoint.person_assignment_id.is_(None),
                            ContactPoint.contact_type == planned.contact_type,
                            ContactPoint.normalized_value == planned.normalized_value,
                            ContactPoint.active.is_(True),
                        ).limit(1)
                    )
                    if contact is None:
                        contact = ContactPoint(
                            agency_id=plan.agency_id,
                            org_unit_id=org.id,
                            duty_id=duty.id,
                            person_assignment_id=None,
                            contact_type=planned.contact_type,
                            value=planned.value,
                            normalized_value=planned.normalized_value,
                            purpose_text=row.duty_title,
                            verified_at=observation.observed_at,
                        )
                        self.session.add(contact)
                        self.session.flush()
                        event = self._event(
                            EntityType.CONTACT_POINT, contact.id, ChangeEventType.CONTACT_ADDED,
                            None, {"type": planned.contact_type.value, "value": planned.value},
                            observation.observed_at,
                        )
                        self.session.add(ContactHistory(
                            contact_id=contact.id, value=contact.value,
                            normalized_value=contact.normalized_value, active=True,
                            valid_from=observation.observed_at, change_event_id=event.id,
                        ))
                        actual["contacts_created"] += 1
                    else:
                        if contact.verified_at is None or contact.verified_at < observation.observed_at:
                            contact.verified_at = observation.observed_at
                        actual["contacts_matched"] += 1
                    self._occurrence(
                        observation.id, EntityType.CONTACT_POINT, contact.id,
                        planned.contact_type.value.lower(), planned.value, row,
                    )
            result = {
                **actual,
                "agency_id": str(plan.agency_id),
                "extraction_run_id": str(extraction_run_id),
                "reused": False,
            }
            claims.complete(acquired.claim, result)
            self.session.commit()
            return result
        except Exception:
            self.session.rollback()
            raise

    def _event(self, entity_type, entity_id, event_type, old_value, new_value, detected_at) -> ChangeEvent:
        event = ChangeEvent(
            entity_type=entity_type, entity_id=entity_id, event_type=event_type,
            old_value=old_value, new_value=new_value, reason="Approved baseline promotion",
            review_status=ReviewStatus.APPROVED, detected_at=detected_at,
            resolved_at=utc_now(),
        )
        self.session.add(event)
        self.session.flush()
        return event

    def _occurrence(self, observation_id, entity_type, entity_id, field_name, value, row) -> None:
        exists = self.session.scalar(select(SourceOccurrence.id).where(
            SourceOccurrence.observation_id == observation_id,
            SourceOccurrence.entity_type == entity_type,
            SourceOccurrence.entity_id == entity_id,
            SourceOccurrence.field_name == field_name,
            SourceOccurrence.source_locator == row.record.source_locator,
        ))
        if exists is None:
            self.session.add(SourceOccurrence(
                observation_id=observation_id, entity_type=entity_type, entity_id=entity_id,
                field_name=field_name, observed_value=value, context_text=row.record.row_text,
                source_locator=row.record.source_locator,
                observed_at=row.record.observation.observed_at,
            ))
