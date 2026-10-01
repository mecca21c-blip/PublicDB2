from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    Agency, AgencyType, CollectionJob, CollectionJobItem, CollectionJobItemStatus,
    CollectionJobStatus, CollectionTriggerType, RefreshRecurrence, RunStatus,
    Source, SourceBinding,
)
from app.services.collection_job_service import (
    CollectionJobService, SCHEDULED_FULL_PRIORITY,
)
from app.services.collection_job_worker import (
    CollectionJobWorker, WorkerResult, recover_interrupted_jobs,
)
from app.services.collection_scheduler import (
    CollectionScheduler, SEOUL, latest_schedule_slot, next_schedule_slot,
)
from app.services.settings_service import SettingsService, SettingsSnapshot
from tests.support import regression_app


@pytest.fixture()
def jobs_db(tmp_path, monkeypatch):
    path = tmp_path / "jobs.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield url, factory, tmp_path
    engine.dispose()


def seed_sources(factory, count: int, *, duplicate_binding: bool = False):
    with factory() as session:
        agency = Agency(
            official_name="테스트 기관", normalized_name="테스트기관",
            agency_type=AgencyType.OTHER, region_code="SEOUL",
        )
        other = Agency(
            official_name="다른 기관", normalized_name="다른기관",
            agency_type=AgencyType.OTHER, region_code="BUSAN",
        )
        session.add_all([agency, other])
        session.flush()
        sources = []
        for index in range(count):
            source = Source(
                url=f"https://example.org/{index}",
                normalized_url=f"https://example.org/{index}",
            )
            session.add(source)
            session.flush()
            session.add(SourceBinding(
                source_id=source.id, agency_id=agency.id,
                scope_key=f"agency:{agency.id}", active=True,
            ))
            if duplicate_binding and index == 0:
                session.add(SourceBinding(
                    source_id=source.id, agency_id=other.id,
                    scope_key=f"agency:{other.id}", active=True,
                ))
            sources.append(source.id)
        session.commit()
        return sources, agency.id


def fake_result(source_id, status=RunStatus.SUCCESS, error=None):
    return SimpleNamespace(crawl_run=SimpleNamespace(
        id=None, source_id=source_id, status=status, error_summary=error,
    ))


def create_all_job(factory):
    with factory() as session:
        return CollectionJobService(session).create_manual(
            CollectionTriggerType.MANUAL_ALL, requested_by_user_id=None
        ).id


def test_source_failures_and_consecutive_runtime_error_continue(jobs_db):
    _url, factory, _root = jobs_db
    source_ids, _agency = seed_sources(factory, 10)
    job_id = create_all_job(factory)
    attempted = []
    failure_indexes = {1: "HTTP_ERROR", 4: "EXTRACTION_ERROR"}

    class FakeService:
        def collect(self, source_id):
            index = source_ids.index(source_id)
            attempted.append(index)
            if index in failure_indexes:
                return fake_result(source_id, RunStatus.FAILED, failure_indexes[index])
            if index == 6:
                raise RuntimeError("unexpected source failure")
            return fake_result(source_id)

    results = CollectionJobWorker(factory, lambda _session: FakeService()).drain()
    assert len(results) == 10
    assert attempted == list(range(10))
    with factory() as session:
        job = session.get(CollectionJob, job_id)
        assert job.status is CollectionJobStatus.COMPLETED_WITH_ERRORS
        assert (job.succeeded_items, job.failed_items) == (7, 3)

    second_job = create_all_job(factory)
    attempted.clear()

    class ConsecutiveFailureService:
        def collect(self, source_id):
            index = source_ids.index(source_id)
            attempted.append(index)
            if index in {2, 3, 4}:
                raise RuntimeError(f"failure {index}")
            return fake_result(source_id)

    CollectionJobWorker(factory, lambda _session: ConsecutiveFailureService()).drain()
    assert attempted == list(range(10))
    with factory() as session:
        assert session.get(CollectionJob, second_job).failed_items == 3


