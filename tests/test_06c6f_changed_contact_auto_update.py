from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.collectors.http_fetcher import HTTPFetcher
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    AgencyType,
    ChangeEvent,
    ChangeEventType,
    CollectionTriggerType,
    ContactHistory,
    ContactPoint,
    ContactType,
    DetectedChangeCandidate,
    Duty,
    EntityType,
    ExtractionRun,
    OperationClaim,
    Person,
    PersonAssignment,
    ReviewStatus,
    Source,
    SourceCoverageMode,
    SourceOccurrence,
)
from app.services.agency_service import AgencyService
from app.services.collection_job_service import CollectionJobService, SCHEDULED_FULL_PRIORITY
from app.services.collection_job_worker import CollectionJobWorker
from app.services.collection_service import CollectionCoordinator, CollectionService
from app.services.source_change_detection_service import (
    AUTO_RESOLUTION_NOTE,
    SourceChangeDetectionService,
)
from app.services.source_service import SourceService
from tests.support import regression_app


PUBLIC_DNS = lambda _host: {"93.184.216.34"}


@pytest.fixture()
def changed_contact_db(tmp_path, monkeypatch):
    path = tmp_path / "changed-contact.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    try:
        yield url, factory, tmp_path
    finally:
        engine.dispose()


def _count(session, model, *criteria) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    return int(session.scalar(statement) or 0)


def _agency_source(session, suffix: str, agency_id: uuid.UUID | None = None):
    if agency_id is None:
        agency, _ = AgencyService(session).create_agency(
            official_name=f"Changed Contact Agency {suffix}",
            agency_type=AgencyType.OTHER,
        )
        agency_id = uuid.UUID(agency["id"])
    binding, _ = SourceService(session).register_binding(
        url=f"https://example.org/changed/{suffix}", agency_id=agency_id,
    )
    return agency_id, uuid.UUID(binding["source_id"])


def _collector(session, root: Path, html: bytes) -> CollectionService:
    fetcher = HTTPFetcher(
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, headers={"Content-Type": "text/html; charset=utf-8"}, content=html,
        )),
        resolver=PUBLIC_DNS,
    )
    return CollectionService(
        session, project_root=root, raw_root=root / "data" / "raw",
        coordinator=CollectionCoordinator(), fetcher=fetcher,
    )


def _directory_html(
    *, duty: str = "Public health", phone: str = "", email: str = "", fax: str = "",
) -> bytes:
    return (
        "<html><body><main><table><thead><tr>"
        "<th>직위</th><th>업무</th><th>전화</th><th>이메일</th><th>팩스</th>"
        "</tr></thead><tbody><tr>"
        f"<td>Lead</td><td>{duty}</td><td>{phone}</td><td>{email}</td><td>{fax}</td>"
        "</tr></tbody></table></main></body></html>"
    ).encode("utf-8")


def _directory_phone_rows_html(*phones: str, duty: str = "Public health") -> bytes:
    rows = "".join(
        f"<tr><td>Lead</td><td>{duty}</td><td>{phone}</td></tr>"
        for phone in phones
    )
    return (
        "<html><body><main><table><thead><tr>"
        "<th>직위</th><th>업무</th><th>전화</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></main></body></html>"
    ).encode("utf-8")


def _pending(session):
    return list(session.scalars(select(DetectedChangeCandidate).where(
        DetectedChangeCandidate.review_status == ReviewStatus.PENDING_REVIEW,
    )))


