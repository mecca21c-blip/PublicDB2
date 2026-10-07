from __future__ import annotations

import socket
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.collectors.http_fetcher import (
    DNSResolutionFailure,
    HTTPFetcher,
    HTTPStatusFailure,
    RemoteConnectionFailure,
    RequestTimeout,
    ResponseTooLarge,
    TLSConnectionFailure,
    UnsafeRequestTarget,
    safe_http_error_summary,
)
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    Agency, AgencyType, CollectionJob, CollectionJobItemStatus, CollectionJobStatus,
    CollectionTriggerType, OrgUnit, OrgUnitType, RunStatus, Source, SourceBinding,
)
from app.models.common import utc_now
from app.services.collection_job_service import CollectionJobService, SCHEDULED_FULL_PRIORITY
from app.services.collection_job_worker import CollectionJobWorker
from app.services.collection_service import CollectionCoordinator, CollectionService
from tests.support import regression_app


PUBLIC_DNS = lambda _host: ["93.184.216.34"]


@pytest.fixture()
def runtime_db(tmp_path, monkeypatch):
    path = tmp_path / "runtime.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield url, factory, tmp_path
    engine.dispose()


def _seed_job(factory, count: int = 4):
    with factory() as session:
        agency = Agency(
            official_name="강서구청", normalized_name="강서구청",
            agency_type=AgencyType.BASIC_LOCAL_GOVERNMENT, region_code="SEOUL",
        )
        session.add(agency)
        session.flush()
        unit = OrgUnit(
            agency_id=agency.id, name="위생관리과", normalized_name="위생관리과",
            unit_type=OrgUnitType.DEPARTMENT,
        )
        session.add(unit)
        session.flush()
        sources = []
        for index in range(count):
            source = Source(
                url=f"https://example.org/staff/{index}?secret=not-for-ui",
                normalized_url=f"https://example.org/staff/{index}?secret=not-for-ui",
                title=f"직원안내 {index + 1}",
            )
            session.add(source)
            session.flush()
            session.add(SourceBinding(
                source_id=source.id, agency_id=agency.id, org_unit_id=unit.id,
                scope_key=f"org_unit:{unit.id}", active=True,
            ))
            sources.append(source)
        session.commit()
        job = CollectionJobService(session).create_manual(
            CollectionTriggerType.MANUAL_SELECTION,
            requested_by_user_id=None,
            source_ids=[source.id for source in sources],
        )
        job.status = CollectionJobStatus.RUNNING
        job.started_at = utc_now()
        if count > 1:
            job.items[0].status = CollectionJobItemStatus.SUCCESS
            job.succeeded_items = 1
        job.items[min(1, count - 1)].status = CollectionJobItemStatus.RUNNING
        session.commit()
        return job.id, sources[min(1, count - 1)].id


def test_active_projection_is_bounded_current_and_multiple_job_aware(runtime_db):
    _url, factory, _root = runtime_db
    job_id, current_source_id = _seed_job(factory)
    with factory() as session:
        service = CollectionJobService(session)
        scheduled = service.create_from_sources(
            CollectionTriggerType.SCHEDULED_FULL,
            service.scheduled_sources(),
            priority=SCHEDULED_FULL_PRIORITY,
            trigger_context={"slot": "test"},
            schedule_slot_key="visibility-test",
        )
        snapshot = service.active_snapshot()
        assert snapshot["active_job_count"] == 2
        assert snapshot["job"]["id"] == str(job_id)
        assert snapshot["job"]["completed_items"] == 1
        assert snapshot["job"]["succeeded_items"] == 1
        assert snapshot["job"]["failed_items"] == 0
        assert "items" not in snapshot["job"]
        current = snapshot["job"]["current_item"]
        assert current["source_id"] == str(current_source_id)
        assert current["agency"] == "강서구청"
        assert current["org_unit"] == "위생관리과"
        assert current["status"] == "RUNNING"
        assert "secret=" not in current["url"]
        scheduled.status = CollectionJobStatus.CANCELLED
        session.commit()


def test_authenticated_pages_and_api_expose_shared_runtime_status(runtime_db):
    url, factory, root = runtime_db
    job_id, _source_id = _seed_job(factory)
    app = regression_app(url, project_root=root)
    with TestClient(app) as client:
        snapshot = client.get("/api/collection-jobs/active")
        assert snapshot.status_code == 200
        assert snapshot.json()["job"]["id"] == str(job_id)
        for route in ("/", "/agencies", "/sources", "/runs", "/contacts", "/review", "/settings"):
            assert 'data-global-job-status' in client.get(route).text
        sources = client.get("/sources").text
        assert 'data-job-status-title' in sources
        assert 'data-source-live-status' in sources


