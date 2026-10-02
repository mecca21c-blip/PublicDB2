from pathlib import Path
import re
import uuid

import httpx
import pytest
from alembic import command
from alembic.config import Config
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.schemas import InteractiveSourceBatchRequest
from app.collectors.http_fetcher import HTTPFetcher
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    Agency, AgencyType, CollectionMethod, CrawlRun, ExtractionRun, Observation,
    OrgUnit, OrgUnitType, Source, SourceBinding, SourceScrapeConfig, UserRole,
)
from app.main import create_app
from app.services.agency_service import AgencyService
from app.services.source_agency_discovery_service import SourceAgencyDiscoveryService
from app.services.source_interactive_batch_service import (
    InteractiveBatchError, SourceInteractiveBatchService,
)
from app.services.user_service import UserService
from tests.support import regression_app


@pytest.fixture()
def intake_db(tmp_path, monkeypatch):
    db_path = tmp_path / "row-intake.sqlite3"
    database_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(database_url)
    factory = create_session_factory(engine)
    yield database_url, factory, tmp_path
    engine.dispose()


def seed_scope(factory):
    with factory() as session:
        service = AgencyService(session)
        first, _ = service.create_agency(official_name="기관 A", agency_type=AgencyType.OTHER)
        second, _ = service.create_agency(official_name="기관 B", agency_type=AgencyType.OTHER)
        org1 = service.create_org_unit(
            agency_id=uuid.UUID(first["id"]), name="부서 1", unit_type=OrgUnitType.DEPARTMENT,
        )
        org2 = service.create_org_unit(
            agency_id=uuid.UUID(first["id"]), name="부서 2", unit_type=OrgUnitType.DEPARTMENT,
        )
        return first, second, org1, org2


def payload(rows, method="WEB_PAGE"):
    return {
        "collection_method": method,
        "rows": rows,
        "description": "행 기반 등록",
        "method_config": {"extract_contacts": True, "extract_directory": True},
        "scheduled_refresh_enabled": False,
    }


def row(row_id, url, agency_name, *, agency_id=None, org_name="", org_id=None, agency_intent=None, org_intent=None):
    return {
        "row_id": row_id, "url": url,
        "agency_name": agency_name, "agency_id": agency_id, "agency_intent": agency_intent,
        "org_unit_name": org_name, "org_unit_id": org_id, "org_unit_intent": org_intent,
    }


def test_row_specific_agencies_orgs_and_common_scrape_config(intake_db):
    _url, factory, _root = intake_db
    agency_a, agency_b, org1, org2 = seed_scope(factory)
    request = InteractiveSourceBatchRequest.model_validate(payload([
        row("r1", "https://example.org/a", "기관 A", org_name="부서 1"),
        row("r2", "https://example.org/b", "기관 A", org_name="부서 2"),
        row("r3", "https://other.example.org/c", "기관 B"),
    ]))
    with factory() as session:
        service = SourceInteractiveBatchService(session)
        preview = service.preview(request)
        assert preview["summary"]["importable"] == 3
        assert preview["summary"]["agency_count"] == 2
        assert preview["summary"]["org_connections"] == 2
        assert {item["status"] for item in preview["rows"]} == {"RESOLVED"}
        result = service.register(request)
        assert result["summary"]["registered"] == 3
        assert result["summary"]["created_sources"] == 3
        bindings = list(session.scalars(select(SourceBinding).order_by(SourceBinding.scope_key)))
        assert len(bindings) == 3
        contexts = {(str(item.agency_id), str(item.org_unit_id) if item.org_unit_id else None) for item in bindings}
        assert contexts == {
            (agency_a["id"], org1["id"]), (agency_a["id"], org2["id"]), (agency_b["id"], None),
        }
        assert session.scalar(select(func.count()).select_from(SourceScrapeConfig)) == 3
        assert all(source.scheduled_refresh_enabled is False for source in session.scalars(select(Source)))
        assert session.scalar(select(func.count()).select_from(CrawlRun)) == 0