@pytest.mark.parametrize(
    ("contact_type", "old", "new", "old_normalized", "new_normalized"),
    (
        (ContactType.PHONE, "02-2600-5861", "02-2600-5999", "0226005861", "0226005999"),
        (ContactType.EMAIL, "old@example.org", "new@example.org", "old@example.org", "new@example.org"),
        (ContactType.FAX, "02-2600-5000", "02-2600-5001", "0226005000", "0226005001"),
    ),
)
def test_strict_structured_exact_change_auto_updates_with_audit_history(
    changed_contact_db, contact_type, old, new, old_normalized, new_normalized,
):
    _url, factory, root = changed_contact_db
    fields = {"phone": "", "email": "", "fax": ""}
    field = contact_type.value.lower()
    with factory() as session:
        agency_id, source_id = _agency_source(session, f"exact-{field}")
        fields[field] = old
        first = _collector(session, root, _directory_html(**fields)).collect(source_id)
        contact = session.scalar(select(ContactPoint).where(
            ContactPoint.agency_id == agency_id,
            ContactPoint.contact_type == contact_type,
        ))
        contact_id = contact.id
        fields[field] = new
        second = _collector(session, root, _directory_html(**fields)).collect(source_id)

        session.expire_all()
        contact = session.get(ContactPoint, contact_id)
        candidate = session.scalar(select(DetectedChangeCandidate).where(
            DetectedChangeCandidate.observation_id == second.observation.id,
            DetectedChangeCandidate.proposed_event_type == ChangeEventType.CONTACT_CHANGED,
        ))
        histories = list(session.scalars(select(ContactHistory).where(
            ContactHistory.contact_id == contact_id,
        ).order_by(ContactHistory.valid_from)))
        assert contact.normalized_value == new_normalized
        assert candidate is not None
        assert candidate.review_status is ReviewStatus.APPROVED
        assert candidate.resolution_note == AUTO_RESOLUTION_NOTE
        assert len(histories) == 2
        assert sum(history.active for history in histories) == 1
        assert histories[0].normalized_value == old_normalized
        assert histories[0].valid_to == second.observation.observed_at
        assert histories[1].normalized_value == new_normalized
        assert _count(
            session, ChangeEvent,
            ChangeEvent.entity_id == contact_id,
            ChangeEvent.event_type == ChangeEventType.CONTACT_CHANGED,
        ) == 1
        assert _count(
            session, SourceOccurrence,
            SourceOccurrence.observation_id == second.observation.id,
            SourceOccurrence.entity_id == contact_id,
        ) == 1
        assert _count(session, Person) == 0
        assert _count(session, PersonAssignment) == 0


def test_multiple_new_values_and_multiple_existing_values_remain_review(changed_contact_db):
    _url, factory, root = changed_contact_db
    with factory() as session:
        agency_id, source_id = _agency_source(session, "multi-new")
        first = _collector(
            session, root, _directory_html(phone="02-1000-1000"),
        ).collect(source_id)
        original = session.scalar(select(ContactPoint).where(ContactPoint.agency_id == agency_id))
        _collector(
            session, root,
            _directory_phone_rows_html("02-2000-2000", "02-3000-3000"),
        ).collect(source_id)
        session.expire_all()
        assert session.get(ContactPoint, original.id).normalized_value == "0210001000"
        changed = [item for item in _pending(session) if item.proposed_event_type is ChangeEventType.CONTACT_CHANGED]
        assert len(changed) == 2

        duty = session.scalar(select(Duty).where(Duty.agency_id == agency_id))
        second_existing = ContactPoint(
            agency_id=agency_id, org_unit_id=None, duty_id=duty.id,
            person_assignment_id=None, contact_type=ContactType.PHONE,
            value="02-4000-4000", normalized_value="0240004000", active=True,
            verified_at=first.observation.observed_at,
        )
        session.add(second_existing)
        session.flush()
        session.add(SourceOccurrence(
            observation_id=first.observation.id,
            entity_type=EntityType.CONTACT_POINT, entity_id=second_existing.id,
            observed_at=first.observation.observed_at,
        ))
        session.commit()
        _collector(
            session, root, _directory_html(phone="02-5000-5000"),
        ).collect(source_id)
        session.expire_all()
        active = list(session.scalars(select(ContactPoint).where(
            ContactPoint.agency_id == agency_id, ContactPoint.active.is_(True),
        )))
        assert {item.normalized_value for item in active} == {"0210001000", "0240004000"}
        assert any(
            item.reason == "AMBIGUOUS_CONTACT_REPLACEMENT" and not item.actionable
            for item in _pending(session)
        )


