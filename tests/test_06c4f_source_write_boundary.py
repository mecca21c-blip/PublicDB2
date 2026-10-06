from pathlib import Path
import uuid

import httpx
import pytest
from alembic import command
from alembic.config import Config
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.schemas import InteractiveSourceBatchRequest
from app.collectors.http_fetcher import HTTPFetcher
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    Agency, AgencyType, CollectionJob, CollectionJobItem, CollectionMethod,
    CrawlRun, ExtractionRun, Observation, OrgUnit, Source, SourceApiConfig,
    SourceBinding, SourceCrawlConfig, SourceScrapeConfig,
)
from app.services.agency_service import AgencyService
from app.services.source_agency_discovery_service import SourceAgencyDiscoveryService
from app.services.source_interactive_batch_service import SourceInteractiveBatchService
from tests.support import regression_app


DOMAIN_MODELS = (
    Agency, OrgUnit, Source, SourceBinding, SourceScrapeConfig, SourceCrawlConfig,
    SourceApiConfig, CrawlRun, Observation, ExtractionRun, CollectionJob,
    CollectionJobItem,
)


@pytest.fixture()
def boundary_db(tmp_path, monkeypatch):
    database_path = tmp_path / "write-boundary.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(database_url)
    factory = create_session_factory(engine)
    yield database_url, factory, tmp_path
    engine.dispose()


def _fingerprint(factory):
    with factory() as session:
        return {
            model.__tablename__: tuple(
                str(value) for value in session.scalars(select(model.id).order_by(model.id))
            )
            for model in DOMAIN_MODELS
        }


def _row(row_id, url, agency_name, *, agency_id=None, agency_intent=None):
    return {
        "row_id": row_id,
        "url": url,
        "agency_name": agency_name,
        "agency_id": agency_id,
        "agency_intent": agency_intent,
        "org_unit_name": "",
        "org_unit_id": None,
        "org_unit_intent": None,
    }


def _request(rows, *, method_config=None):
    return InteractiveSourceBatchRequest.model_validate({
        "collection_method": "WEB_PAGE",
        "rows": rows,
        "description": "06C-4F write-boundary regression",
        "method_config": method_config or {
            "extract_contacts": True,
            "extract_directory": True,
        },
        "scheduled_refresh_enabled": True,
    })


def _seed_incident_agencies(factory):
    with factory() as session:
        service = AgencyService(session)
        seoul, _ = service.create_agency(
            official_name="서울특별시",
            agency_type=AgencyType.METROPOLITAN_GOVERNMENT,
        )
        gangseo, _ = service.create_agency(
            official_name="강서구청",
            agency_type=AgencyType.BASIC_LOCAL_GOVERNMENT,
        )
        return seoul, gangseo


def test_create_wizard_has_one_explicit_registration_owner(boundary_db):
    database_url, _factory, root = boundary_db
    page = TestClient(regression_app(database_url, project_root=root)).get("/sources")
    assert page.status_code == 200
    soup = BeautifulSoup(page.text, "html.parser")
    create = soup.select_one('[data-modal="source-create"] [data-source-create]')
    assert create is not None
    assert not create.has_attr("data-api-form")
    assert all(button.get("type") == "button" for button in create.select("button"))
    register = create.select_one("[data-source-register][data-wizard-submit]")
    assert register is not None and register.get("type") == "button"
    assert register.has_attr("disabled")
    assert not create.select_one('button[type="submit"]')
    assert create.select_one("[data-interactive-result-title]")
    assert create.select_one("[data-interactive-error-rows]")
    assert create.select_one("[data-interactive-fix]")
    assert create.select_one("[data-scrape-list]")
    assert create.select_one("[data-scrape-collect]")

    edit = soup.select_one('[data-modal^="edit-"] [data-source-wizard]')
    if edit is not None:
        assert edit.has_attr("data-api-form")
        assert len(edit.select('button[type="submit"]')) == 1
        assert all(
            button.get("type") == "button"
            for button in edit.select("button:not([type=submit])")
        )

    javascript = Path("app/web/static/js/app.js").read_text(encoding="utf-8")
    generic_start = javascript.index('document.querySelectorAll("[data-api-form]")')
    generic_end = javascript.index('document.querySelectorAll("[data-api-action]")', generic_start)
    generic_handler = javascript[generic_start:generic_end]
    assert "/api/source-bindings/interactive/register" not in generic_handler
    assert 'form.addEventListener("submit", (event) =>' in javascript
    assert "Source 등록은 3단계의 등록 버튼으로만 실행할 수 있습니다." in javascript
    assert 'submit.addEventListener("click", async () =>' in javascript
    assert 'submit.textContent = "등록 중..."' in javascript
    assert "if (registering) return" in javascript
    assert "submit.disabled = requestSucceeded || !registrationReady" in javascript
    assert 'errorBox.scrollIntoView({block: "nearest"})' in javascript
    assert "등록 완료 · 일부 오류" in javascript
    assert "등록 요청은 성공했지만 결과 화면을 표시하지 못했습니다." in javascript