def test_unknown_resolution_is_unique_and_typo_is_suggestion_only(intake_db):
    _url, factory, _root = intake_db
    with factory() as session:
        AgencyService(session).create_agency(
            official_name="서울특별시", agency_type=AgencyType.METROPOLITAN_GOVERNMENT,
        )
    rows = [row(f"r{index}", f"https://example.org/{index}", "서을특별시") for index in range(5)]
    request = InteractiveSourceBatchRequest.model_validate(payload(rows))
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(Agency))
        preview = SourceInteractiveBatchService(session).preview(request)
        assert preview["summary"]["unresolved"] == 5
        assert len(preview["agency_resolutions"]) == 1
        resolution = preview["agency_resolutions"][0]
        assert resolution["row_ids"] == [f"r{index}" for index in range(5)]
        assert resolution["similar"][0]["name"] == "서울특별시"
        assert all(item["agency_id"] is None for item in preview["rows"])
        assert session.scalar(select(func.count()).select_from(Agency)) == before
        assert session.scalar(select(func.count()).select_from(Source)) == 0


def test_new_entity_intents_write_only_at_final_and_race_reuses(intake_db):
    _url, factory, _root = intake_db
    agency_intent = {
        "official_name": "신규 기관", "agency_type": "OTHER", "region_code": "SEOUL",
        "external_identifier": None, "address": "서울",
    }
    org_intent = {"name": "신규 부서", "unit_type": "DEPARTMENT"}
    request = InteractiveSourceBatchRequest.model_validate(payload([
        row("r1", "https://example.org/a", "신규 기관", agency_intent=agency_intent, org_name="신규 부서", org_intent=org_intent),
        row("r2", "https://example.org/b", "신규 기관", agency_intent=agency_intent),
    ]))
    with factory() as session:
        preview = SourceInteractiveBatchService(session).preview(request)
        assert preview["summary"]["planned_new"] == 2
        assert preview["summary"]["new_agencies_planned"] == 1
        assert preview["summary"]["new_org_units_planned"] == 1
        assert session.scalar(select(func.count()).select_from(Agency)) == 0
        assert session.scalar(select(func.count()).select_from(OrgUnit)) == 0
        assert session.scalar(select(func.count()).select_from(Source)) == 0

    # Simulate another writer completing the Agency after preview but before final registration.
    with factory() as session:
        AgencyService(session).create_agency(official_name="신규 기관", agency_type=AgencyType.OTHER, region_code="SEOUL")
    with factory() as session:
        result = SourceInteractiveBatchService(session).register(request)
        assert result["summary"]["registered"] == 2
        assert result["summary"]["created_agencies"] == 0
        assert result["summary"]["created_org_units"] == 1
        assert session.scalar(select(func.count()).select_from(Agency)) == 1
        assert session.scalar(select(func.count()).select_from(OrgUnit)) == 1
        assert session.scalar(select(func.count()).select_from(Source)) == 2