def test_cross_source_provenance_blocks_auto_update(changed_contact_db):
    _url, factory, root = changed_contact_db
    old_html = _directory_html(phone="02-1111-1111")
    with factory() as session:
        agency_id, source_a = _agency_source(session, "source-a")
        session.get(Source, source_a).coverage_mode = SourceCoverageMode.COMPLETE_SNAPSHOT
        session.commit()
        _collector(session, root, old_html).collect(source_a)
        _agency, source_b = _agency_source(session, "source-b", agency_id)
        _collector(session, root, old_html).collect(source_b)
        contact = session.scalar(select(ContactPoint).where(ContactPoint.agency_id == agency_id))
        _collector(
            session, root, _directory_html(phone="02-2222-2222"),
        ).collect(source_a)

        session.expire_all()
        assert session.get(ContactPoint, contact.id).normalized_value == "0211111111"
        candidate = session.scalar(select(DetectedChangeCandidate).where(
            DetectedChangeCandidate.source_id == source_a,
            DetectedChangeCandidate.proposed_event_type == ChangeEventType.CONTACT_CHANGED,
        ).order_by(DetectedChangeCandidate.created_at.desc()))
        assert candidate is not None
        assert candidate.review_status is ReviewStatus.PENDING_REVIEW
        assert _count(
            session, DetectedChangeCandidate,
            DetectedChangeCandidate.observation_id == candidate.observation_id,
            DetectedChangeCandidate.proposed_event_type == ChangeEventType.CONTACT_MISSING,
        ) == 0


def test_context_change_inserts_new_context_without_replacing_old(changed_contact_db):
    _url, factory, root = changed_contact_db
    with factory() as session:
        agency_id, source_id = _agency_source(session, "context")
        _collector(
            session, root, _directory_html(duty="Duty A", phone="02-1111-1000"),
        ).collect(source_id)
        _collector(
            session, root, _directory_html(duty="Duty B", phone="02-2222-2000"),
        ).collect(source_id)
        contacts = list(session.scalars(select(ContactPoint).where(
            ContactPoint.agency_id == agency_id, ContactPoint.active.is_(True),
        )))
        assert {item.normalized_value for item in contacts} == {"0211111000", "0222222000"}
        assert _count(
            session, ChangeEvent,
            ChangeEvent.event_type == ChangeEventType.CONTACT_CHANGED,
        ) == 0


@pytest.mark.parametrize(
    ("first_html", "second_html"),
    (
        (
            b"<html><body><main><p>Business 02-6100-1000</p></main></body></html>",
            b"<html><body><main><p>Business 02-6100-2000</p></main></body></html>",
        ),
        (
            b"<html><body><footer>Representative 02-6200-1000</footer></body></html>",
            b"<html><body><footer>Representative 02-6200-2000</footer></body></html>",
        ),
    ),
)
def test_standalone_business_and_site_wide_changes_remain_review(
    changed_contact_db, first_html, second_html,
):
    _url, factory, root = changed_contact_db
    with factory() as session:
        agency_id, source_id = _agency_source(session, uuid.uuid4().hex)
        _collector(session, root, first_html).collect(source_id)
        contact = session.scalar(select(ContactPoint).where(ContactPoint.agency_id == agency_id))
        old = contact.normalized_value
        _collector(session, root, second_html).collect(source_id)
        session.expire_all()
        assert session.get(ContactPoint, contact.id).normalized_value == old
        assert any(item.contact_candidate_id is not None for item in _pending(session))


