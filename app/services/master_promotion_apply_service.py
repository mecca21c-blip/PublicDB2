"""Atomic deterministic writer for confirmed organization, duty, and contact data."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.collectors.html_contact_extractor import is_supported_phone, normalize_email
from app.core.discovery_quality import ContactScope, classify_contact_scope
from app.models import (
    ChangeEvent, ChangeEventType, ContactHistory, ContactPoint, ContactType, Duty,
    EntityType, ExtractionRun, ExtractionStatus, Observation, OrgUnit, OrgUnitType,
    ReviewStatus, SourceOccurrence,
)
from app.models.common import utc_now
from app.services.master_normalization import normalize_contact, normalize_text
from app.services.master_promotion_planner import MasterPromotionPlanner, PromotionError
from app.services.operation_claim_service import OperationClaimService
from app.services.semantic_discovery_service import SemanticDiscoveryProjector


class MasterPromotionApplyService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.planner = MasterPromotionPlanner(session)

    def preview(self, extraction_run_id: uuid.UUID, agency_id: uuid.UUID | None = None) -> dict:
        plan = self.planner.plan(extraction_run_id, agency_id)
        return {**plan.summary(), "agency_id": str(plan.agency_id), "extraction_run_id": str(extraction_run_id)}

    def apply(self, extraction_run_id: uuid.UUID, agency_id: uuid.UUID | None = None) -> dict:
        plan = self.planner.plan(extraction_run_id, agency_id)
        return self._execute(
            resource_key=f"baseline:{extraction_run_id}:{plan.agency_id}",
            operation="BASELINE_APPLY",
            observation=plan.extraction_run.observation,
            agency_id=plan.agency_id,
            plans=(plan,),
            include_standalone=False,
            metadata={"extraction_run_id": str(extraction_run_id)},
            event_reason="Approved baseline promotion",
        )

    def sync_observation(
        self, observation_id: uuid.UUID, agency_id: uuid.UUID | None = None,
    ) -> dict:
        """Synchronize deterministic non-person Master values for persisted evidence."""
        observation = self.session.scalar(
            select(Observation)
            .options(selectinload(Observation.crawl_run))
            .where(Observation.id == observation_id)
        )
        if observation is None:
            raise PromotionError("Observation was not found.")
        resolved_agency = self.planner.resolve_agency(observation.source_id, agency_id)
        extraction_ids = tuple(self.session.scalars(
            select(ExtractionRun.id).where(
                ExtractionRun.observation_id == observation.id,
                ExtractionRun.extractor_name == "staff_directory",
                ExtractionRun.status == ExtractionStatus.SUCCESS,
            ).order_by(ExtractionRun.id)
        ))
        plans = tuple(self.planner.plan(value, resolved_agency) for value in extraction_ids)
        return self._execute(
            resource_key=f"auto-master:{observation.id}:{resolved_agency}",
            operation="AUTO_MASTER_SYNC",
            observation=observation,
            agency_id=resolved_agency,
            plans=plans,
            include_standalone=True,
            metadata={"observation_id": str(observation.id)},
            event_reason="Deterministic collection auto-sync",
        )

    def _execute(
        self, *, resource_key: str, operation: str, observation: Observation,
        agency_id: uuid.UUID, plans: tuple, include_standalone: bool,
        metadata: dict[str, str], event_reason: str,
    ) -> dict:
        count_keys = (
            "org_units_created", "duties_created", "contacts_created", "contacts_matched",
            "contacts_changed_skipped", "rows_skipped", "standalone_created",
            "standalone_matched", "standalone_unknown_skipped",
        )
        try:
            claims = OperationClaimService(self.session)
            acquired = claims.acquire(resource_key, operation)
            if not acquired.acquired:
                if acquired.claim.status == "COMPLETED" and acquired.claim.result_payload:
                    result = {**acquired.claim.result_payload, "reused": True}
                    for key in count_keys:
                        result[key] = 0
                    return result
                raise PromotionError("Master synchronization is already in progress.")
            actual = {key: 0 for key in count_keys}
            initial_contacts = tuple(self.session.scalars(select(ContactPoint).where(
                ContactPoint.agency_id == agency_id,
                ContactPoint.active.is_(True),
            )))
            org_cache: dict[str, OrgUnit] = {}
            duty_cache: dict[tuple[uuid.UUID | None, str], Duty] = {}
            for plan in plans:
                self._write_directory_plan(
                    plan, observation, initial_contacts, org_cache, duty_cache,
                    actual, event_reason,
                )
            if include_standalone:
                self._write_standalone_contacts(
                    observation, agency_id, initial_contacts, actual, event_reason,
                )
            result = {**actual, "agency_id": str(agency_id), **metadata, "reused": False}
            claims.complete(acquired.claim, result)
            self.session.commit()
            return result
        except Exception:
            self.session.rollback()
            raise

    def _write_directory_plan(
        self, plan, observation, initial_contacts, org_cache, duty_cache,
        actual, event_reason,
    ) -> None:
        for row in plan.rows:
            if not row.safe:
                actual["rows_skipped"] += 1
                continue
            org = self._resolve_or_create_org(plan, row, org_cache, observation, event_reason, actual)
            org_id = org.id if org is not None else None
            if org is not None:
                self._occurrence(
                    observation, EntityType.ORG_UNIT, org.id, "name", org.name,
                    row.record.source_locator, row.record.row_text,
                )
            duty = self._resolve_or_create_duty(
                plan, row, org_id, duty_cache, observation, event_reason, actual,
            )
            self._occurrence(
                observation, EntityType.DUTY, duty.id, "title", duty.title,
                row.record.source_locator, row.record.row_text,
            )
            for planned in row.contacts:
                matches = self._exact_contacts(
                    plan.agency_id, org_id, duty.id, planned.contact_type,
                    planned.normalized_value,
                )
                if len(matches) > 1:
                    actual["rows_skipped"] += 1
                    continue
                contact = matches[0] if matches else None
                if contact is None and self._has_changed_context(
                    initial_contacts, org_id, duty.id, planned.contact_type,
                    planned.normalized_value,
                ):
                    actual["contacts_changed_skipped"] += 1
                    continue
                if contact is None:
                    contact = ContactPoint(
                        agency_id=plan.agency_id, org_unit_id=org_id, duty_id=duty.id,
                        person_assignment_id=None, contact_type=planned.contact_type,
                        value=planned.value, normalized_value=planned.normalized_value,
                        purpose_text=row.duty_title, verified_at=observation.observed_at,
                    )
                    self.session.add(contact)
                    self.session.flush()
                    event = self._event(
                        EntityType.CONTACT_POINT, contact.id, ChangeEventType.CONTACT_ADDED,
                        None, {"type": planned.contact_type.value, "value": planned.value},
                        observation.observed_at, event_reason,
                    )
                    self.session.add(ContactHistory(
                        contact_id=contact.id, value=contact.value,
                        normalized_value=contact.normalized_value, active=True,
                        valid_from=observation.observed_at, change_event_id=event.id,
                    ))
                    actual["contacts_created"] += 1
                else:
                    self._refresh_confirmation(contact, observation)
                    actual["contacts_matched"] += 1
                self._occurrence(
                    observation, EntityType.CONTACT_POINT, contact.id,
                    planned.contact_type.value.lower(), planned.value,
                    row.record.source_locator, row.record.row_text,
                )

    def _resolve_or_create_org(self, plan, row, cache, observation, reason, actual):
        if row.org_id is not None:
            return self.session.get(OrgUnit, row.org_id)
        if not row.org_name:
            return None
        key = normalize_text(row.org_name)
        org = cache.get(key) or self.session.scalar(select(OrgUnit).where(
            OrgUnit.agency_id == plan.agency_id,
            OrgUnit.normalized_name == key,
            OrgUnit.active.is_(True),
        ).limit(1))
        if org is None:
            org = OrgUnit(
                agency_id=plan.agency_id, name=row.org_name,
                normalized_name=key, unit_type=OrgUnitType.OTHER,
            )
            self.session.add(org)
            self.session.flush()
            actual["org_units_created"] += 1
            self._event(
                EntityType.ORG_UNIT, org.id, ChangeEventType.ENTITY_ADDED,
                None, {"name": org.name}, observation.observed_at, reason,
            )
        cache[key] = org
        return org

    def _resolve_or_create_duty(self, plan, row, org_id, cache, observation, reason, actual):
        key = (org_id, normalize_text(row.duty_title))
        duty = cache.get(key)
        if duty is None and row.duty_id is not None:
            duty = self.session.get(Duty, row.duty_id)
        if duty is None:
            duty = next((candidate for candidate in self.session.scalars(select(Duty).where(
                Duty.agency_id == plan.agency_id,
                Duty.org_unit_id == org_id,
                Duty.active.is_(True),
            )) if normalize_text(candidate.title) == key[1]), None)
        if duty is None:
            duty = Duty(agency_id=plan.agency_id, org_unit_id=org_id, title=row.duty_title)
            self.session.add(duty)
            self.session.flush()
            actual["duties_created"] += 1
            self._event(
                EntityType.DUTY, duty.id, ChangeEventType.ENTITY_ADDED,
                None, {"title": duty.title}, observation.observed_at, reason,
            )
        cache[key] = duty
        return duty

    def _write_standalone_contacts(
        self, observation, agency_id, initial_contacts, actual, event_reason,
    ) -> None:
        candidates = SemanticDiscoveryProjector(self.session).standalone_contacts_for_observation(
            observation.crawl_run_id, observation.id,
        )
        for candidate in candidates:
            scope = classify_contact_scope(candidate.source_locator, candidate.context_text)
            if scope is ContactScope.UNKNOWN:
                actual["standalone_unknown_skipped"] += 1
                continue
            contact_type = ContactType(candidate.candidate_type.value)
            normalized = self._validated_normalized_contact(contact_type, candidate.raw_value)
            if normalized is None:
                actual["rows_skipped"] += 1
                continue
            org_id = self.planner.standalone_org_id(observation.source_id, agency_id, scope)
            matches = self._exact_contacts(agency_id, org_id, None, contact_type, normalized)
            if len(matches) > 1:
                actual["rows_skipped"] += 1
                continue
            contact = matches[0] if matches else None
            if contact is None and self._has_changed_context(
                initial_contacts, org_id, None, contact_type, normalized,
            ):
                actual["contacts_changed_skipped"] += 1
                continue
            if contact is None:
                contact = ContactPoint(
                    agency_id=agency_id, org_unit_id=org_id, duty_id=None,
                    person_assignment_id=None, contact_type=contact_type,
                    value=candidate.raw_value, normalized_value=normalized,
                    purpose_text=None, verified_at=observation.observed_at,
                )
                self.session.add(contact)
                self.session.flush()
                event = self._event(
                    EntityType.CONTACT_POINT, contact.id, ChangeEventType.CONTACT_ADDED,
                    None, {"type": contact_type.value, "value": candidate.raw_value},
                    observation.observed_at, event_reason,
                )
                self.session.add(ContactHistory(
                    contact_id=contact.id, value=contact.value,
                    normalized_value=contact.normalized_value, active=True,
                    valid_from=observation.observed_at, change_event_id=event.id,
                ))
                actual["contacts_created"] += 1
                actual["standalone_created"] += 1
            else:
                self._refresh_confirmation(contact, observation)
                actual["contacts_matched"] += 1
                actual["standalone_matched"] += 1
            self._occurrence(
                observation, EntityType.CONTACT_POINT, contact.id,
                contact_type.value.lower(), candidate.raw_value,
                candidate.source_locator, candidate.context_text,
            )

    def _exact_contacts(self, agency_id, org_id, duty_id, contact_type, normalized):
        return list(self.session.scalars(select(ContactPoint).where(
            ContactPoint.agency_id == agency_id,
            ContactPoint.org_unit_id == org_id,
            ContactPoint.duty_id == duty_id,
            ContactPoint.person_assignment_id.is_(None),
            ContactPoint.contact_type == contact_type,
            ContactPoint.normalized_value == normalized,
            ContactPoint.active.is_(True),
        )))

    @staticmethod
    def _has_changed_context(contacts, org_id, duty_id, contact_type, normalized):
        return any(
            contact.org_unit_id == org_id
            and contact.duty_id == duty_id
            and contact.person_assignment_id is None
            and contact.contact_type is contact_type
            and contact.normalized_value != normalized
            for contact in contacts
        )

    @staticmethod
    def _validated_normalized_contact(contact_type, value):
        if contact_type in (ContactType.PHONE, ContactType.FAX) and not is_supported_phone(value):
            return None
        if contact_type is ContactType.EMAIL:
            try:
                normalize_email(value)
            except ValueError:
                return None
        return normalize_contact(contact_type, value) or None

    @staticmethod
    def _refresh_confirmation(contact, observation):
        if contact.verified_at is None or contact.verified_at < observation.observed_at:
            contact.verified_at = observation.observed_at

    def _event(
        self, entity_type, entity_id, event_type, old_value, new_value,
        detected_at, reason,
    ) -> ChangeEvent:
        event = ChangeEvent(
            entity_type=entity_type, entity_id=entity_id, event_type=event_type,
            old_value=old_value, new_value=new_value, reason=reason,
            review_status=ReviewStatus.APPROVED, detected_at=detected_at,
            resolved_at=utc_now(),
        )
        self.session.add(event)
        self.session.flush()
        return event

    def _occurrence(
        self, observation, entity_type, entity_id, field_name, value,
        source_locator, context_text,
    ) -> None:
        exists = self.session.scalar(select(SourceOccurrence.id).where(
            SourceOccurrence.observation_id == observation.id,
            SourceOccurrence.entity_type == entity_type,
            SourceOccurrence.entity_id == entity_id,
            SourceOccurrence.field_name == field_name,
            SourceOccurrence.source_locator == source_locator,
        ))
        if exists is None:
            self.session.add(SourceOccurrence(
                observation_id=observation.id, entity_type=entity_type, entity_id=entity_id,
                field_name=field_name, observed_value=value, context_text=context_text,
                source_locator=source_locator, observed_at=observation.observed_at,
            ))