def test_duplicate_partial_success_and_single_row_methods(intake_db):
    _url, factory, _root = intake_db
    agency, _other, _org1, _org2 = seed_scope(factory)
    mixed = InteractiveSourceBatchRequest.model_validate(payload([
        row("r1", "https://EXAMPLE.org:443/a#top", "기관 A", agency_id=agency["id"]),
        row("r2", "https://example.org/a", "기관 A", agency_id=agency["id"]),
        row("r3", "not-a-url", "기관 A", agency_id=agency["id"]),
        row("r4", "https://example.org/good", "기관 A", agency_id=agency["id"]),
    ]))
    with factory() as session:
        preview = SourceInteractiveBatchService(session).preview(mixed)
        assert preview["summary"]["duplicates"] == 1
        assert preview["summary"]["errors"] == 1
        result = SourceInteractiveBatchService(session).register(mixed)
        assert result["summary"]["registered"] == 2
        assert result["summary"]["duplicates_skipped"] == 1
        assert result["summary"]["errors"] == 1
        assert session.scalar(select(func.count()).select_from(Source)) == 2

        crawl = InteractiveSourceBatchRequest.model_validate(payload([
            row("c1", "https://example.org/index", "기관 A", agency_id=agency["id"]),
            row("c2", "https://example.org/index2", "기관 A", agency_id=agency["id"]),
        ], method="WEB_CRAWL"))
        with pytest.raises(InteractiveBatchError):
            SourceInteractiveBatchService(session).preview(crawl)

        oversized = InteractiveSourceBatchRequest.model_validate(payload([
            row(f"many-{index}", f"https://example.org/many/{index}", "기관 A", agency_id=agency["id"])
            for index in range(201)
        ]))
        with pytest.raises(InteractiveBatchError, match="엑셀 업로드"):
            SourceInteractiveBatchService(session).preview(oversized)


def test_existing_canonical_source_is_reused_for_new_row_context(intake_db):
    _url, factory, _root = intake_db
    agency_a, agency_b, _org1, _org2 = seed_scope(factory)
    first = InteractiveSourceBatchRequest.model_validate(payload([
        row("first", "https://example.org/shared", "기관 A", agency_id=agency_a["id"]),
    ]))
    second = InteractiveSourceBatchRequest.model_validate(payload([
        row("second", "https://EXAMPLE.org:443/shared#fragment", "기관 B", agency_id=agency_b["id"]),
    ]))
    with factory() as session:
        service = SourceInteractiveBatchService(session)
        assert service.register(first)["summary"]["created_sources"] == 1
        preview = service.preview(second)
        assert preview["summary"]["existing_sources"] == 1
        assert preview["summary"]["new_bindings"] == 1
        result = service.register(second)
        assert result["summary"]["existing_sources_reused"] == 1
        assert result["summary"]["created_bindings"] == 1
        assert session.scalar(select(func.count()).select_from(Source)) == 1
        assert session.scalar(select(func.count()).select_from(SourceBinding)) == 2


def test_discovery_adds_conservative_org_suggestion_without_writes(intake_db):
    _url, factory, _root = intake_db
    with factory() as session:
        agency, _created = AgencyService(session).create_agency(
            official_name="강서구청", agency_type=AgencyType.BASIC_LOCAL_GOVERNMENT,
        )
        AgencyService(session).create_org_unit(
            agency_id=uuid.UUID(agency["id"]), name="정보화담당관", unit_type=OrgUnitType.DEPARTMENT,
        )
        html = """<html><head><meta property="og:site_name" content="강서구청">
        <title>정보화담당관 | 강서구청</title><script type="application/ld+json">{"@type":"Organization","name":"강서구청"}</script>
        </head><body><nav class="breadcrumb">홈 &gt; 강서구청 &gt; 정보화담당관</nav><h1>정보화담당관</h1></body></html>""".encode()
        fetcher = HTTPFetcher(
            transport=httpx.MockTransport(lambda request: httpx.Response(
                200, request=request, headers={"Content-Type": "text/html; charset=utf-8"}, content=html,
            )), resolver=lambda _host: {"93.184.216.34"},
        )
        before = {
            model: session.scalar(select(func.count()).select_from(model))
            for model in (Agency, OrgUnit, Source, CrawlRun, Observation, ExtractionRun)
        }
        result = SourceAgencyDiscoveryService(session, fetcher).discover(
            representative_url="https://example.org/staff", collection_method=CollectionMethod.WEB_PAGE,
        )
        assert result["candidate_name"] == "강서구청"
        assert result["existing_agency"]["id"] == agency["id"]
        assert result["org_unit_candidate"] == "정보화담당관"
        assert result["existing_org_unit"]["name"] == "정보화담당관"
        assert {model: session.scalar(select(func.count()).select_from(model)) for model in before} == before


