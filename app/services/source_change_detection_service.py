"""Persisted, Source-scoped change detection without confirmed-data mutation."""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ChangeDetection, ChangeEventType, ContactPoint, ContactType,
    DetectedChangeCandidate, Duty, EntityType, ExtractionRun, ExtractionStatus,
    Observation, OrgUnit, ReviewStatus, RunStatus, Source, SourceBinding, SourceCoverageMode,
    SourceOccurrence,
)
from app.models.common import utc_now
from app.core.discovery_quality import classify_contact_scope
from app.services.master_normalization import normalize_contact, normalize_text
from app.services.master_promotion_planner import MasterPromotionPlanner, PromotionError
from app.services.semantic_discovery_service import SemanticDiscoveryProjector


class DetectionError(ValueError):
    pass


def _key(*parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SourceChangeDetectionService:
    NAME = "source_change"
    VERSION = "1.1"

    def __init__(self, session: Session) -> None:
        self.session = session
        self.planner = MasterPromotionPlanner(session)

    def baseline_exists(self, source_id: uuid.UUID, agency_id: uuid.UUID) -> bool:
        occurrences = list(self.session.scalars(
            select(SourceOccurrence)
            .join(SourceOccurrence.observation)
            .where(SourceOccurrence.observation.has(source_id=source_id))
        ))
        for occurrence in occurrences:
            model = {
                EntityType.ORG_UNIT: OrgUnit,
                EntityType.DUTY: Duty,
                EntityType.CONTACT_POINT: ContactPoint,
            }.get(occurrence.entity_type)
            entity = self.session.get(model, occurrence.entity_id) if model else None
            if entity is not None and entity.agency_id == agency_id:
                return True
        return False

    def generate(self, extraction_run_id: uuid.UUID, agency_id: uuid.UUID | None = None) -> dict:
        extraction = self.session.scalar(
            select(ExtractionRun)
            .options(selectinload(ExtractionRun.observation))
            .where(ExtractionRun.id == extraction_run_id)
        )
        if extraction is None or extraction.status is not ExtractionStatus.SUCCESS:
            raise DetectionError("A successful extraction is required.")
        observation = extraction.observation
        resolved_agency = self.planner.resolve_agency(observation.source_id, agency_id)
        if not self.baseline_exists(observation.source_id, resolved_agency):
            raise DetectionError("No confirmed baseline exists for this Source and Agency.")
        existing = self.session.scalar(
            select(ChangeDetection)
            .options(selectinload(ChangeDetection.candidates))
            .where(
                ChangeDetection.extraction_run_id == extraction.id,
                ChangeDetection.agency_id == resolved_agency,
                ChangeDetection.detector_name == self.NAME,
                ChangeDetection.detector_version == self.VERSION,
                ChangeDetection.status == RunStatus.SUCCESS,
            )
        )
        if existing is not None:
            return {"detection_id": str(existing.id), "candidates": len(existing.candidates), "reused": True}

        source = self.session.get(Source, observation.source_id)
        detection = ChangeDetection(
            source_id=observation.source_id,
            observation_id=observation.id,
            extraction_run_id=extraction.id,
            agency_id=resolved_agency,
            detector_name=self.NAME,
            detector_version=self.VERSION,
            coverage_mode=source.coverage_mode,
            status=RunStatus.RUNNING,
            started_at=utc_now(),
        )
        self.session.add(detection)
        self.session.flush()
        try:
            specs: list[dict] = []
            if extraction.extractor_name == "staff_directory":
                plan = self.planner.plan(extraction.id, resolved_agency)
                prior = self._prior_entities(observation.source_id, resolved_agency)
                current_orgs, current_duties, current_contacts = set(), set(), set()
                complete = True
                for row in plan.rows:
                    org_norm, duty_norm = normalize_text(row.org_name), normalize_text(row.duty_title)
                    if row.blockers:
                        complete = False
                        specs.append(self._spec(
                            EntityType.ORG_UNIT, ChangeEventType.OTHER, row.record.id, None,
                            None, {"org_unit": row.org_name, "duty": row.duty_title},
                            "PLANNER_REVIEW_REQUIRED: " + ", ".join(row.blockers),
                            row.record.source_locator, False, "Manual context confirmation is required.",
                        ))
                        continue
                    if org_norm:
                        current_orgs.add(org_norm)
                    current_duties.add((org_norm, duty_norm))
                    if row.org_id is None and row.org_name:
                        specs.append(self._spec(
                            EntityType.ORG_UNIT, ChangeEventType.ENTITY_ADDED, row.record.id, None,
                            None, {"name": row.org_name, "normalized_name": org_norm},
                            "New exact organization unit", row.record.source_locator,
                        ))
                    if row.duty_id is None:
                        specs.append(self._spec(
                            EntityType.DUTY, ChangeEventType.ENTITY_ADDED, row.record.id, None,
                            None, {"title": row.duty_title, "org_unit_name": row.org_name},
                            "New exact duty", row.record.source_locator,
                        ))
                    for contact in row.contacts:
                        context_key = (org_norm, duty_norm, contact.contact_type.value)
                        current_contacts.add((*context_key, contact.normalized_value))
                        if contact.existing_id:
                            continue
                        prior_contacts = [
                            entity for entity in prior[EntityType.CONTACT_POINT]
                            if entity.active
                            and entity.contact_type is contact.contact_type
                            and normalize_text(entity.org_unit.name if entity.org_unit else "") == org_norm
                            and normalize_text(entity.duty.title if entity.duty else "") == duty_norm
                        ]
                        if len(prior_contacts) == 1:
                            old = prior_contacts[0]
                            specs.append(self._spec(
                                EntityType.CONTACT_POINT, ChangeEventType.CONTACT_CHANGED, row.record.id, None,
                                old.id,
                                {"type": contact.contact_type.value, "value": contact.value,
                                 "normalized_value": contact.normalized_value,
                                 "org_unit_name": row.org_name, "duty_title": row.duty_title},
                                "Safe contextual contact replacement", row.record.source_locator,
                                old_value={"type": old.contact_type.value, "value": old.value,
                                           "normalized_value": old.normalized_value},
                            ))
                        elif len(prior_contacts) > 1:
                            specs.append(self._spec(
                                EntityType.CONTACT_POINT, ChangeEventType.OTHER, row.record.id, None,
                                None, {"type": contact.contact_type.value, "value": contact.value,
                                       "org_unit_name": row.org_name, "duty_title": row.duty_title},
                                "AMBIGUOUS_CONTACT_REPLACEMENT", row.record.source_locator,
                                False, "More than one prior contextual contact matches.",
                            ))
                        else:
                            specs.append(self._spec(
                                EntityType.CONTACT_POINT, ChangeEventType.CONTACT_ADDED, row.record.id, None,
                                None, {"type": contact.contact_type.value, "value": contact.value,
                                       "normalized_value": contact.normalized_value,
                                       "org_unit_name": row.org_name, "duty_title": row.duty_title},
                                "New contextual contact", row.record.source_locator,
                            ))
                if source.coverage_mode is SourceCoverageMode.COMPLETE_SNAPSHOT and complete:
                    specs.extend(self._missing_specs(prior, current_orgs, current_duties, current_contacts))
                specs.extend(self._generic_specs(observation.id, observation.source_id, resolved_agency))
            else:
                specs.extend(self._generic_specs(observation.id, observation.source_id, resolved_agency))
            for spec in specs:
                candidate = DetectedChangeCandidate(
                    detection_run_id=detection.id,
                    source_id=observation.source_id,
                    observation_id=observation.id,
                    agency_id=resolved_agency,
                    candidate_key=_key(spec["entity_type"].value, spec["event_type"].value, spec.get("existing_id"), spec.get("directory_id"), spec.get("contact_id"), spec.get("new_value")),
                    entity_type=spec["entity_type"],
                    existing_entity_id=spec.get("existing_id"),
                    directory_record_id=spec.get("directory_id"),
                    contact_candidate_id=spec.get("contact_id"),
                    proposed_event_type=spec["event_type"],
                    old_value=spec.get("old_value"),
                    new_value=spec.get("new_value"),
                    reason=spec["reason"],
                    source_locator=spec.get("locator"),
                    actionable=spec.get("actionable", True),
                    blocked_reason=spec.get("blocked_reason"),
                    review_status=ReviewStatus.PENDING_REVIEW,
                )
                self.session.add(candidate)
            detection.status = RunStatus.SUCCESS
            detection.candidates_found = len(specs)
            detection.finished_at = utc_now()
            self.session.commit()
            return {"detection_id": str(detection.id), "candidates": len(specs), "reused": False}
        except Exception as error:
            self.session.rollback()
            failed = ChangeDetection(
                source_id=observation.source_id, observation_id=observation.id,
                extraction_run_id=extraction.id, agency_id=resolved_agency,
                detector_name=self.NAME, detector_version=self.VERSION,
                coverage_mode=source.coverage_mode, status=RunStatus.FAILED,
                candidates_found=0, error_summary=str(error),
                started_at=utc_now(), finished_at=utc_now(),
            )
            self.session.add(failed)
            self.session.commit()
            raise

    def _prior_entities(self, source_id, agency_id):
        result = {EntityType.ORG_UNIT: [], EntityType.DUTY: [], EntityType.CONTACT_POINT: []}
        occurrences = list(self.session.scalars(
            select(SourceOccurrence)
            .where(SourceOccurrence.observation.has(source_id=source_id))
        ))
        seen = set()
        for occurrence in occurrences:
            marker = (occurrence.entity_type, occurrence.entity_id)
            if marker in seen or occurrence.entity_type not in result:
                continue
            seen.add(marker)
            model = {EntityType.ORG_UNIT: OrgUnit, EntityType.DUTY: Duty, EntityType.CONTACT_POINT: ContactPoint}[occurrence.entity_type]
            entity = self.session.get(model, occurrence.entity_id)
            if entity is not None and entity.agency_id == agency_id:
                result[occurrence.entity_type].append(entity)
        return result

    def _missing_specs(self, prior, orgs, duties, contacts):
        specs = []
        for entity in prior[EntityType.CONTACT_POINT]:
            key = (
                normalize_text(entity.org_unit.name if entity.org_unit else ""),
                normalize_text(entity.duty.title if entity.duty else ""),
                entity.contact_type.value, entity.normalized_value,
            )
            if entity.active and key not in contacts:
                specs.append(self._spec(
                    EntityType.CONTACT_POINT, ChangeEventType.CONTACT_MISSING, None, None,
                    entity.id, None, "Previously evidenced contact is absent from complete snapshot",
                    None, old_value={"type": entity.contact_type.value, "value": entity.value,
                                    "normalized_value": entity.normalized_value},
                ))
        for entity in prior[EntityType.DUTY]:
            key = (normalize_text(entity.org_unit.name if entity.org_unit else ""), normalize_text(entity.title))
            if entity.active and key not in duties:
                specs.append(self._spec(
                    EntityType.DUTY, ChangeEventType.ENTITY_MISSING, None, None,
                    entity.id, None, "Previously evidenced duty is absent from complete snapshot",
                    None, old_value={"title": entity.title},
                ))
        for entity in prior[EntityType.ORG_UNIT]:
            if entity.active and normalize_text(entity.name) not in orgs:
                specs.append(self._spec(
                    EntityType.ORG_UNIT, ChangeEventType.ENTITY_MISSING, None, None,
                    entity.id, None, "Previously evidenced organization unit is absent from complete snapshot",
                    None, old_value={"name": entity.name},
                ))
        return specs

    def _generic_specs(self, observation_id, source_id, agency_id):
        observation = self.session.get(Observation, observation_id)
        contacts = (
            SemanticDiscoveryProjector(self.session).standalone_contacts_for_observation(
                observation.crawl_run_id, observation_id,
            )
            if observation is not None else ()
        )
        bindings = list(self.session.scalars(
            select(SourceBinding).where(SourceBinding.source_id == source_id, SourceBinding.active.is_(True))
        ))
        agency_ids = {binding.agency_id for binding in bindings}
        specs = []
        for contact in contacts:
            contact_type = ContactType(contact.candidate_type.value)
            scope = classify_contact_scope(contact.source_locator, contact.context_text)
            normalized = normalize_contact(contact_type, contact.raw_value)
            if len(agency_ids) != 1 or agency_id not in agency_ids:
                specs.append(self._spec(
                    EntityType.CONTACT_POINT, ChangeEventType.OTHER, None, contact.id,
                    None, {"type": contact_type.value, "value": contact.raw_value,
                           "normalized_value": normalized},
                    "CONTEXT_REQUIRED", contact.source_locator, False,
                    "Generic contact has multiple or missing active Agency bindings.",
                ))
                continue
            org_id = self.planner.standalone_org_id(source_id, agency_id, scope)
            contextual = list(self.session.scalars(select(ContactPoint).where(
                ContactPoint.agency_id == agency_id,
                ContactPoint.org_unit_id == org_id,
                ContactPoint.duty_id.is_(None),
                ContactPoint.person_assignment_id.is_(None),
                ContactPoint.contact_type == contact_type,
                ContactPoint.active.is_(True),
            )))
            if any(item.normalized_value == normalized for item in contextual):
                continue
            new_value = {
                "type": contact_type.value, "value": contact.raw_value,
                "normalized_value": normalized,
                "org_unit_id": str(org_id) if org_id else None,
                "duty_id": None,
            }
            if len(contextual) == 1:
                old = contextual[0]
                specs.append(self._spec(
                    EntityType.CONTACT_POINT, ChangeEventType.CONTACT_CHANGED,
                    None, contact.id, old.id, new_value,
                    "Safe contextual contact replacement", contact.source_locator,
                    old_value={"type": old.contact_type.value, "value": old.value,
                               "normalized_value": old.normalized_value},
                ))
            elif len(contextual) > 1:
                specs.append(self._spec(
                    EntityType.CONTACT_POINT, ChangeEventType.OTHER,
                    None, contact.id, None, new_value,
                    "AMBIGUOUS_CONTACT_REPLACEMENT", contact.source_locator,
                    False, "More than one prior contextual contact matches.",
                ))
            else:
                specs.append(self._spec(
                    EntityType.CONTACT_POINT, ChangeEventType.CONTACT_ADDED,
                    None, contact.id, None, new_value,
                    "Generic page contact requires review", contact.source_locator,
                ))
        return specs

    @staticmethod
    def _spec(entity_type, event_type, directory_id, contact_id, existing_id, new_value,
              reason, locator, actionable=True, blocked_reason=None, old_value=None):
        return {
            "entity_type": entity_type, "event_type": event_type,
            "directory_id": directory_id, "contact_id": contact_id,
            "existing_id": existing_id, "old_value": old_value,
            "new_value": new_value, "reason": reason, "locator": locator,
            "actionable": actionable, "blocked_reason": blocked_reason,
        }
