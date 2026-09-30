from __future__ import annotations

import socket
import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from tests.support import regression_app
from app.models import AgencyType, Duty, OrgUnitType, Source, SourceBinding
from app.services.agency_service import AgencyService, AgencyServiceError
from app.services.normalization import SourceURLValidationError, normalize_source_url
from app.services.source_service import SourceService


@pytest.fixture()
def database(tmp_path, monkeypatch):
    path = tmp_path / "publicdb2.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    yield url
    command.downgrade(config, "base")


@pytest.fixture()
def session(database):
    engine = create_db_engine(database)
    factory = create_session_factory(engine)
    with factory() as value:
        yield value
    engine.dispose()


def agency_and_units(session):
    agencies = AgencyService(session)
    agency, _ = agencies.create_agency(official_name=" 한빛  시청 ", agency_type=AgencyType.BASIC_LOCAL_GOVERNMENT)
    agency_id = uuid.UUID(agency["id"])
    first = agencies.create_org_unit(agency_id=agency_id, name="정보 과", unit_type=OrgUnitType.DEPARTMENT)
    second = agencies.create_org_unit(agency_id=agency_id, name="민원과", unit_type=OrgUnitType.DEPARTMENT)
    return agency_id, uuid.UUID(first["id"]), uuid.UUID(second["id"])


def test_migration_upgrade_and_downgrade(database):
    assert database.endswith("publicdb2.sqlite3")


def test_agency_org_unit_and_duty_relations(session):
    agency_id, unit_id, _ = agency_and_units(session)
    service = AgencyService(session)
    duty = service.create_duty(agency_id=agency_id, org_unit_id=unit_id, title="정보 공개")
    detail = service.agency_detail(agency_id)
    assert detail["name"] == "한빛 시청"
    assert detail["departments"][0]["agency_id"] == str(agency_id)
    assert duty["title"] == "정보 공개"
    assert session.scalar(select(func.count()).select_from(Duty)) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" HTTPS://Example.COM:443/path#part ", "https://example.com/path"),
        ("http://Example.com:80/a/?x=1#f", "http://example.com/a/?x=1"),
        ("https://example.com/a", "https://example.com/a"),
    ],
)
def test_legacy_url_normalization_semantics(raw, expected):
    assert normalize_source_url(raw) == expected


@pytest.mark.parametrize("url", ["", "ftp://example.com/a", "http://localhost/a", "http://127.0.0.1/a", "https://user:pw@example.com"])
def test_invalid_urls_are_rejected(url):
    with pytest.raises(SourceURLValidationError):
        normalize_source_url(url)


def test_canonical_dedup_multi_scope_idempotency_and_lifecycle(session, monkeypatch):
    agency_id, first_id, second_id = agency_and_units(session)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network call"))
    service = SourceService(session)
    first, created = service.register_binding(url="HTTPS://Example.COM:443/a#x", agency_id=agency_id, org_unit_id=first_id)
    same, created_again = service.register_binding(url="https://example.com/a", agency_id=agency_id, org_unit_id=first_id)
    second, created_second = service.register_binding(url="https://example.com/a", agency_id=agency_id, org_unit_id=second_id)
    agency_only, _ = service.register_binding(url="https://example.com/a", agency_id=agency_id)

    assert created and not created_again and created_second
    assert same["id"] == first["id"]
    assert len({first["source_id"], second["source_id"], agency_only["source_id"]}) == 1
    assert session.scalar(select(func.count()).select_from(Source)) == 1
    assert session.scalar(select(func.count()).select_from(SourceBinding)) == 3
    excluded = service.exclude_binding(uuid.UUID(first["id"]), "불필요")
    assert excluded["status"] == "제외"
    assert session.get(Source, uuid.UUID(first["source_id"])) is not None
    assert service.reactivate_binding(uuid.UUID(first["id"]))["status"] == "미확인"


def test_wrong_agency_org_rejected_without_partial_source(session):
    first_agency, _, _ = agency_and_units(session)
    other, _ = AgencyService(session).create_agency(official_name="다른 기관", agency_type=AgencyType.OTHER)
    other_unit = AgencyService(session).create_org_unit(agency_id=uuid.UUID(other["id"]), name="타부서", unit_type=OrgUnitType.DEPARTMENT)
    with pytest.raises(ValueError):
        SourceService(session).register_binding(url="https://invalid-scope.example/path", agency_id=first_agency, org_unit_id=uuid.UUID(other_unit["id"]))
    assert session.scalar(select(func.count()).select_from(Source).where(Source.normalized_url == "https://invalid-scope.example/path")) == 0


def test_live_ui_api_and_empty_states(database):
    app = regression_app(database)
    with TestClient(app) as client:
        empty_agencies = client.get("/agencies")
        empty_sources = client.get("/sources")
        created = client.post("/api/agencies", json={"official_name": "실데이터 기관", "agency_type": "OTHER"})
        agency_id = created.json()["item"]["id"]
        source = client.post("/api/source-bindings", json={"agency_id": agency_id, "url": "https://example.org/source"})
        live_agencies = client.get("/agencies")
        live_sources = client.get("/sources")

    assert "등록된 기관이 없습니다" in empty_agencies.text
    assert "등록된 수집 소스가 없습니다" in empty_sources.text
    assert "실데이터 기관" in live_agencies.text
    assert "https://example.org/source" in live_sources.text
    assert "미확인" in live_sources.text
    assert source.json()["item"]["status"] == "미확인"
    assert "한빛시청" not in live_agencies.text


def test_db_failure_is_explicit_and_not_fixture(tmp_path):
    missing = f"sqlite:///{(tmp_path / 'missing.sqlite3').as_posix()}"
    with TestClient(regression_app(missing)) as client:
        agencies = client.get("/agencies")
        sources = client.get("/sources")
    assert "데이터베이스 오류" in agencies.text
    assert "데이터베이스 오류" in sources.text
    assert "한빛시청" not in agencies.text


def test_all_seven_routes_and_dashboard_freeze(database):
    with TestClient(regression_app(database)) as client:
        pages = {path: client.get(path) for path in ["/", "/agencies", "/sources", "/runs", "/contacts", "/review", "/settings"]}
    assert all(response.status_code == 200 for response in pages.values())
    assert "운영 대시보드" in pages["/"].text
    assert "샘플 데이터" not in pages["/"].text
    assert "kpi-grid" in pages["/"].text
    assert "PublicDB2 DB" in pages["/agencies"].text
    assert "PublicDB2 DB" in pages["/sources"].text

