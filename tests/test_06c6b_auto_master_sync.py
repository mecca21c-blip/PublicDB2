from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.collectors.http_fetcher import HTTPFetcher
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    AgencyType, CollectionJob, CollectionJobStatus, CollectionTriggerType,
    ContactHistory, ContactPoint, Duty,
    EntityType, OrgUnit, OrgUnitType, Person, PersonAssignment, ReviewStatus, Source,
    SourceOccurrence, DetectedChangeCandidate, ChangeEventType,
)
from app.services.agency_service import AgencyService
from app.services.collection_job_service import CollectionJobService, SCHEDULED_FULL_PRIORITY
from app.services.collection_job_worker import CollectionJobWorker, WorkerResult
from app.services.collection_service import CollectionCoordinator, CollectionService
from app.services.master_promotion_apply_service import MasterPromotionApplyService
from app.services.semantic_discovery_service import SemanticDiscoveryProjector
from app.services.source_service import SourceService


PUBLIC_DNS = lambda _host: {"93.184.216.34"}


@pytest.fixture()
def auto_sync_db(tmp_path, monkeypatch):
    path = tmp_path / "auto-sync.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield factory, tmp_path
    engine.dispose()


def _source(session, suffix: str):
    agency, _created = AgencyService(session).create_agency(
        official_name=f"Auto Sync Agency {suffix}", agency_type=AgencyType.OTHER,
    )
    agency_id = uuid.UUID(agency["id"])
    binding, _created = SourceService(session).register_binding(
        url=f"https://example.org/{suffix}", agency_id=agency_id,
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


def _gangseo_shape_html() -> bytes:
    rows = "".join(
        f"<tr><td>Role {index}</td><td>Duty {index}</td><td>02-2600-{1000 + index:04d}</td></tr>"
        for index in range(22)
    )
    business = "".join(
        f"<p>Business phone 02-7000-{1000 + index:04d}</p>" for index in range(2)
    )
    site_wide = "".join(
        f"<p>Representative phone 02-8000-{1000 + index:04d}</p>" for index in range(5)
    )
    return (
        "<html><body>"
        f"<main>{business}<table><thead><tr>"
        "<th>\uc9c1\uc704</th><th>\uc5c5\ubb34</th><th>\uc804\ud654</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></main>"
        f"<footer>{site_wide}</footer>"
        "</body></html>"
    ).encode("utf-8")


def _one_directory_html(phone: str) -> bytes:
    return (
        "<html><body><main><table><thead><tr>"
        "<th>\uc9c1\uc704</th><th>\uc5c5\ubb34</th><th>\uc804\ud654</th>"
        "</tr></thead><tbody>"
        f"<tr><td>Lead</td><td>Public health</td><td>{phone}</td></tr>"
        "</tbody></table></main></body></html>"
    ).encode("utf-8")


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_real_gangseo_shape_auto_sync_and_same_data_recollection(auto_sync_db):
    factory, root = auto_sync_db
    html = _gangseo_shape_html()
    with factory() as session:
        agency_id, source_id = _source(session, "gangseo")
        first = _collector(session, root, html).collect(source_id)
        first_summary = SemanticDiscoveryProjector(session).summary(first.crawl_run.id)
        first_verified = {
            item.id: item.verified_at for item in session.scalars(
                select(ContactPoint).where(ContactPoint.agency_id == agency_id)
            )
        }
        first_occurrences = _count(session, SourceOccurrence)

        assert first_summary["raw_evidence_count"] == 51
        assert first_summary["semantic_discovery_count"] == 29
        assert first_summary["directory_records"] == 22
        assert first_summary["business_contacts"] == 2
        assert first_summary["site_wide_contacts"] == 5
        assert first_summary["unknown_contacts"] == 0
        assert _count(session, ContactPoint) == 29
        assert _count(session, Duty) == 22
        assert _count(session, OrgUnit) == 0
        assert _count(session, Person) == 0
        assert _count(session, PersonAssignment) == 0
        assert _count(session, ContactHistory) == 29
        assert first_occurrences == 51
        assert _count(session, DetectedChangeCandidate) == 0

        replay = MasterPromotionApplyService(session).sync_observation(first.observation.id)
        assert replay["reused"]
        assert replay["contacts_created"] == 0
        assert _count(session, ContactPoint) == 29
        assert _count(session, SourceOccurrence) == first_occurrences

        second = _collector(session, root, html).collect(source_id)
        assert SemanticDiscoveryProjector(session).summary(second.crawl_run.id)["semantic_discovery_count"] == 29
        assert _count(session, ContactPoint) == 29
        assert _count(session, Duty) == 22
        assert _count(session, OrgUnit) == 0
        assert _count(session, ContactHistory) == 29
        assert _count(session, SourceOccurrence) == first_occurrences * 2
        assert all(
            item.verified_at > first_verified[item.id]
            for item in session.scalars(select(ContactPoint).where(ContactPoint.agency_id == agency_id))
        )


def test_strict_changed_directory_contact_auto_updates_through_review_writer(auto_sync_db):
    factory, root = auto_sync_db
    with factory() as session:
        agency_id, source_id = _source(session, "changed")
        _collector(session, root, _one_directory_html("02-1111-2222")).collect(source_id)
        original = session.scalar(select(ContactPoint).where(ContactPoint.agency_id == agency_id))
        original_id = original.id
        _collector(session, root, _one_directory_html("02-3333-4444")).collect(source_id)

        session.expire_all()
        current = session.get(ContactPoint, original_id)
        changed = session.scalar(select(DetectedChangeCandidate).where(
            DetectedChangeCandidate.proposed_event_type == ChangeEventType.CONTACT_CHANGED,
            DetectedChangeCandidate.review_status == ReviewStatus.APPROVED,
        ))
        assert _count(session, ContactPoint) == 1
        assert current.normalized_value == "0233334444"
        assert _count(session, ContactHistory) == 2
        assert changed is not None and changed.existing_entity_id == original_id
        assert changed.resolution_note == "AUTO_SAFE_STRUCTURED_CONTACT_CHANGE"


def test_unknown_standalone_is_discovery_only(auto_sync_db):
    factory, root = auto_sync_db
    html = b"<html><body><aside>Unknown contact 02-4444-5555</aside></body></html>"
    with factory() as session:
        _agency_id, source_id = _source(session, "unknown")
        result = _collector(session, root, html).collect(source_id)
        summary = SemanticDiscoveryProjector(session).summary(result.crawl_run.id)
        assert summary["unknown_contacts"] == 1
        assert _count(session, ContactPoint) == 0


def test_business_binding_org_and_site_wide_agency_context_are_separate(auto_sync_db):
    factory, root = auto_sync_db
    html = (
        "<html><body><main><p>Business 02-6100-1000</p></main>"
        "<footer><p>Representative 02-6200-2000</p></footer></body></html>"
    ).encode("utf-8")
    with factory() as session:
        agency, _created = AgencyService(session).create_agency(
            official_name="Scoped Agency", agency_type=AgencyType.OTHER,
        )
        agency_id = uuid.UUID(agency["id"])
        unit = AgencyService(session).create_org_unit(
            agency_id=agency_id, name="Public Health", unit_type=OrgUnitType.DEPARTMENT,
        )
        unit_id = uuid.UUID(unit["id"])
        binding, _created = SourceService(session).register_binding(
            url="https://example.org/scoped", agency_id=agency_id,
            org_unit_id=unit_id,
        )
        _collector(session, root, html).collect(uuid.UUID(binding["source_id"]))
        business = session.scalar(select(ContactPoint).where(
            ContactPoint.normalized_value == "0261001000",
        ))
        site_wide = session.scalar(select(ContactPoint).where(
            ContactPoint.normalized_value == "0262002000",
        ))
        assert business.org_unit_id == unit_id and business.duty_id is None
        assert site_wide.org_unit_id is None and site_wide.duty_id is None


def test_seoul_one_contact_syncs_and_busan_zero_writes_nothing(auto_sync_db):
    factory, root = auto_sync_db
    with factory() as session:
        seoul_agency, seoul_source = _source(session, "seoul")
        busan_agency, busan_source = _source(session, "busan")
        seoul = _collector(
            session, root,
            b"<html><body><footer>Representative 02-6114-1000</footer></body></html>",
        ).collect(seoul_source)
        busan = _collector(
            session, root,
            b"<html><body><p>No published contact data.</p></body></html>",
        ).collect(busan_source)
        assert SemanticDiscoveryProjector(session).summary(seoul.crawl_run.id)["semantic_discovery_count"] == 1
        assert SemanticDiscoveryProjector(session).summary(busan.crawl_run.id)["semantic_discovery_count"] == 0
        assert session.scalar(select(func.count()).select_from(ContactPoint).where(
            ContactPoint.agency_id == seoul_agency,
        )) == 1
        assert session.scalar(select(func.count()).select_from(ContactPoint).where(
            ContactPoint.agency_id == busan_agency,
        )) == 0


def test_manual_and_scheduled_jobs_share_collection_master_sync(auto_sync_db):
    factory, root = auto_sync_db
    with factory() as session:
        manual_agency, manual_source = _source(session, "manual")
        scheduled_agency, scheduled_source = _source(session, "scheduled")
        jobs = CollectionJobService(session)
        jobs.create_manual(
            CollectionTriggerType.MANUAL_SOURCE,
            requested_by_user_id=None, source_id=manual_source,
        )
        jobs.create_from_sources(
            CollectionTriggerType.SCHEDULED_FULL,
            [session.get(Source, scheduled_source)],
            priority=SCHEDULED_FULL_PRIORITY,
            trigger_context={"test": "shared-sync-owner"},
            schedule_slot_key="06c6b-shared-owner",
        )

    html = _one_directory_html("02-5555-6666")
    worker = CollectionJobWorker(
        factory, lambda session: _collector(session, root, html),
    )
    assert worker.run_one() is WorkerResult.ITEM_COMPLETE
    assert worker.run_one() is WorkerResult.ITEM_COMPLETE
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ContactPoint).where(
            ContactPoint.agency_id == manual_agency,
        )) == 1
        assert session.scalar(select(func.count()).select_from(ContactPoint).where(
            ContactPoint.agency_id == scheduled_agency,
        )) == 1
        assert _count(session, Person) == 0
        assert _count(session, PersonAssignment) == 0


def test_master_sync_failure_isolated_from_multi_source_job(auto_sync_db, monkeypatch):
    factory, root = auto_sync_db
    with factory() as session:
        _source(session, "failure-one")
        _source(session, "failure-two")
        job_id = CollectionJobService(session).create_manual(
            CollectionTriggerType.MANUAL_ALL, requested_by_user_id=None,
        ).id

    def fail_sync(*_args, **_kwargs):
        raise RuntimeError("isolated master sync failure")

    monkeypatch.setattr(MasterPromotionApplyService, "sync_observation", fail_sync)
    worker = CollectionJobWorker(
        factory, lambda session: _collector(
            session, root, _one_directory_html("02-7777-8888"),
        ),
    )
    assert len(worker.drain()) == 2
    with factory() as session:
        job = session.get(CollectionJob, job_id)
        assert job.status is CollectionJobStatus.COMPLETED
        assert job.succeeded_items == 2 and job.failed_items == 0
        assert _count(session, ContactPoint) == 0