def test_discovery_lookup_preview_intent_and_cancel_write_zero(boundary_db):
    database_url, factory, root = boundary_db
    seoul, _gangseo = _seed_incident_agencies(factory)
    baseline = _fingerprint(factory)

    html = b"""<html><head><meta property='og:site_name' content='Seoul'></head>
    <body><h1>Seoul</h1></body></html>"""
    with factory() as session:
        fetcher = HTTPFetcher(
            transport=httpx.MockTransport(lambda request: httpx.Response(
                200,
                request=request,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=html,
            )),
            resolver=lambda _host: {"93.184.216.34"},
        )
        SourceAgencyDiscoveryService(session, fetcher).discover(
            representative_url="https://example.org/seoul",
            collection_method=CollectionMethod.WEB_PAGE,
        )
    assert _fingerprint(factory) == baseline

    client = TestClient(regression_app(database_url, project_root=root))
    assert client.get("/api/lookups/agencies?q=서울&limit=8").status_code == 200
    assert client.get(f"/api/lookups/agencies/{seoul['id']}/org-units?q=정보&limit=8").status_code == 200
    assert _fingerprint(factory) == baseline

    request = _request([
        _row(
            "intent-1",
            "https://example.org/new-agency",
            "신규 시험기관",
            agency_intent={
                "official_name": "신규 시험기관",
                "agency_type": "OTHER",
                "region_code": None,
                "external_identifier": None,
                "address": None,
            },
        ),
    ])
    with factory() as session:
        preview = SourceInteractiveBatchService(session).preview(request)
        assert preview["summary"]["new_agencies_planned"] == 1
        assert preview["summary"]["new_sources"] == 1
    assert _fingerprint(factory) == baseline
    # Closing/cancelling performs no endpoint call, so the full domain snapshot remains S0.
    assert _fingerprint(factory) == baseline


def test_original_three_row_incident_preview_is_read_only_and_register_is_idempotent(boundary_db):
    database_url, factory, root = boundary_db
    seoul, gangseo = _seed_incident_agencies(factory)
    request = _request([
        _row(
            "seoul",
            "https://www.seoul.go.kr/seoul/seoul.do",
            "서울특별시",
            agency_id=seoul["id"],
        ),
        _row(
            "gangseo",
            "https://data.gangseo.seoul.kr/openinf/openapiview.jsp?infId=OA-10005",
            "강서구청",
            agency_id=gangseo["id"],
        ),
        _row(
            "busan",
            "https://www.busan.go.kr/bhinspec01?dc=6261933&org=y",
            "부산광역시",
            agency_intent={
                "official_name": "부산광역시",
                "agency_type": "METROPOLITAN_GOVERNMENT",
                "region_code": "BUSAN",
                "external_identifier": None,
                "address": None,
            },
        ),
    ])
    baseline = _fingerprint(factory)
    with factory() as session:
        preview = SourceInteractiveBatchService(session).preview(request)
        assert preview["summary"]["input"] == 3
        assert preview["summary"]["new_sources"] == 3
        assert preview["summary"]["existing_sources"] == 0
        assert preview["summary"]["new_agencies_planned"] == 1
        assert preview["summary"]["new_bindings"] == 3
    assert _fingerprint(factory) == baseline

    client = TestClient(regression_app(database_url, project_root=root))
    response = client.post(
        "/api/source-bindings/interactive/preview",
        headers={"X-CSRF-Token": "test-csrf"},
        json=request.model_dump(mode="json"),
    )
    assert response.status_code == 200
    assert response.json()["summary"]["new_sources"] == 3
    assert response.json()["summary"]["existing_sources"] == 0
    assert _fingerprint(factory) == baseline

    with factory() as session:
        first = SourceInteractiveBatchService(session).register(request)
        assert first["summary"]["registered"] == 3
        assert first["summary"]["created_sources"] == 3
        assert first["summary"]["created_bindings"] == 3
        assert first["summary"]["created_agencies"] == 1
    after_first = _fingerprint(factory)
    assert len(after_first["sources"]) == 3
    assert len(after_first["source_bindings"]) == 3
    assert len(after_first["source_scrape_configs"]) == 3
    assert len(after_first["agencies"]) == 3
    assert not after_first["crawl_runs"]
    assert not after_first["observations"]
    assert not after_first["collection_jobs"]

    with factory() as session:
        repeated = SourceInteractiveBatchService(session).register(request)
        assert repeated["summary"]["created_sources"] == 0
        assert repeated["summary"]["created_bindings"] == 0
        assert repeated["summary"]["created_agencies"] == 0
        assert repeated["summary"]["existing_sources_reused"] == 3
    assert _fingerprint(factory) == after_first


def test_failed_row_rolls_back_new_agency_and_source_together(boundary_db):
    _database_url, factory, _root = boundary_db
    request = _request([
        _row(
            "rollback",
            "https://example.org/config-error",
            "고아 방지 기관",
            agency_intent={
                "official_name": "고아 방지 기관",
                "agency_type": "OTHER",
                "region_code": None,
                "external_identifier": None,
                "address": None,
            },
        ),
    ], method_config={"extract_contacts": False, "extract_directory": False})
    baseline = _fingerprint(factory)
    with factory() as session:
        result = SourceInteractiveBatchService(session).register(request)
        assert result["summary"]["registered"] == 0
        assert result["summary"]["errors"] == 1
        assert result["rows"][0]["result"] == "ERROR"
    assert _fingerprint(factory) == baseline