def test_row_grid_ui_and_bounded_queue_contract(intake_db):
    database_url, _factory, root = intake_db
    page = TestClient(regression_app(database_url, project_root=root)).get("/sources")
    assert page.status_code == 200
    form = BeautifulSoup(page.text, "html.parser").select_one('[data-modal="source-create"] [data-source-create]')
    steps = form.select("[data-wizard-step]")
    assert [item["data-wizard-kind"] for item in steps] == ["row-basic", "config", "review"]
    assert [item.get_text(" ", strip=True).split(" ", 2)[:2] for item in form.select("[data-wizard-indicator]")] == [
        ["1", "기본"], ["2", "수집"], ["3", "등록"],
    ]
    grid = form.select_one("[data-source-intake]")
    assert grid.select_one("[data-source-row-template]")
    assert grid.select_one("[data-row-url]") and grid.select_one("[data-row-agency]") and grid.select_one("[data-row-org]")
    assert not form.select_one('[data-wizard-step="2"] textarea[name="urls"]')
    assert not form.select_one("[data-discovery-representative]")
    js = Path("app/web/static/js/app.js").read_text(encoding="utf-8")
    assert "activeDiscoveries < 2" in js
    assert "job.generation !== state.generation" in js
    assert "input._lookupGeneration !== lookupGeneration" in js
    assert "candidate.isConnected && !stateOf(candidate).duplicateOf" in js
    assert 'text.split(/\\r?\\n/)' in js
    assert 'fetch("/api/source-bindings/interactive/preview"' in js
    assert 'fetch("/api/source-bindings/interactive/register"' in js


def test_interactive_preview_and_register_api_routes(intake_db):
    database_url, factory, root = intake_db
    agency, _other, _org1, _org2 = seed_scope(factory)
    client = TestClient(regression_app(database_url, project_root=root))
    body = payload([
        row("api-1", "https://example.org/api-route", "기관 A", agency_id=agency["id"]),
    ])
    preview = client.post(
        "/api/source-bindings/interactive/preview",
        headers={"X-CSRF-Token": "test-csrf"}, json=body,
    )
    assert preview.status_code == 200
    assert preview.json()["summary"]["importable"] == 1
    registered = client.post(
        "/api/source-bindings/interactive/register",
        headers={"X-CSRF-Token": "test-csrf"}, json=body,
    )
    assert registered.status_code == 201
    assert registered.json()["summary"]["registered"] == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 1
        assert session.scalar(select(func.count()).select_from(CrawlRun)) == 0


def test_interactive_routes_require_operator_and_csrf(intake_db):
    database_url, factory, root = intake_db
    agency, _other, _org1, _org2 = seed_scope(factory)
    with factory() as session:
        UserService(session).create(username="viewer", password="CorrectHorse12", role=UserRole.VIEWER)
        UserService(session).create(username="operator", password="CorrectHorse12", role=UserRole.OPERATOR)
    app = create_app(database_url, project_root=root)
    body = payload([row("auth-1", "https://example.org/auth", "기관 A", agency_id=agency["id"])])

    def login(client, username):
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert client.post(
            "/login", data={"username": username, "password": "CorrectHorse12", "csrf_token": token},
            follow_redirects=False,
        ).status_code == 303
        return re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text).group(1)

    with TestClient(app) as viewer:
        token = login(viewer, "viewer")
        assert viewer.post(
            "/api/source-bindings/interactive/preview", headers={"X-CSRF-Token": token}, json=body,
        ).status_code == 403
    with TestClient(app) as operator:
        token = login(operator, "operator")
        assert operator.post("/api/source-bindings/interactive/preview", json=body).status_code == 403
        assert operator.post(
            "/api/source-bindings/interactive/preview", headers={"X-CSRF-Token": token}, json=body,
        ).status_code == 200
