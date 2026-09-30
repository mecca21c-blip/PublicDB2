from __future__ import annotations

from datetime import timedelta
import socket
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.main import create_app
from app.models import (
    AgencyType, CandidateType, ChangeDetection, ChangeEvent, ChangeEventType,
    ContactHistory, ContactPoint, ContactType, DetectionMethod, DetectedChangeCandidate,
    DirectoryRecordType, Duty, EntityType, ExtractedContactCandidate,
    ExtractedDirectoryRecord, ExtractionRun, ExtractionStatus, OrgUnit, OrgUnitType,
    Person, PersonAssignment, ReviewStatus, RunStatus, Source, SourceCoverageMode,
    SourceOccurrence, StageStatus, CrawlRun, Observation,
)
from app.models.common import utc_now
from app.services.agency_service import AgencyService
from app.services.collection_service import CollectionCoordinator, CollectionService
from app.services.master_promotion_apply_service import MasterPromotionApplyService
from app.services.master_promotion_planner import AgencyContextRequired, MasterPromotionPlanner
from app.services.review_service import ReviewConflict, ReviewService
from app.services.source_change_detection_service import SourceChangeDetectionService
from app.services.source_service import SourceService


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "publicdb2.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    with factory() as session:
        yield url, session
    engine.dispose()


def add_context(session, url="https://example.org/staff", agency_name="Agency One"):
    agency, _ = AgencyService(session).create_agency(
        official_name=agency_name, agency_type=AgencyType.OTHER
    )
    agency_id = uuid.UUID(agency["id"])
    unit = AgencyService(session).create_org_unit(
        agency_id=agency_id, name="Binding Unit", unit_type=OrgUnitType.DEPARTMENT
    )
    binding, _ = SourceService(session).register_binding(
        url=url, agency_id=agency_id, org_unit_id=uuid.UUID(unit["id"])
    )
    return agency_id, uuid.UUID(binding["source_id"]), uuid.UUID(unit["id"])


def add_extraction(
    session, source_id, *, org="Digital Office", duty="Public Data",
    phone="02-1111-2222", email="team@example.org", person="Discovered Name",
    generic=(),
):
    observed = utc_now()
    run = CrawlRun(
        source_id=source_id, status=RunStatus.SUCCESS,
        connection_status=StageStatus.SUCCESS, raw_status=StageStatus.SUCCESS,
        extraction_status=StageStatus.SUCCESS, started_at=observed,
        finished_at=observed, records_observed=1 + len(generic), http_status=200,
    )
    session.add(run)
    session.flush()
    observation = Observation(
        crawl_run_id=run.id, source_id=source_id, observed_at=observed,
        page_url="https://example.org/staff",
    )
    session.add(observation)
    session.flush()
    extraction = ExtractionRun(
        observation_id=observation.id, extractor_name="staff_directory",
        extractor_version="test-1", status=ExtractionStatus.SUCCESS,
        started_at=observed, finished_at=observed, candidates_found=1,
    )
    session.add(extraction)
    session.flush()
    record = ExtractedDirectoryRecord(
        extraction_run_id=extraction.id, observation_id=observation.id,
        record_type=DirectoryRecordType.STAFF_DIRECTORY_ROW,
        org_unit_text=org, duty_text=duty, person_name_text=person,
        phone_text=phone, email_text=email, row_text=f"{org} {duty}",
        source_locator="table[0]/row[1]",
    )
    session.add(record)
    if generic:
        html = ExtractionRun(
            observation_id=observation.id, extractor_name="html_contact",
            extractor_version="test-1", status=ExtractionStatus.SUCCESS,
            started_at=observed, finished_at=observed, candidates_found=len(generic),
        )
        session.add(html)
        session.flush()
        for index, (kind, raw, normalized) in enumerate(generic):
            session.add(ExtractedContactCandidate(
                extraction_run_id=html.id, observation_id=observation.id,
                candidate_type=kind, raw_value=raw, normalized_value=normalized,
                context_text="generic page contact", source_locator=f"body[{index}]",
                detection_method=DetectionMethod.TEXT_PATTERN,
            ))
    session.commit()
    return extraction.id, observation.id, record.id


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_baseline_preview_apply_person_safety_provenance_and_idempotency(db):
    _, session = db
    agency_id, source_id, _ = add_context(session)
    assert session.get(Source, source_id).coverage_mode is SourceCoverageMode.UNKNOWN
    extraction_id, observation_id, _ = add_extraction(session, source_id)
    before = {model: count(session, model) for model in (
        OrgUnit, Duty, ContactPoint, ChangeEvent, ContactHistory, SourceOccurrence
    )}
    preview = MasterPromotionApplyService(session).preview(extraction_id)
    after_preview = {model: count(session, model) for model in before}
    assert before == after_preview
    assert preview["directory_records"] == 1
    assert preview["person_names_detected"] == 1
    assert preview["rows_requiring_review"] == 0
    result = MasterPromotionApplyService(session).apply(extraction_id)
    assert result["org_units_created"] == 1
    assert result["duties_created"] == 1
    assert result["contacts_created"] == 2
    assert count(session, Person) == 0
    assert count(session, PersonAssignment) == 0
    assert count(session, SourceOccurrence) == 4
    assert count(session, ContactHistory) == 2
    contacts = list(session.scalars(select(ContactPoint)))
    assert all(item.person_assignment_id is None for item in contacts)
    assert all(item.verified_at == session.get(Observation, observation_id).observed_at for item in contacts)
    event_count, occurrence_count, history_count = (
        count(session, ChangeEvent), count(session, SourceOccurrence), count(session, ContactHistory)
    )
    again = MasterPromotionApplyService(session).apply(extraction_id)
    assert again["org_units_created"] == 0
    assert again["duties_created"] == 0
    assert again["contacts_created"] == 0
    assert (count(session, ChangeEvent), count(session, SourceOccurrence), count(session, ContactHistory)) == (
        event_count, occurrence_count, history_count
    )
    assert SourceChangeDetectionService(session).baseline_exists(source_id, agency_id)


