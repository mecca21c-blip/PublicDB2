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
from app.main import create_app
from app.models import (
    AgencyType, CollectionMethod, CrawlRun, Observation,
    OrgUnitType, RunStatus, Source, StageStatus,
)
from app.models.common import utc_now
from app.services.agency_service import AgencyService
from app.services.collection_service import CollectionCoordinator, CollectionService
from app.services.source_service import SourceService


HTML = """<html><body><p>전화 02-1234-5678 contact@example.go.kr</p>
<table><tr><th>부서</th><th>업무</th><th>담당자</th></tr>
<tr><td>정보과</td><td>공공데이터</td><td>홍길동</td></tr></table></body></html>""".encode()
PUBLIC_DNS = lambda _host: {"93.184.216.34"}


@pytest.fixture()
def web_db(tmp_path, monkeypatch):
    path = tmp_path / "db" / "web.sqlite3"
    path.parent.mkdir()
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield url, factory, tmp_path
    engine.dispose()


def add_binding(session, *, agency_name, url="https://example.org/staff"):
    agency, _ = AgencyService(session).create_agency(
        official_name=agency_name, agency_type=AgencyType.OTHER
    )
    agency_id = uuid.UUID(agency["id"])
    unit = AgencyService(session).create_org_unit(
        agency_id=agency_id, name="정보과", unit_type=OrgUnitType.DEPARTMENT
    )
    binding, _ = SourceService(session).register_binding(
        url=url, agency_id=agency_id, org_unit_id=uuid.UUID(unit["id"])
    )
    return agency_id, uuid.UUID(binding["source_id"]), uuid.UUID(binding["binding_id"])


def install_mock_collection(app, root: Path, handler=None):
    coordinator = CollectionCoordinator()
    transport = httpx.MockTransport(handler or (
        lambda _request: httpx.Response(
            200, headers={"Content-Type": "text/html; charset=utf-8"}, content=HTML
        )
    ))
    app.state.collection_service_factory = lambda session: CollectionService(
        session,
        project_root=root,
        raw_root=root / "data" / "raw",
        coordinator=coordinator,
        fetcher=HTTPFetcher(transport=transport, resolver=PUBLIC_DNS),
    )


def test_sources_collect_action_calls_real_pipeline_and_runs_are_real(web_db):
    url, factory, root = web_db
    with factory() as session:
        _, source_id, _ = add_binding(session, agency_name="실기관")
    app = create_app(url, project_root=root)
    install_mock_collection(app, root)
    with TestClient(app) as client:
        source_page = client.get("/sources")
        assert f'data-collect-source="{source_id}"' in source_page.text
        response = client.post(f"/api/sources/{source_id}/collect")
        assert response.status_code == 200
        assert response.json()["run"]["status"] == "SUCCESS"
        updated_sources = client.get("/sources")
        runs = client.get("/runs")
    assert "정상" in updated_sources.text
    assert "실기관" in runs.text
    assert "RAW 상대 경로" in runs.text
    assert "확정 DB 반영" in runs.text
    assert "샘플 데이터" not in runs.text
    assert 'data-detail-id="run-a"' not in runs.text
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CrawlRun)) == 1


def test_runs_filters_through_all_bindings_and_shows_multi_context(web_db):
    url, factory, root = web_db
    with factory() as session:
        first_agency, source_id, _ = add_binding(session, agency_name="첫기관")
        second_agency, same_source, _ = add_binding(session, agency_name="둘기관")
        assert same_source == source_id
    app = create_app(url, project_root=root)
    install_mock_collection(app, root)
    with TestClient(app) as client:
        assert client.post(f"/api/sources/{source_id}/collect").status_code == 200
        first = client.get(f"/runs?agency_id={first_agency}")
        second = client.get(f"/runs?agency_id={second_agency}")
        failed_only = client.get("/runs?status=FAILED")
        searched = client.get("/runs?search=둘기관")
        future = client.get("/runs?date_from=2099-01-01")
    assert "외 1개 연결" in first.text
    assert "둘기관" in first.text
    assert "첫기관" in second.text
    assert "외 1개 연결" in searched.text
    assert "조건에 맞는 수집 이력이 없습니다" in failed_only.text
    assert "조건에 맞는 수집 이력이 없습니다" in future.text