def test_unknown_changed_value_never_auto_updates(changed_contact_db):
    _url, factory, root = changed_contact_db
    with factory() as session:
        agency_id, source_id = _agency_source(session, "unknown")
        first = _collector(
            session, root, b"<html><body><aside>Unknown 02-7100-1000</aside></body></html>",
        ).collect(source_id)
        contact = ContactPoint(
            agency_id=agency_id, org_unit_id=None, duty_id=None,
            person_assignment_id=None, contact_type=ContactType.PHONE,
            value="02-7100-1000", normalized_value="0271001000", active=True,
            verified_at=first.observation.observed_at,
        )
        session.add(contact)
        session.flush()
        session.add(SourceOccurrence(
            observation_id=first.observation.id,
            entity_type=EntityType.CONTACT_POINT, entity_id=contact.id,
            observed_at=first.observation.observed_at,
        ))
        session.commit()
        _collector(
            session, root, b"<html><body><aside>Unknown 02-7100-2000</aside></body></html>",
        ).collect(source_id)
        session.expire_all()
        assert session.get(ContactPoint, contact.id).normalized_value == "0271001000"
        assert any(item.contact_candidate_id is not None for item in _pending(session))


def test_invalid_structured_value_never_auto_updates(changed_contact_db):
    _url, factory, root = changed_contact_db
    with factory() as session:
        agency_id, source_id = _agency_source(session, "invalid")
        _collector(
            session, root, _directory_html(phone="02-7200-1000"),
        ).collect(source_id)
        contact = session.scalar(select(ContactPoint).where(ContactPoint.agency_id == agency_id))
        _collector(
            session, root, _directory_html(phone="not-a-phone"),
        ).collect(source_id)
        session.expire_all()
        assert session.get(ContactPoint, contact.id).normalized_value == "0272001000"
        assert _count(
            session, DetectedChangeCandidate,
            DetectedChangeCandidate.review_status == ReviewStatus.APPROVED,
            DetectedChangeCandidate.resolution_note == AUTO_RESOLUTION_NOTE,
        ) == 0


def test_complete_snapshot_suppresses_duplicate_missing_and_is_idempotent(changed_contact_db):
    _url, factory, root = changed_contact_db
    with factory() as session:
        agency_id, source_id = _agency_source(session, "complete")
        source = session.get(Source, source_id)
        source.coverage_mode = SourceCoverageMode.COMPLETE_SNAPSHOT
        session.commit()
        _collector(
            session, root, _directory_html(phone="02-8100-1000"),
        ).collect(source_id)
        contact = session.scalar(select(ContactPoint).where(ContactPoint.agency_id == agency_id))
        changed = _collector(
            session, root, _directory_html(phone="02-8100-2000"),
        ).collect(source_id)
        extraction_id = session.scalar(select(ExtractionRun.id).where(
            ExtractionRun.observation_id == changed.observation.id,
            ExtractionRun.extractor_name == "staff_directory",
        ))
        before = (
            _count(session, DetectedChangeCandidate),
            _count(session, ContactHistory),
            _count(session, ChangeEvent, ChangeEvent.event_type == ChangeEventType.CONTACT_CHANGED),
            _count(session, SourceOccurrence),
        )
        assert _count(
            session, DetectedChangeCandidate,
            DetectedChangeCandidate.observation_id == changed.observation.id,
            DetectedChangeCandidate.proposed_event_type == ChangeEventType.CONTACT_MISSING,
        ) == 0
        _collector(
            session, root, _directory_html(phone="02-8100-2000"),
        )._detect_changes_best_effort(extraction_id)
        after_replay = (
            _count(session, DetectedChangeCandidate),
            _count(session, ContactHistory),
            _count(session, ChangeEvent, ChangeEvent.event_type == ChangeEventType.CONTACT_CHANGED),
            _count(session, SourceOccurrence),
        )
        assert after_replay == before
        assert _count(
            session, OperationClaim,
            OperationClaim.resource_key == (
                f"auto-change:{changed.observation.id}:{agency_id}"
            ),
            OperationClaim.status == "COMPLETED",
        ) == 1

        _collector(
            session, root, _directory_html(phone="02-8100-2000"),
        ).collect(source_id)
        session.expire_all()
        assert session.get(ContactPoint, contact.id).normalized_value == "0281002000"
        assert _count(session, ContactHistory) == before[1]
        assert _count(
            session, ChangeEvent, ChangeEvent.event_type == ChangeEventType.CONTACT_CHANGED,
        ) == before[2]