def test_ambiguous_rows_skip_and_multi_agency_requires_selection(db):
    _, session = db
    first, source_id, _ = add_context(session)
    second, same_source, _ = add_context(session, agency_name="Agency Two")
    assert source_id == same_source
    extraction_id, _, _ = add_extraction(session, source_id)
    with pytest.raises(AgencyContextRequired):
        MasterPromotionPlanner(session).plan(extraction_id)
    assert MasterPromotionPlanner(session).plan(extraction_id, first).agency_id == first
    session.add_all([
        OrgUnit(agency_id=first, name="Digital Office", normalized_name="digital office", unit_type=OrgUnitType.OTHER),
        OrgUnit(agency_id=first, name="Digital Office", normalized_name="digital office", unit_type=OrgUnitType.OTHER),
    ])
    session.commit()
    preview = MasterPromotionApplyService(session).preview(extraction_id, first)
    assert preview["rows_requiring_review"] == 1
    result = MasterPromotionApplyService(session).apply(extraction_id, first)
    assert result["rows_skipped"] == 1
    assert count(session, ContactPoint) == 0
    assert second != first


@pytest.mark.parametrize(
    ("mode", "expected_missing"),
    [
        (SourceCoverageMode.UNKNOWN, 0),
        (SourceCoverageMode.ADDITIVE_ONLY, 0),
        (SourceCoverageMode.COMPLETE_SNAPSHOT, 1),
    ],
)
def test_coverage_controls_missing_contact_detection(db, mode, expected_missing):
    _, session = db
    agency_id, source_id, _ = add_context(session)
    baseline, _, _ = add_extraction(session, source_id, email=None)
    MasterPromotionApplyService(session).apply(baseline)
    source = session.get(Source, source_id)
    source.coverage_mode = mode
    session.commit()
    current, _, _ = add_extraction(session, source_id, phone=None, email=None, person=None)
    SourceChangeDetectionService(session).generate(current)
    missing = session.scalar(select(func.count()).select_from(DetectedChangeCandidate).where(
        DetectedChangeCandidate.proposed_event_type == ChangeEventType.CONTACT_MISSING
    ))
    assert missing == expected_missing
    if expected_missing:
        candidate = session.scalar(select(DetectedChangeCandidate).where(
            DetectedChangeCandidate.proposed_event_type == ChangeEventType.CONTACT_MISSING
        ))
        contact_id = candidate.existing_entity_id
        ReviewService(session).approve(candidate.id)
        assert session.get(ContactPoint, contact_id) is not None
        assert not session.get(ContactPoint, contact_id).active