def test_run_detail_never_exposes_absolute_artifact_path(web_db):
    url, factory, root = web_db
    with factory() as session:
        _, source_id, _ = add_binding(session, agency_name="기관")
    app = create_app(url, project_root=root)
    install_mock_collection(app, root)
    with TestClient(app) as client:
        client.post(f"/api/sources/{source_id}/collect")
    with factory() as session:
        observation = session.scalar(select(Observation))
        observation.artifact_path = r"C:\private\secret\response.html"
        session.commit()
    with TestClient(create_app(url, project_root=root)) as client:
        page = client.get("/runs")
    assert "C:\\private\\secret" not in page.text
    assert "02-1234-5678" not in page.text
    assert "contact@example.go.kr" not in page.text


def test_excluded_and_unsupported_collection_controls_are_disabled(web_db):
    url, factory, root = web_db
    with factory() as session:
        _, active_source, active_binding = add_binding(session, agency_name="활성")
        _, unsupported_source, _ = add_binding(
            session, agency_name="미지원", url="https://example.org/api"
        )
        session.get(Source, unsupported_source).collection_method = CollectionMethod.API
        SourceService(session).exclude_binding(active_binding, "사용 안 함")
        session.commit()
    with TestClient(create_app(url, project_root=root)) as client:
        page = client.get("/sources")
    assert f'data-collect-source="{active_source}" disabled title="제외된 연결입니다."' in page.text
    assert f'data-collect-source="{unsupported_source}" disabled title="지원하지 않는 수집 방식입니다."' in page.text


def test_api_busy_unknown_and_unsupported_are_truthful(web_db):
    url, factory, root = web_db
    with factory() as session:
        _, busy_source, _ = add_binding(session, agency_name="busy")
        _, unsupported_source, _ = add_binding(
            session, agency_name="unsupported", url="https://example.org/api"
        )
        session.get(Source, unsupported_source).collection_method = CollectionMethod.DOCUMENT
        session.add(CrawlRun(
            source_id=busy_source, status=RunStatus.RUNNING,
            connection_status=StageStatus.PENDING, raw_status=StageStatus.PENDING,
            extraction_status=StageStatus.PENDING, started_at=utc_now(),
        ))
        session.commit()
    app = create_app(url, project_root=root)
    install_mock_collection(app, root)
    with TestClient(app) as client:
        assert client.post(f"/api/sources/{busy_source}/collect").status_code == 409
        assert client.post(f"/api/sources/{unsupported_source}/collect").status_code == 409
        assert client.post(f"/api/sources/{uuid.uuid4()}/collect").status_code == 404


def test_registration_import_page_load_and_startup_never_collect(web_db):
    url, factory, root = web_db
    calls = 0
    def forbidden(_request):
        nonlocal calls
        calls += 1
        raise AssertionError("automatic network call")
    app = create_app(url, project_root=root)
    install_mock_collection(app, root, forbidden)
    with TestClient(app) as client:
        agency = client.post("/api/agencies", json={"official_name": "수동기관", "agency_type": "OTHER"})
        agency_id = agency.json()["item"]["id"]
        assert client.post(
            "/api/source-bindings",
            json={"agency_id": agency_id, "url": "https://example.org/manual"},
        ).status_code == 201
        for route in ("/", "/agencies", "/sources", "/runs", "/contacts", "/review", "/settings"):
            assert client.get(route).status_code == 200
    assert calls == 0


def test_source_state_is_shared_but_exclusion_overrides(web_db):
    url, factory, root = web_db
    with factory() as session:
        _, source_id, first_binding = add_binding(session, agency_name="첫기관")
        _, _, second_binding = add_binding(session, agency_name="둘기관")
        SourceService(session).exclude_binding(second_binding, "제외")
    app = create_app(url, project_root=root)
    install_mock_collection(app, root)
    with TestClient(app) as client:
        client.post(f"/api/sources/{source_id}/collect")
    with factory() as session:
        service = SourceService(session)
        assert service.get_binding(first_binding)["status"] == "정상"
        assert service.get_binding(second_binding)["status"] == "제외"