def test_system_failure_stops_and_poisoned_session_is_not_reused(jobs_db):
    _url, factory, _root = jobs_db
    source_ids, _agency = seed_sources(factory, 3)
    job_id = create_all_job(factory)

    class BrokenDatabaseService:
        def collect(self, _source_id):
            raise SQLAlchemyError("database unavailable")

    result = CollectionJobWorker(factory, lambda _session: BrokenDatabaseService()).run_one()
    assert result is WorkerResult.SYSTEM_FAILURE
    with factory() as session:
        job = session.get(CollectionJob, job_id)
        statuses = [item.status for item in job.items]
        assert statuses.count(CollectionJobItemStatus.RUNNING) == 1
        assert statuses.count(CollectionJobItemStatus.PENDING) == 2

    with factory() as session:
        recover_interrupted_jobs(session)
        old_job = session.get(CollectionJob, job_id)
        old_job.status = CollectionJobStatus.CANCELLED
        for item in old_job.items:
            if item.status is CollectionJobItemStatus.PENDING:
                item.status = CollectionJobItemStatus.SKIPPED
        session.commit()
    poison_job = create_all_job(factory)
    sessions = []
    calls = 0

    class PoisonOnce:
        def __init__(self, session):
            self.session = session

        def collect(self, source_id):
            nonlocal calls
            sessions.append(self.session)
            calls += 1
            if calls == 1:
                self.session.add(Source(url="duplicate", normalized_url="https://example.org/0"))
                try:
                    self.session.flush()
                except IntegrityError:
                    raise RuntimeError("source-local poisoned transaction")
            return fake_result(source_id)

    worker = CollectionJobWorker(factory, lambda session: PoisonOnce(session))
    worker.drain()
    assert len(sessions) == 3
    assert len({id(value) for value in sessions}) == 3
    with factory() as session:
        job = session.get(CollectionJob, poison_job)
        assert (job.succeeded_items, job.failed_items) == (2, 1)


def test_restart_resumes_remainder_without_repeating_terminal_items(jobs_db):
    _url, factory, _root = jobs_db
    source_ids, _agency = seed_sources(factory, 10)
    job_id = create_all_job(factory)
    with factory() as session:
        job = session.get(CollectionJob, job_id)
        job.status = CollectionJobStatus.RUNNING
        for item in job.items[:4]:
            item.status = CollectionJobItemStatus.SUCCESS
        job.items[4].status = CollectionJobItemStatus.RUNNING
        session.commit()
    with factory() as session:
        assert recover_interrupted_jobs(session) == 1
    attempted = []

    class FakeService:
        def collect(self, source_id):
            attempted.append(source_ids.index(source_id) + 1)
            return fake_result(source_id)

    CollectionJobWorker(factory, lambda _session: FakeService()).drain()
    assert attempted == [6, 7, 8, 9, 10]
    with factory() as session:
        job = session.get(CollectionJob, job_id)
        assert (job.succeeded_items, job.failed_items) == (9, 1)
        assert job.status is CollectionJobStatus.COMPLETED_WITH_ERRORS
        assert job.items[4].error_code == "INTERRUPTED_BY_RESTART"


def test_failure_recording_error_stops_and_process_lock_prevents_parallel_collection(jobs_db, monkeypatch):
    _url, factory, _root = jobs_db
    source_ids, _agency = seed_sources(factory, 2)
    job_id = create_all_job(factory)

    class SuccessService:
        def collect(self, source_id):
            return fake_result(source_id)

    broken_worker = CollectionJobWorker(factory, lambda _session: SuccessService())
    monkeypatch.setattr(
        broken_worker, "_finish_item",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SQLAlchemyError("result write failed")),
    )
    assert broken_worker.run_one() is WorkerResult.SYSTEM_FAILURE
    with factory() as session:
        job = session.get(CollectionJob, job_id)
        assert [item.status for item in job.items] == [
            CollectionJobItemStatus.RUNNING, CollectionJobItemStatus.PENDING,
        ]
        recover_interrupted_jobs(session)
        job.status = CollectionJobStatus.CANCELLED
        job.items[1].status = CollectionJobItemStatus.SKIPPED
        session.commit()

    parallel_job = create_all_job(factory)
    entered = threading.Event()
    release = threading.Event()
    active = 0
    maximum = 0
    guard = threading.Lock()

    class BlockingService:
        def collect(self, source_id):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            entered.set()
            release.wait(2)
            with guard:
                active -= 1
            return fake_result(source_id)

    first = CollectionJobWorker(factory, lambda _session: BlockingService())
    second = CollectionJobWorker(factory, lambda _session: BlockingService())
    thread = threading.Thread(target=first.run_one)
    thread.start()
    assert entered.wait(2)
    assert second.run_one() is WorkerResult.IDLE
    release.set()
    thread.join(2)
    assert maximum == 1