def test_new_entities_detection_review_apply_and_detection_idempotency(db):
    _, session = db
    agency_id, source_id, _ = add_context(session)
    baseline, _, _ = add_extraction(session, source_id, email=None)
    MasterPromotionApplyService(session).apply(baseline)
    current, observation_id, _ = add_extraction(
        session, source_id, org="New Bureau", duty="New Duty",
        phone="02-3333-4444", email=None, person="Name Only",
    )
    service = SourceChangeDetectionService(session)
    first = service.generate(current)
    second = service.generate(current)
    assert not first["reused"] and second["reused"]
    candidates = list(session.scalars(select(DetectedChangeCandidate).where(
        DetectedChangeCandidate.detection_run_id == uuid.UUID(first["detection_id"])
    )))
    assert {item.entity_type for item in candidates} >= {
        EntityType.ORG_UNIT, EntityType.DUTY, EntityType.CONTACT_POINT
    }
    for entity_type in (EntityType.ORG_UNIT, EntityType.DUTY, EntityType.CONTACT_POINT):
        candidate = next(item for item in candidates if item.entity_type is entity_type)
        result = ReviewService(session).approve(candidate.id)
        assert not result["reused"]
        assert ReviewService(session).approve(candidate.id)["reused"]
    assert count(session, Person) == 0
    assert count(session, PersonAssignment) == 0
    created = session.scalar(select(ContactPoint).where(ContactPoint.normalized_value == "0233334444"))
    assert created is not None
    assert count(session, ContactHistory) == 2
    assert session.scalar(select(func.count()).select_from(SourceOccurrence).where(
        SourceOccurrence.observation_id == observation_id,
        SourceOccurrence.entity_type == EntityType.CONTACT_POINT,
    )) == 1


def test_replacement_history_approve_once_and_stale_protection(db):
    _, session = db
    agency_id, source_id, _ = add_context(session)
    baseline, _, _ = add_extraction(session, source_id, email=None)
    MasterPromotionApplyService(session).apply(baseline)
    current, _, _ = add_extraction(session, source_id, phone="02-9999-0000", email=None)
    SourceChangeDetectionService(session).generate(current)
    candidate = session.scalar(select(DetectedChangeCandidate).where(
        DetectedChangeCandidate.proposed_event_type == ChangeEventType.CONTACT_CHANGED
    ))
    assert candidate is not None and candidate.actionable
    contact_id = candidate.existing_entity_id
    ReviewService(session).approve(candidate.id)
    contact = session.get(ContactPoint, contact_id)
    assert contact.normalized_value == "0299990000"
    histories = list(session.scalars(select(ContactHistory).where(ContactHistory.contact_id == contact_id)))
    assert len(histories) == 2
    assert sum(item.active for item in histories) == 1
    assert ReviewService(session).approve(candidate.id)["reused"]

    later, _, _ = add_extraction(session, source_id, phone="02-7777-8888", email=None)
    SourceChangeDetectionService(session).generate(later)
    stale = session.scalar(select(DetectedChangeCandidate).where(
        DetectedChangeCandidate.proposed_event_type == ChangeEventType.CONTACT_CHANGED,
        DetectedChangeCandidate.review_status == ReviewStatus.PENDING_REVIEW,
    ))
    contact.value = "02-0000-0000"
    contact.normalized_value = "0200000000"
    session.commit()
    with pytest.raises(ReviewConflict, match="Re-review required"):
        ReviewService(session).approve(stale.id)
    assert session.get(DetectedChangeCandidate, stale.id).review_status is ReviewStatus.PENDING_REVIEW