def test_stale_target_is_not_forced_and_candidate_remains_review(changed_contact_db, monkeypatch):
    _url, factory, root = changed_contact_db
    with factory() as session:
        agency_id, source_id = _agency_source(session, "stale")
        _collector(
            session, root, _directory_html(phone="02-9100-1000"),
        ).collect(source_id)
        original_auto_resolve = SourceChangeDetectionService.auto_resolve
        monkeypatch.setattr(
            SourceChangeDetectionService, "auto_resolve",
            lambda _self, detection_id: {
                "detection_id": str(detection_id), "resolved": 0, "failed": 0, "reused": False,
            },
        )
        changed = _collector(
            session, root, _directory_html(phone="02-9100-2000"),
        ).collect(source_id)
        candidate = session.scalar(select(DetectedChangeCandidate).where(
            DetectedChangeCandidate.observation_id == changed.observation.id,
            DetectedChangeCandidate.proposed_event_type == ChangeEventType.CONTACT_CHANGED,
        ))
        contact = session.get(ContactPoint, candidate.existing_entity_id)
        contact.value = "02-9100-3000"
        contact.normalized_value = "0291003000"
        session.commit()
        monkeypatch.setattr(SourceChangeDetectionService, "auto_resolve", original_auto_resolve)

        SourceChangeDetectionService(session).auto_resolve(candidate.detection_run_id)
        session.expire_all()
        assert session.get(ContactPoint, contact.id).normalized_value == "0291003000"
        assert session.get(DetectedChangeCandidate, candidate.id).review_status is ReviewStatus.PENDING_REVIEW


def test_manual_and_scheduled_jobs_share_auto_change_owner(changed_contact_db):
    _url, factory, root = changed_contact_db
    old_html = _directory_html(phone="02-9300-1000")
    new_html = _directory_html(phone="02-9300-2000")
    with factory() as session:
        manual_agency, manual_source = _agency_source(session, "manual")
        scheduled_agency, scheduled_source = _agency_source(session, "scheduled")
        _collector(session, root, old_html).collect(manual_source)
        _collector(session, root, old_html).collect(scheduled_source)
        jobs = CollectionJobService(session)
        jobs.create_manual(
            CollectionTriggerType.MANUAL_SOURCE,
            requested_by_user_id=None, source_id=manual_source,
        )
        jobs.create_from_sources(
            CollectionTriggerType.SCHEDULED_FULL,
            [session.get(Source, scheduled_source)],
            priority=SCHEDULED_FULL_PRIORITY,
            trigger_context={"test": "06c6f-shared-owner"},
            schedule_slot_key="06c6f-shared-owner",
        )

    worker = CollectionJobWorker(
        factory, lambda session: _collector(session, root, new_html),
    )
    assert len(worker.drain()) == 2
    with factory() as session:
        for agency_id in (manual_agency, scheduled_agency):
            contact = session.scalar(select(ContactPoint).where(
                ContactPoint.agency_id == agency_id,
            ))
            assert contact.normalized_value == "0293002000"
        assert _count(
            session, DetectedChangeCandidate,
            DetectedChangeCandidate.review_status == ReviewStatus.APPROVED,
            DetectedChangeCandidate.resolution_note == AUTO_RESOLUTION_NOTE,
        ) == 2


def test_review_filter_with_real_pending_candidate(changed_contact_db):
    database_url, factory, root = changed_contact_db
    with factory() as session:
        _agency_id, source_id = _agency_source(session, "review-filter")
        _collector(
            session, root, _directory_html(phone="02-9500-1000"),
        ).collect(source_id)
        _collector(
            session, root,
            _directory_phone_rows_html("02-9500-2000", "02-9500-3000"),
        ).collect(source_id)
        assert len(_pending(session)) == 2

    client = TestClient(regression_app(database_url))
    response = client.get(
        "/review?review_status=PENDING_REVIEW&change_type=CONTACT_CHANGED"
    )
    assert response.status_code == 200
    assert "02-9500-2000" in response.text
    assert "02-9500-3000" in response.text
    assert client.get("/review?review_status=&change_type=").status_code == 200