def test_canonical_dedup_and_manual_priority_at_item_boundary(jobs_db):
    _url, factory, _root = jobs_db
    source_ids, _agency = seed_sources(factory, 3, duplicate_binding=True)
    with factory() as session:
        dedup = CollectionJobService(session).create_manual(
            CollectionTriggerType.MANUAL_ALL, requested_by_user_id=None
        )
        assert dedup.total_items == 3
        dedup.status = CollectionJobStatus.CANCELLED
        for item in dedup.items:
            item.status = CollectionJobItemStatus.SKIPPED
        session.commit()
        service = CollectionJobService(session)
        scheduled = service.create_from_sources(
            CollectionTriggerType.SCHEDULED_FULL, service.scheduled_sources(),
            priority=SCHEDULED_FULL_PRIORITY,
            trigger_context={"slot": "2026-01-01T02:00:00+09:00"},
            schedule_slot_key="priority-test",
        )
    attempted = []

    class FakeService:
        def collect(self, source_id):
            attempted.append(source_id)
            return fake_result(source_id)

    worker = CollectionJobWorker(factory, lambda _session: FakeService())
    assert worker.run_one() is WorkerResult.ITEM_COMPLETE
    with factory() as session:
        CollectionJobService(session).create_manual(
            CollectionTriggerType.MANUAL_SOURCE,
            requested_by_user_id=None, source_id=source_ids[2],
        )
    assert worker.run_one() is WorkerResult.ITEM_COMPLETE
    assert attempted[1] == source_ids[2]


def test_schedule_calculation_duplicate_catchup_retry_and_off_switch(jobs_db):
    _url, factory, _root = jobs_db
    seed_sources(factory, 1)
    saturday_defaults = SettingsSnapshot(1, 100, "agent")
    assert saturday_defaults.refresh_weekday == 5
    daily = SettingsSnapshot(1, 100, "agent", True, RefreshRecurrence.DAILY, None, None, "02:00", True)
    weekly = SettingsSnapshot(1, 100, "agent", True, RefreshRecurrence.WEEKLY, 5, None, "02:00", True)
    monthly = SettingsSnapshot(1, 100, "agent", True, RefreshRecurrence.MONTHLY, None, 31, "02:00", True)
    now = datetime(2026, 10, 1, 12, 0, tzinfo=SEOUL)
    assert next_schedule_slot(daily, now) == datetime(2026, 10, 2, 2, 0, tzinfo=SEOUL)
    assert next_schedule_slot(weekly, now).weekday() == 5
    assert latest_schedule_slot(monthly, datetime(2026, 4, 30, 3, 0, tzinfo=SEOUL)).day == 30

    scheduler = CollectionScheduler(factory)
    assert scheduler.tick(now) == []
    with factory() as session:
        SettingsService(session).update(
            http_timeout_seconds=10, max_response_bytes=1024 * 1024, user_agent="test-agent",
            automatic_refresh_enabled=True, refresh_recurrence=RefreshRecurrence.DAILY,
            refresh_time_of_day="02:00", retry_failed_next_day=True,
        )
    first = scheduler.tick(now)
    assert len(first) == 1
    assert CollectionScheduler(factory).tick(now) == []
    with factory() as session:
        jobs = list(session.scalars(select(CollectionJob).where(
            CollectionJob.trigger_type == CollectionTriggerType.SCHEDULED_FULL
        )))
        assert len(jobs) == 1
        assert jobs[0].trigger_context["slot"] == "2026-10-01T02:00:00+09:00"
        jobs[0].status = CollectionJobStatus.COMPLETED_WITH_ERRORS
        jobs[0].failed_items = 1
        jobs[0].items[0].status = CollectionJobItemStatus.FAILED
        session.commit()
    scheduler.tick(datetime(2026, 10, 2, 3, 0, tzinfo=SEOUL))
    with factory() as session:
        retry_count = session.scalar(select(func.count()).select_from(CollectionJob).where(
            CollectionJob.trigger_type == CollectionTriggerType.SCHEDULED_RETRY
        ))
        assert retry_count == 1


def test_api_is_nonblocking_and_ui_exposes_progress(jobs_db):
    url, factory, root = jobs_db
    source_ids, _agency = seed_sources(factory, 1)
    release = threading.Event()
    finished = threading.Event()

    class BlockingService:
        def collect(self, source_id):
            release.wait(2)
            finished.set()
            return fake_result(source_id)

    app = regression_app(url, project_root=root)
    app.state.collection_service_factory = lambda _session: BlockingService()
    with TestClient(app) as client:
        response = client.post(f"/api/sources/{source_ids[0]}/collect")
        assert response.status_code == 202
        job = response.json()["job"]
        assert job["total_items"] == 1
        source_page = client.get("/sources")
        runs_page = client.get("/runs")
        assert "선택 소스 지금 수집" in source_page.text
        assert "수집 작업" in runs_page.text
        release.set()
        assert finished.wait(2)
        completed = client.get(f"/api/collection-jobs/{job['id']}").json()["job"]
        assert completed["status"] in {"RUNNING", "COMPLETED"}
