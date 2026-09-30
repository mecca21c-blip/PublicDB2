"""Review decisions and stale-protected confirmed-data mutations."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ChangeEvent, ChangeEventType, ContactHistory, ContactPoint, ContactType,
    DetectedChangeCandidate, Duty, EntityType, OrgUnit, OrgUnitType,
    ReviewStatus, SourceOccurrence,
)
from app.models.common import utc_now
from app.services.master_normalization import normalize_text
from app.services.operation_claim_service import OperationClaimService


class ReviewConflict(ValueError):
    pass


class ReviewService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def approve(self, candidate_id: uuid.UUID, note: str | None = None) -> dict:
        candidate = self._candidate(candidate_id)
        if candidate.review_status is ReviewStatus.APPROVED:
            return {"candidate_id": str(candidate.id), "status": ReviewStatus.APPROVED.value, "reused": True}
        if candidate.review_status not in (ReviewStatus.PENDING_REVIEW, ReviewStatus.DEFERRED):
            raise ReviewConflict("This review candidate is already terminal.")
        if not candidate.actionable:
            raise ReviewConflict(candidate.blocked_reason or "This candidate requires manual context confirmation.")
        if candidate.observation.source_id != candidate.source_id:
            raise ReviewConflict("Source evidence no longer matches the candidate.")
        claims, claim, reused = self._claim(candidate_id, 'REVIEW_APPROVE')
        if reused is not None:
            return reused
        try:
            self.session.refresh(candidate)
            if candidate.review_status is ReviewStatus.APPROVED:
                result = {"candidate_id": str(candidate.id), "status": ReviewStatus.APPROVED.value, "reused": True}
                claims.complete(claim, result)
                self.session.commit()
                return result
            if candidate.review_status not in (ReviewStatus.PENDING_REVIEW, ReviewStatus.DEFERRED):
                raise ReviewConflict("This review candidate is already terminal.")
            entity = self._apply(candidate)
            candidate.review_status = ReviewStatus.APPROVED
            candidate.resolved_at = utc_now()
            candidate.resolution_note = note
            result = {
                "candidate_id": str(candidate.id),
                "status": candidate.review_status.value,
                "entity_id": str(entity.id),
                "reused": False,
            }
            claims.complete(claim, result)
            self.session.commit()
            return result
        except Exception:
            self.session.rollback()
            raise

    def reject(self, candidate_id: uuid.UUID, note: str | None = None) -> dict:
        candidate = self._candidate(candidate_id)
        if candidate.review_status is ReviewStatus.REJECTED:
            return {"candidate_id": str(candidate.id), "status": ReviewStatus.REJECTED.value, "reused": True}
        if candidate.review_status not in (ReviewStatus.PENDING_REVIEW, ReviewStatus.DEFERRED):
            raise ReviewConflict("This review candidate is already terminal.")
        claims, claim, reused = self._claim(candidate_id, 'REVIEW_REJECT')
        if reused is not None:
            return reused
        self.session.refresh(candidate)
        if candidate.review_status not in (ReviewStatus.PENDING_REVIEW, ReviewStatus.DEFERRED):
            self.session.rollback()
            raise ReviewConflict("This review candidate is already terminal.")
        candidate.review_status = ReviewStatus.REJECTED
        candidate.resolved_at = utc_now()
        candidate.resolution_note = note
        result = {"candidate_id": str(candidate.id), "status": candidate.review_status.value, "reused": False}
        claims.complete(claim, result)
        self.session.commit()
        return result

    def defer(self, candidate_id: uuid.UUID, note: str | None = None) -> dict:
        candidate = self._candidate(candidate_id)
        if candidate.review_status is ReviewStatus.DEFERRED:
            return {"candidate_id": str(candidate.id), "status": ReviewStatus.DEFERRED.value, "reused": True}
        if candidate.review_status is not ReviewStatus.PENDING_REVIEW:
            raise ReviewConflict("Only a pending candidate can be deferred.")
        claims, claim, reused = self._claim(candidate_id, 'REVIEW_DEFER')
        if reused is not None:
            return reused
        self.session.refresh(candidate)
        if candidate.review_status is not ReviewStatus.PENDING_REVIEW:
            self.session.rollback()
            raise ReviewConflict("Only a pending candidate can be deferred.")
        candidate.review_status = ReviewStatus.DEFERRED
        candidate.resolved_at = None
        candidate.resolution_note = note
        result = {"candidate_id": str(candidate.id), "status": candidate.review_status.value, "reused": False}
        claims.complete(claim, result)
        self.session.commit()
        return result

    def _claim(self, candidate_id: uuid.UUID, operation: str):
        claims = OperationClaimService(self.session)
        acquired = claims.acquire(f'review:{candidate_id}', operation)
        if acquired.acquired:
            return claims, acquired.claim, None
        existing = acquired.claim
        if existing.status == 'COMPLETED' and existing.operation == operation and existing.result_payload:
            return claims, existing, {**existing.result_payload, 'reused': True}
        if (
            existing.status == 'COMPLETED'
            and existing.operation == 'REVIEW_DEFER'
            and operation in {'REVIEW_APPROVE', 'REVIEW_REJECT'}
        ):
            self.session.delete(existing)
            self.session.commit()
            return self._claim(candidate_id, operation)
        if existing.status == 'COMPLETED':
            raise ReviewConflict(f'Review candidate already resolved by {existing.operation}.')
        raise ReviewConflict('Review candidate is already being resolved.')

    def _candidate(self, candidate_id: uuid.UUID) -> DetectedChangeCandidate:
        candidate = self.session.scalar(
            select(DetectedChangeCandidate)
            .options(selectinload(DetectedChangeCandidate.observation))
            .where(DetectedChangeCandidate.id == candidate_id)
        )
        if candidate is None:
            raise ReviewConflict("Review candidate not found.")
        return candidate

    def _apply(self, candidate: DetectedChangeCandidate):
        if candidate.proposed_event_type is ChangeEventType.ENTITY_ADDED:
            if candidate.entity_type is EntityType.ORG_UNIT:
                return self._add_org(candidate)
            if candidate.entity_type is EntityType.DUTY:
                return self._add_duty(candidate)
        if candidate.proposed_event_type is ChangeEventType.CONTACT_ADDED:
            return self._add_contact(candidate)
        if candidate.proposed_event_type is ChangeEventType.CONTACT_CHANGED:
            return self._replace_contact(candidate)
        if candidate.proposed_event_type in (ChangeEventType.ENTITY_MISSING, ChangeEventType.CONTACT_MISSING):
            return self._deactivate(candidate)
        raise ReviewConflict("This candidate has no safe automatic writer.")

    def _add_org(self, candidate):
        values = candidate.new_value or {}
        normalized = values.get("normalized_name") or normalize_text(values.get("name"))
        matches = list(self.session.scalars(select(OrgUnit).where(
            OrgUnit.agency_id == candidate.agency_id,
            OrgUnit.normalized_name == normalized,
            OrgUnit.active.is_(True),
        )))
        if matches:
            raise ReviewConflict("Re-review required: organization context changed.")
        entity = OrgUnit(
            agency_id=candidate.agency_id, name=values["name"],
            normalized_name=normalized, unit_type=OrgUnitType.OTHER,
        )
        self.session.add(entity)
        self.session.flush()
        self._audit(candidate, entity, None, values)
        return entity

    def _resolve_org(self, candidate, values):
        if values.get("org_unit_id"):
            entity = self.session.get(OrgUnit, uuid.UUID(values["org_unit_id"]))
            if entity is not None and entity.active and entity.agency_id == candidate.agency_id:
                return entity
            raise ReviewConflict("Re-review required: organization context is unavailable.")
        name = values.get("org_unit_name")
        if not name:
            return None
        matches = [
            item for item in self.session.scalars(select(OrgUnit).where(
                OrgUnit.agency_id == candidate.agency_id, OrgUnit.active.is_(True)
            )) if normalize_text(item.name) == normalize_text(name)
        ]
        if len(matches) != 1:
            raise ReviewConflict("Re-review required: organization context is not unique.")
        return matches[0]

    def _add_duty(self, candidate):
        values = candidate.new_value or {}
        org = self._resolve_org(candidate, values)
        if org is None:
            raise ReviewConflict("Re-review required: duty requires an organization unit.")
        matches = [
            item for item in self.session.scalars(select(Duty).where(
                Duty.agency_id == candidate.agency_id,
                Duty.org_unit_id == org.id,
                Duty.active.is_(True),
            )) if normalize_text(item.title) == normalize_text(values.get("title"))
        ]
        if matches:
            raise ReviewConflict("Re-review required: duty context changed.")
        entity = Duty(agency_id=candidate.agency_id, org_unit_id=org.id, title=values["title"])
        self.session.add(entity)
        self.session.flush()
        self._audit(candidate, entity, None, values)
        return entity

    def _resolve_duty(self, candidate, values, org):
        if values.get("duty_id"):
            entity = self.session.get(Duty, uuid.UUID(values["duty_id"]))
            if entity is not None and entity.active and entity.agency_id == candidate.agency_id:
                return entity
            raise ReviewConflict("Re-review required: duty context is unavailable.")
        title = values.get("duty_title")
        if not title:
            return None
        if org is None:
            raise ReviewConflict("Re-review required: duty requires an organization unit.")
        matches = [
            item for item in self.session.scalars(select(Duty).where(
                Duty.agency_id == candidate.agency_id,
                Duty.org_unit_id == org.id,
                Duty.active.is_(True),
            )) if normalize_text(item.title) == normalize_text(title)
        ]
        if len(matches) != 1:
            raise ReviewConflict("Re-review required: duty context is not unique.")
        return matches[0]

    def _add_contact(self, candidate):
        values = candidate.new_value or {}
        org = self._resolve_org(candidate, values)
        duty = self._resolve_duty(candidate, values, org)
        contact_type = ContactType(values["type"])
        normalized = values["normalized_value"]
        duplicate = self.session.scalar(select(ContactPoint).where(
            ContactPoint.agency_id == candidate.agency_id,
            ContactPoint.org_unit_id == (org.id if org else None),
            ContactPoint.duty_id == (duty.id if duty else None),
            ContactPoint.person_assignment_id.is_(None),
            ContactPoint.contact_type == contact_type,
            ContactPoint.normalized_value == normalized,
            ContactPoint.active.is_(True),
        ).limit(1))
        if duplicate is not None:
            raise ReviewConflict("Re-review required: the contact already exists.")
        entity = ContactPoint(
            agency_id=candidate.agency_id,
            org_unit_id=org.id if org else None,
            duty_id=duty.id if duty else None,
            person_assignment_id=None,
            contact_type=contact_type,
            value=values["value"],
            normalized_value=normalized,
            purpose_text=duty.title if duty else None,
            verified_at=candidate.observation.observed_at,
        )
        self.session.add(entity)
        self.session.flush()
        event = self._audit(candidate, entity, None, values)
        self.session.add(ContactHistory(
            contact_id=entity.id, value=entity.value, normalized_value=entity.normalized_value,
            active=True, valid_from=candidate.observation.observed_at, change_event_id=event.id,
        ))
        return entity

    def _replace_contact(self, candidate):
        entity = self.session.get(ContactPoint, candidate.existing_entity_id)
        old = candidate.old_value or {}
        if (
            entity is None or not entity.active or entity.agency_id != candidate.agency_id
            or entity.normalized_value != old.get("normalized_value")
        ):
            raise ReviewConflict("Re-review required: confirmed contact changed after detection.")
        values = candidate.new_value or {}
        active_histories = list(self.session.scalars(select(ContactHistory).where(
            ContactHistory.contact_id == entity.id, ContactHistory.active.is_(True)
        )))
        if not active_histories:
            self.session.add(ContactHistory(
                contact_id=entity.id, value=entity.value, normalized_value=entity.normalized_value,
                active=False, valid_from=entity.created_at,
                valid_to=candidate.observation.observed_at,
            ))
        for history in active_histories:
            history.active = False
            history.valid_to = candidate.observation.observed_at
        entity.value = values["value"]
        entity.normalized_value = values["normalized_value"]
        entity.verified_at = candidate.observation.observed_at
        event = self._audit(candidate, entity, old, values)
        self.session.add(ContactHistory(
            contact_id=entity.id, value=entity.value, normalized_value=entity.normalized_value,
            active=True, valid_from=candidate.observation.observed_at, change_event_id=event.id,
        ))
        return entity

    def _deactivate(self, candidate):
        model = {
            EntityType.ORG_UNIT: OrgUnit,
            EntityType.DUTY: Duty,
            EntityType.CONTACT_POINT: ContactPoint,
        }.get(candidate.entity_type)
        entity = self.session.get(model, candidate.existing_entity_id) if model else None
        if entity is None or not entity.active or entity.agency_id != candidate.agency_id:
            raise ReviewConflict("Re-review required: confirmed entity is no longer active.")
        if isinstance(entity, ContactPoint):
            old = candidate.old_value or {}
            if entity.normalized_value != old.get("normalized_value"):
                raise ReviewConflict("Re-review required: confirmed contact changed after detection.")
            for history in self.session.scalars(select(ContactHistory).where(
                ContactHistory.contact_id == entity.id, ContactHistory.active.is_(True)
            )):
                history.active = False
                history.valid_to = candidate.observation.observed_at
        entity.active = False
        self._audit(candidate, entity, candidate.old_value, None)
        return entity

    def _audit(self, candidate, entity, old_value, new_value):
        event = ChangeEvent(
            entity_type=candidate.entity_type, entity_id=entity.id,
            event_type=candidate.proposed_event_type,
            old_value=old_value, new_value=new_value,
            reason=candidate.reason, review_status=ReviewStatus.APPROVED,
            detected_at=candidate.observation.observed_at, resolved_at=utc_now(),
        )
        self.session.add(event)
        self.session.flush()
        exists = self.session.scalar(select(SourceOccurrence.id).where(
            SourceOccurrence.observation_id == candidate.observation_id,
            SourceOccurrence.entity_type == candidate.entity_type,
            SourceOccurrence.entity_id == entity.id,
            SourceOccurrence.source_locator == candidate.source_locator,
        ))
        if exists is None:
            display = None
            if isinstance(new_value, dict):
                display = new_value.get("value") or new_value.get("title") or new_value.get("name")
            self.session.add(SourceOccurrence(
                observation_id=candidate.observation_id,
                entity_type=candidate.entity_type, entity_id=entity.id,
                field_name="review_approved", observed_value=display,
                context_text=candidate.reason, source_locator=candidate.source_locator,
                observed_at=candidate.observation.observed_at,
            ))
        return event