def test_generic_candidates_context_review_keep_defer_and_later_approve(db):
    url, session = db
    agency_id, source_id, binding_unit = add_context(session)
    baseline, _, _ = add_extraction(session, source_id, email=None)
    MasterPromotionApplyService(session).apply(baseline)
    current, _, _ = add_extraction(
        session, source_id, phone="02-1111-2222", email=None,
        generic=(
            (CandidateType.EMAIL, "generic@example.org", "generic@example.org"),
            (CandidateType.FAX, "02-5555-6666", "0255556666"),
        ),
    )
    SourceChangeDetectionService(session).generate(current)
    generic = list(session.scalars(select(DetectedChangeCandidate).where(
        DetectedChangeCandidate.contact_candidate_id.is_not(None)
    )))
    assert len(generic) == 2 and all(item.actionable for item in generic)
    deferred, rejected = generic
    assert ReviewService(session).defer(deferred.id)["status"] == "DEFERRED"
    assert ReviewService(session).defer(deferred.id)["reused"]
    assert ReviewService(session).approve(deferred.id)["status"] == "APPROVED"
    assert ReviewService(session).reject(rejected.id)["status"] == "REJECTED"
    assert ReviewService(session).reject(rejected.id)["reused"]
    assert session.scalar(select(ContactPoint).where(
        ContactPoint.org_unit_id == binding_unit,
        ContactPoint.normalized_value == (deferred.new_value or {})["normalized_value"],
    )) is not None

    second_agency, same_source, _ = add_context(session, agency_name="Agency Two")
    ambiguous, _, _ = add_extraction(
        session, source_id, generic=((CandidateType.EMAIL, "blocked@example.org", "blocked@example.org"),)
    )
    SourceChangeDetectionService(session).generate(ambiguous, agency_id)
    blocked = session.scalar(select(DetectedChangeCandidate).where(
        DetectedChangeCandidate.contact_candidate_id.is_not(None),
        DetectedChangeCandidate.reason == "CONTEXT_REQUIRED",
    ))
    assert blocked is not None and not blocked.actionable
    with pytest.raises(ReviewConflict):
        ReviewService(session).approve(blocked.id)
    with TestClient(create_app(url)) as client:
        page = client.get(
            f"/review?agency_id={agency_id}&review_status=PENDING_REVIEW"
            "&change_type=OTHER&search=CONTEXT_REQUIRED"
        )
    assert str(blocked.id) in page.text
    assert f'data-candidate-id="{blocked.id}" disabled' in page.text
    assert second_agency != agency_id and same_source == source_id


def test_contacts_review_routes_filters_source_and_no_discovery_leak(db, monkeypatch):
    url, session = db
    agency_id, source_id, _ = add_context(session, agency_name="Search Agency")
    baseline, observation_id, _ = add_extraction(session, source_id)
    MasterPromotionApplyService(session).apply(baseline)
    observation = session.get(Observation, observation_id)
    html = ExtractionRun(
        observation_id=observation_id, extractor_name="html_contact",
        extractor_version="unapproved", status=ExtractionStatus.SUCCESS,
        started_at=observation.observed_at, finished_at=observation.observed_at,
        candidates_found=1,
    )
    session.add(html)
    session.flush()
    session.add(ExtractedContactCandidate(
        extraction_run_id=html.id, observation_id=observation_id,
        candidate_type=CandidateType.EMAIL, raw_value="unapproved@example.org",
        normalized_value="unapproved@example.org", context_text="discovery only",
        source_locator="body[99]", detection_method=DetectionMethod.TEXT_PATTERN,
    ))
    session.commit()
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("external network"))
    with TestClient(create_app(url)) as client:
        contacts = client.get("/contacts")
        searched = client.get("/contacts?search=Search+Agency")
        phones = client.get("/contacts?contact_type=PHONE")
        emails = client.get("/contacts?contact_type=EMAIL")
        filtered = client.get(f"/contacts?agency_id={agency_id}")
        org_id = session.scalar(select(OrgUnit.id).where(OrgUnit.name == "Digital Office"))
        org_filtered = client.get(f"/contacts?org_unit_id={org_id}")
        review = client.get("/review")
        routes = [client.get(path) for path in ("/", "/agencies", "/sources", "/runs", "/contacts", "/review", "/settings")]
    assert "02-1111-2222" in contacts.text
    assert "team@example.org" in contacts.text
    assert "https://example.org/staff" in contacts.text
    assert "unapproved@example.org" not in contacts.text
    assert "Search Agency" in searched.text and "Search Agency" in filtered.text
    assert "Digital Office" in org_filtered.text
    assert "02-1111-2222" in phones.text and "team@example.org" not in phones.text
    assert "team@example.org" in emails.text and "02-1111-2222" not in emails.text
    assert "sample data" not in review.text.casefold()
    assert all(response.status_code == 200 for response in routes)
    status_before = SourceService(session).get_binding(
        session.scalar(select(Source).where(Source.id == source_id)).bindings[0].id
    )["status_code"]
    detection = ChangeDetection(
        source_id=source_id, observation_id=observation_id, extraction_run_id=baseline,
        agency_id=agency_id, status=RunStatus.SUCCESS, coverage_mode=SourceCoverageMode.UNKNOWN,
        detector_name="manual-test", detector_version="1", candidates_found=1,
        started_at=utc_now(), finished_at=utc_now(),
    )
    session.add(detection)
    session.commit()
    assert SourceService(session).get_binding(session.get(Source, source_id).bindings[0].id)["status_code"] == status_before


