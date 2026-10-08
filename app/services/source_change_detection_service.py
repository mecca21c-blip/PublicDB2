"""Persisted, Source-scoped change detection without confirmed-data mutation."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ChangeDetection, ChangeEventType, ContactPoint, ContactType,
    DetectedChangeCandidate, DirectoryRecordType, Duty, EntityType,
    ExtractedDirectoryRecord, ExtractionRun, ExtractionStatus,
    Observation, OrgUnit, ReviewStatus, RunStatus, Source, SourceBinding, SourceCoverageMode,
    SourceOccurrence,
)
from app.models.common import utc_now
from app.collectors.html_contact_extractor import is_supported_phone, normalize_email
from app.core.discovery_quality import classify_contact_scope
from app.services.master_normalization import normalize_contact, normalize_text
from app.services.master_promotion_planner import MasterPromotionPlanner, PromotionError
from app.services.operation_claim_service import OperationClaimService
from app.services.review_service import ReviewService
from app.services.semantic_discovery_service import SemanticDiscoveryProjector


class DetectionError(ValueError):
    pass


logger = logging.getLogger(__name__)
AUTO_RESOLUTION_NOTE = "AUTO_SAFE_STRUCTURED_CONTACT_CHANGE"
AUTO_CLASSIFICATION = "STRICT_STRUCTURED_ONE_TO_ONE"
AUTO_CONTACT_TYPES = frozenset((ContactType.PHONE, ContactType.EMAIL, ContactType.FAX))


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
                structured_groups: dict[tuple, list[tuple]] = {}
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
                        structured_groups.setdefault(
                            (row.org_id, row.duty_id, contact.contact_type), []
                        ).append((row, contact))
                structured_specs, paired_contact_ids = self._structured_contact_specs(
                    resolved_agency, structured_groups
                )
                specs.extend(structured_specs)
                if source.coverage_mode is SourceCoverageMode.COMPLETE_SNAPSHOT and complete:
                    specs.extend(self._missing_specs(
                        prior, current_orgs, current_duties, current_contacts,
                        paired_contact_ids,
                    ))
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

    def auto_resolve(self, detection_id: uuid.UUID) -> dict:
        """Resolve only globally safe structured one-to-one contact changes."""
        detection = self.session.scalar(
            select(ChangeDetection)
            .options(selectinload(ChangeDetection.candidates))
            .where(ChangeDetection.id == detection_id)
        )
        if detection is None or detection.status is not RunStatus.SUCCESS:
            raise DetectionError("A successful change detection is required.")
        observation_id = detection.observation_id
        resource_key = f"auto-change:{observation_id}:{detection.agency_id}"
        claims = OperationClaimService(self.session)
        acquired = claims.acquire(resource_key, "AUTO_SAFE_STRUCTURED_CHANGE", commit=True)
        if not acquired.acquired:
            if acquired.claim.status == "COMPLETED" and acquired.claim.result_payload:
                return {**acquired.claim.result_payload, "reused": True}
            return {"detection_id": str(detection.id), "resolved": 0, "reused": True}

        resolved = 0
        failed = 0
        candidate_ids = [candidate.id for candidate in detection.candidates]
        for candidate_id in candidate_ids:
            candidate = self.session.get(DetectedChangeCandidate, candidate_id)
            if candidate is None or not self._auto_eligible(candidate):
                continue
            try:
                ReviewService(self.session).approve(candidate.id, AUTO_RESOLUTION_NOTE)
                resolved += 1
            except Exception as error:
                self.session.rollback()
                failed += 1
                logger.warning(
                    "auto_change_review_required candidate_id=%s observation_id=%s error_class=%s",
                    candidate_id, observation_id, error.__class__.__name__,
                )

        claim = self.session.get(type(acquired.claim), acquired.claim.id)
        result = {
            "detection_id": str(detection.id),
            "resolved": resolved,
            "failed": failed,
            "reused": False,
        }
        if claim is not None:
            claims.complete(claim, result)
            self.session.commit()
        return result

    def _structured_contact_specs(self, agency_id, groups):
        specs: list[dict] = []
        paired_contact_ids: set[uuid.UUID] = set()
        for (org_id, duty_id, contact_type), evidence in groups.items():
            unique = {}
            for row, contact in evidence:
                unique.setdefault(contact.normalized_value, (row, contact))
            existing = list(self.session.scalars(select(ContactPoint).where(
                ContactPoint.agency_id == agency_id,
                ContactPoint.org_unit_id == org_id,
                ContactPoint.duty_id == duty_id,
                ContactPoint.person_assignment_id.is_(None),
                ContactPoint.contact_type == contact_type,
                ContactPoint.active.is_(True),
            )))
            existing_values = {item.normalized_value for item in existing}
            new_evidence = [item for normalized, item in unique.items() if normalized not in existing_values]
            if not new_evidence:
                continue

            if len(existing) == 1:
                old = existing[0]
                exact_pair = len(unique) == 1 and old.normalized_value not in unique
                if exact_pair:
                    paired_contact_ids.add(old.id)
                for row, contact in new_evidence:
                    reason = (
                        "STRICT_STRUCTURED_CONTACT_REPLACEMENT"
                        if exact_pair else "MULTIPLE_STRUCTURED_CONTACT_VALUES"
                    )
                    specs.append(self._structured_change_spec(
                        row, contact, old, reason, auto_classified=exact_pair,
                    ))
                continue

            if len(existing) > 1:
                for row, contact in new_evidence:
                    values = self._structured_values(row, contact)
                    specs.append(self._spec(
                        EntityType.CONTACT_POINT, ChangeEventType.OTHER,
                        row.record.id, None, None, values,
                        "AMBIGUOUS_CONTACT_REPLACEMENT", row.record.source_locator,
                        False, "More than one active contact occupies the semantic slot.",
                    ))
                continue

            for row, contact in new_evidence:
                specs.append(self._spec(
                    EntityType.CONTACT_POINT, ChangeEventType.CONTACT_ADDED,
                    row.record.id, None, None, self._structured_values(row, contact),
                    "New contextual contact", row.record.source_locator,
                ))
        return specs, paired_contact_ids

    def _structured_change_spec(self, row, contact, old, reason, *, auto_classified):
        values = self._structured_values(row, contact)
        if auto_classified:
            values["auto_classification"] = AUTO_CLASSIFICATION
        return self._spec(
            EntityType.CONTACT_POINT, ChangeEventType.CONTACT_CHANGED,
            row.record.id, None, old.id, values,
            reason, row.record.source_locator,
            old_value={
                "type": old.contact_type.value,
                "value": old.value,
                "normalized_value": old.normalized_value,
            },
        )

    @staticmethod
    def _structured_values(row, contact):
        return {
            "type": contact.contact_type.value,
            "value": contact.value,
            "normalized_value": contact.normalized_value,
            "org_unit_id": str(row.org_id) if row.org_id else None,
            "duty_id": str(row.duty_id) if row.duty_id else None,
            "person_assignment_id": None,
            "org_unit_name": row.org_name,
            "duty_title": row.duty_title,
        }

    def _auto_eligible(self, candidate: DetectedChangeCandidate) -> bool:
        if (
            candidate.review_status is not ReviewStatus.PENDING_REVIEW
            or not candidate.actionable
            or candidate.directory_record_id is None
            or candidate.contact_candidate_id is not None
            or candidate.entity_type is not EntityType.CONTACT_POINT
            or candidate.proposed_event_type is not ChangeEventType.CONTACT_CHANGED
            or candidate.existing_entity_id is None
            or not isinstance(candidate.new_value, dict)
            or candidate.new_value.get("auto_classification") != AUTO_CLASSIFICATION
        ):
            return False
        detection = self.session.get(ChangeDetection, candidate.detection_run_id)
        if detection is None or detection.extraction_run_id is None:
            return False
        record = self.session.get(ExtractedDirectoryRecord, candidate.directory_record_id)
        if (
            record is None
            or record.extraction_run_id != detection.extraction_run_id
            or record.observation_id != candidate.observation_id
            or record.record_type is not DirectoryRecordType.STAFF_DIRECTORY_ROW
        ):
            return False
        try:
            plan = self.planner.plan(detection.extraction_run_id, candidate.agency_id)
        except PromotionError:
            return False
        if plan.source_id != candidate.source_id:
            return False
        agency_ids = set(self.session.scalars(
            select(SourceBinding.agency_id).where(
                SourceBinding.source_id == candidate.source_id,
                SourceBinding.active.is_(True),
            ).distinct()
        ))
        if agency_ids != {candidate.agency_id}:
            return False

        values = candidate.new_value if isinstance(candidate.new_value, dict) else {}
        old = candidate.old_value if isinstance(candidate.old_value, dict) else {}
        try:
            contact_type = ContactType(values.get("type"))
        except ValueError:
            return False
        if contact_type not in AUTO_CONTACT_TYPES:
            return False
        normalized = self._validated_normalized(contact_type, values.get("value"))
        if not normalized or normalized != values.get("normalized_value"):
            return False
        if normalized == old.get("normalized_value"):
            return False

        target_row = next(
            (row for row in plan.rows if row.record.id == candidate.directory_record_id),
            None,
        )
        if target_row is None or not target_row.safe or target_row.duty_id is None:
            return False
        expected_org = str(target_row.org_id) if target_row.org_id else None
        if values.get("org_unit_id") != expected_org or values.get("duty_id") != str(target_row.duty_id):
            return False
        if not self._deterministic_org(plan.source_id, candidate.agency_id, target_row):
            return False
        duty_matches = [item for item in self.session.scalars(select(Duty).where(
            Duty.agency_id == candidate.agency_id,
            Duty.org_unit_id == target_row.org_id,
            Duty.active.is_(True),
        )) if normalize_text(item.title) == normalize_text(target_row.duty_title)]
        if len(duty_matches) != 1 or duty_matches[0].id != target_row.duty_id:
            return False

        current_values = {
            contact.normalized_value
            for row in plan.rows if row.safe
            and row.org_id == target_row.org_id
            and row.duty_id == target_row.duty_id
            for contact in row.contacts if contact.contact_type is contact_type
        }
        if current_values != {normalized} or old.get("normalized_value") in current_values:
            return False

        slot = list(self.session.scalars(select(ContactPoint).where(
            ContactPoint.agency_id == candidate.agency_id,
            ContactPoint.org_unit_id == target_row.org_id,
            ContactPoint.duty_id == target_row.duty_id,
            ContactPoint.person_assignment_id.is_(None),
            ContactPoint.contact_type == contact_type,
            ContactPoint.active.is_(True),
        )))
        if len(slot) != 1:
            return False
        entity = slot[0]
        if entity.id != candidate.existing_entity_id or entity.normalized_value != old.get("normalized_value"):
            return False
        provenance_sources = set(self.session.scalars(
            select(Observation.source_id)
            .join(SourceOccurrence, SourceOccurrence.observation_id == Observation.id)
            .where(
                SourceOccurrence.entity_type == EntityType.CONTACT_POINT,
                SourceOccurrence.entity_id == entity.id,
            ).distinct()
        ))
        return provenance_sources == {candidate.source_id}

    def _deterministic_org(self, source_id, agency_id, row) -> bool:
        if row.org_name:
            matches = [item for item in self.session.scalars(select(OrgUnit).where(
                OrgUnit.agency_id == agency_id,
                OrgUnit.active.is_(True),
            )) if normalize_text(item.name) == normalize_text(row.org_name)]
            return len(matches) == 1 and matches[0].id == row.org_id
        bindings = self.planner.active_bindings(source_id, agency_id)
        return len(bindings) == 1 and bindings[0].org_unit_id == row.org_id

    @staticmethod
    def _validated_normalized(contact_type, value) -> str | None:
        if not isinstance(value, str):
            return None
        if contact_type in (ContactType.PHONE, ContactType.FAX) and not is_supported_phone(value):
            return None
        if contact_type is ContactType.EMAIL:
            try:
                normalize_email(value)
            except ValueError:
                return None
        return normalize_contact(contact_type, value) or None

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

    def _missing_specs(self, prior, orgs, duties, contacts, paired_contact_ids=()):
        specs = []
        for entity in prior[EntityType.CONTACT_POINT]:
            key = (
                normalize_text(entity.org_unit.name if entity.org_unit else ""),
                normalize_text(entity.duty.title if entity.duty else ""),
                entity.contact_type.value, entity.normalized_value,
            )
            if entity.active and entity.id not in paired_contact_ids and key not in contacts:
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