def test_recent_terminal_feedback_and_no_stale_active_state(runtime_db):
    _url, factory, _root = runtime_db
    job_id, _source_id = _seed_job(factory)
    with factory() as session:
        job = CollectionJobService(session).get(job_id)
        for item in job.items:
            item.status = CollectionJobItemStatus.SUCCESS
        job.succeeded_items = job.total_items
        job.status = CollectionJobStatus.COMPLETED
        job.finished_at = utc_now()
        session.commit()
        snapshot = CollectionJobService(session).active_snapshot()
        assert snapshot["active_job_count"] == 0
        assert snapshot["recent_terminal"] is True
        assert snapshot["job"]["status"] == "COMPLETED"
        assert snapshot["job"]["current_item"] is None


def test_collection_persists_safe_http_reason_and_code(runtime_db):
    _url, factory, root = runtime_db
    _job_id, source_id = _seed_job(factory, count=1)

    def timeout(request):
        raise httpx.ReadTimeout("secret low-level text", request=request)

    with factory() as session:
        result = CollectionService(
            session,
            project_root=root,
            raw_root=root / "raw",
            coordinator=CollectionCoordinator(),
            fetcher=HTTPFetcher(transport=httpx.MockTransport(timeout), resolver=PUBLIC_DNS),
        ).collect(source_id)
        assert result.crawl_run.error_summary == "연결 시간 초과"
        assert result.crawl_run.collection_statistics["error_code"] == "HTTP_TIMEOUT"
        assert "secret" not in result.crawl_run.error_summary


def test_http_item_failure_keeps_queue_running_and_preserves_safe_code(runtime_db):
    _url, factory, _root = runtime_db
    job_id, _source_id = _seed_job(factory)
    with factory() as session:
        job = session.get(CollectionJob, job_id)
        job.status = CollectionJobStatus.PENDING
        job.started_at = None
        job.succeeded_items = 0
        for item in job.items:
            item.status = CollectionJobItemStatus.PENDING
        session.commit()
    calls = 0

    class FakeCollection:
        def collect(self, source_id):
            nonlocal calls
            calls += 1
            failed = calls == 1
            return SimpleNamespace(crawl_run=SimpleNamespace(
                id=None,
                status=RunStatus.FAILED if failed else RunStatus.SUCCESS,
                error_summary="연결 시간 초과" if failed else None,
                collection_statistics={"error_code": "HTTP_TIMEOUT"} if failed else {},
            ))

    CollectionJobWorker(factory, lambda _session: FakeCollection()).drain()
    with factory() as session:
        job = session.get(CollectionJob, job_id)
        assert calls == 4
        assert job.status is CollectionJobStatus.COMPLETED_WITH_ERRORS
        assert (job.succeeded_items, job.failed_items) == (3, 1)
        failed = next(item for item in job.items if item.status is CollectionJobItemStatus.FAILED)
        assert failed.error_code == "HTTP_TIMEOUT"
        assert failed.error_summary == "연결 시간 초과"


@pytest.mark.parametrize(
    ("raised", "expected_type", "summary"),
    [
        (httpx.ReadTimeout("slow"), RequestTimeout, "연결 시간 초과"),
        (httpx.ConnectError("connection refused"), RemoteConnectionFailure, "원격 서버 연결 실패"),
        (httpx.ConnectError("SSL certificate verify failed"), TLSConnectionFailure, "TLS 연결 실패"),
    ],
)
def test_http_transport_errors_have_safe_categories(raised, expected_type, summary):
    def handler(request):
        raised.request = request
        raise raised

    with pytest.raises(expected_type) as caught:
        HTTPFetcher(transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS).fetch("https://example.org/a?api_key=secret")
    assert safe_http_error_summary(caught.value) == summary
    assert "secret" not in safe_http_error_summary(caught.value)


def test_http_status_safety_dns_and_size_categories():
    status_fetcher = HTTPFetcher(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)), resolver=PUBLIC_DNS,
    )
    with pytest.raises(HTTPStatusFailure) as status_error:
        status_fetcher.fetch("https://example.org")
    assert safe_http_error_summary(status_error.value) == "HTTP 503"

    with pytest.raises(UnsafeRequestTarget) as unsafe_error:
        HTTPFetcher(resolver=lambda _host: ["127.0.0.1"]).fetch("https://internal.example")
    assert safe_http_error_summary(unsafe_error.value) == "안전하지 않은 주소 차단"

    with pytest.raises(DNSResolutionFailure) as dns_error:
        HTTPFetcher(resolver=lambda _host: (_ for _ in ()).throw(socket.gaierror())).fetch("https://missing.example")
    assert safe_http_error_summary(dns_error.value) == "DNS 확인 실패"

    large_fetcher = HTTPFetcher(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"12345")),
        resolver=PUBLIC_DNS, max_response_bytes=4,
    )
    with pytest.raises(ResponseTooLarge) as large_error:
        large_fetcher.fetch("https://example.org")
    assert safe_http_error_summary(large_error.value) == "응답 크기 제한 초과"