def test_ambiguous_contextual_replacement_is_non_actionable(db):
    _, session = db
    agency_id, source_id, _ = add_context(session)
    baseline, observation_id, _ = add_extraction(session, source_id, email=None)
    MasterPromotionApplyService(session).apply(baseline)
    original = session.scalar(select(ContactPoint).where(ContactPoint.contact_type == ContactType.PHONE))
    duplicate = ContactPoint(
        agency_id=agency_id, org_unit_id=original.org_unit_id, duty_id=original.duty_id,
        person_assignment_id=None, contact_type=original.contact_type,
        value="02-8888-9999", normalized_value="0288889999",
        purpose_text=original.purpose_text, verified_at=original.verified_at,
    )
    session.add(duplicate)
    session.flush()
    session.add(SourceOccurrence(
        observation_id=observation_id, entity_type=EntityType.CONTACT_POINT,
        entity_id=duplicate.id, field_name="phone", observed_value=duplicate.value,
        context_text="duplicate prior evidence", source_locator="manual[1]",
        observed_at=session.get(Observation, observation_id).observed_at,
    ))
    session.commit()
    current, _, _ = add_extraction(session, source_id, phone="02-7777-0000", email=None)
    SourceChangeDetectionService(session).generate(current)
    candidate = session.scalar(select(DetectedChangeCandidate).where(
        DetectedChangeCandidate.reason == "AMBIGUOUS_CONTACT_REPLACEMENT"
    ))
    assert candidate is not None
    assert not candidate.actionable
    assert candidate.blocked_reason


def test_stored_run_api_promotion_coverage_and_future_detection_hook(db):
    url, session = db
    agency_id, source_id, _ = add_context(session)
    baseline, _, _ = add_extraction(session, source_id, email=None)
    with TestClient(create_app(url)) as client:
        run_page = client.get("/runs")
        assert 'data-master-action="promote"' in run_page.text
        preview = client.get(f"/api/extractions/{baseline}/promotion-preview")
        assert preview.status_code == 200
        promoted = client.post(f"/api/extractions/{baseline}/promote")
        assert promoted.status_code == 200
        coverage = client.patch(
            f"/api/sources/{source_id}/coverage",
            json={"coverage_mode": "ADDITIVE_ONLY"},
        )
        assert coverage.status_code == 200
    session.expire_all()
    assert session.get(Source, source_id).coverage_mode is SourceCoverageMode.ADDITIVE_ONLY
    future, _, _ = add_extraction(session, source_id, phone="02-2222-3333", email=None)
    CollectionService(
        session, project_root=Path.cwd(), raw_root=Path.cwd() / "data" / "raw",
        coordinator=CollectionCoordinator(),
    )._detect_changes_best_effort(future)
    detection = session.scalar(select(ChangeDetection).where(
        ChangeDetection.extraction_run_id == future,
        ChangeDetection.status == RunStatus.SUCCESS,
    ))
    assert detection is not None
    run = session.get(CrawlRun, session.get(Observation, detection.observation_id).crawl_run_id)
    assert run.status is RunStatus.SUCCESS


def test_detection_failure_never_rewrites_successful_crawl_run(db, monkeypatch):
    _, session = db
    _, source_id, _ = add_context(session)
    baseline, _, _ = add_extraction(session, source_id, email=None)
    MasterPromotionApplyService(session).apply(baseline)
    future, observation_id, _ = add_extraction(session, source_id, phone="02-4444-5555", email=None)

    def fail_detection(*_args, **_kwargs):
        raise RuntimeError("isolated detection failure")

    monkeypatch.setattr(SourceChangeDetectionService, "generate", fail_detection)
    CollectionService(
        session, project_root=Path.cwd(), raw_root=Path.cwd() / "data" / "raw",
        coordinator=CollectionCoordinator(),
    )._detect_changes_best_effort(future)
    run = session.get(CrawlRun, session.get(Observation, observation_id).crawl_run_id)
    assert run.status is RunStatus.SUCCESS
    assert run.extraction_status is StageStatus.SUCCESS
