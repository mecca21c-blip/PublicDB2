from pathlib import Path
import re

import httpx
import pytest
from alembic import command
from alembic.config import Config
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.collectors.http_fetcher import HTTPFetcher
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    Agency, AgencyType, CollectionMethod, ContactPoint, CrawlRun,
    DetectedChangeCandidate, ExtractedContactCandidate, ExtractionRun, Observation,
    OrgUnit, Source, SourceBinding, UserRole,
)
from app.main import create_app
from app.services.agency_service import AgencyService
from app.services.source_agency_discovery_service import (
    AgencyDiscoveryError, SourceAgencyDiscoveryService,
)
from app.services.user_service import UserService
from tests.support import regression_app


HTML = """<!doctype html><html><head>
<title>직원 안내 | 서울특별시 강서구청</title>
<meta property="og:site_name" content="강서구청">
<meta name="description" content="서울특별시 강서구청 직원 안내">
<script type="application/ld+json">{"@type":"GovernmentOrganization","name":"강서구청"}</script>
</head><body><nav class="breadcrumb">홈 &gt; 강서구청 &gt; 직원 안내</nav>
<h1>강서구청 직원 안내</h1><footer>Copyright 강서구청</footer></body></html>""".encode()


@pytest.fixture()
def discovery_db(tmp_path, monkeypatch):
    db_path = tmp_path / "discovery.sqlite3"
    database_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(database_url)
    factory = create_session_factory(engine)
    yield database_url, factory, tmp_path
    engine.dispose()


def fetcher(handler, resolver=lambda _host: {"93.184.216.34"}):
    return HTTPFetcher(
        transport=httpx.MockTransport(handler), resolver=resolver,
        timeout_seconds=1, max_response_bytes=2 * 1024 * 1024,
    )


def html_fetcher(content=HTML):
    return fetcher(lambda request: httpx.Response(
        200, request=request, headers={"Content-Type": "text/html; charset=utf-8"}, content=content,
    ))


def counts(session):
    return {
        model.__name__: session.scalar(select(func.count()).select_from(model))
        for model in (
            Agency, OrgUnit, Source, SourceBinding, CrawlRun, Observation, ExtractionRun,
            ExtractedContactCandidate, DetectedChangeCandidate, ContactPoint,
        )
    }


def test_html_discovery_is_bounded_read_only_and_exact_only(discovery_db):
    _url, factory, root = discovery_db
    with factory() as session:
        service = SourceAgencyDiscoveryService(session, html_fetcher())
        before = counts(session)
        result = service.discover(
            representative_url="https://example.org/staff", collection_method=CollectionMethod.WEB_PAGE,
        )
        assert result["candidate_name"] == "강서구청"
        assert result["existing_agency"] is None
        assert 2 <= len(result["evidence"]) <= 8
        assert result["evidence_summary"]["count"] == len(result["evidence"])
        assert result["evidence_summary"]["types"] == [item["type"] for item in result["evidence"]]
        assert all(len(item["snippet"]) <= 160 for item in result["evidence"])
        assert result["suggested_agency_type"] == "BASIC_LOCAL_GOVERNMENT"
        assert result["suggested_region_code"] == "SEOUL"
        assert counts(session) == before
        assert not list(root.rglob("*.raw"))

        AgencyService(session).create_agency(
            official_name="강서구청소년상담복지센터", agency_type=AgencyType.PUBLIC_INSTITUTION,
        )
        result = service.discover(
            representative_url="https://example.org/staff", collection_method=CollectionMethod.WEB_PAGE,
        )
        assert result["existing_agency"] is None

        exact, _created = AgencyService(session).create_agency(
            official_name="강서구청", agency_type=AgencyType.BASIC_LOCAL_GOVERNMENT,
        )
        result = service.discover(
            representative_url="https://example.org/staff", collection_method=CollectionMethod.WEB_PAGE,
        )
        assert result["existing_agency"]["id"] == exact["id"]
        assert result["match_type"] == "EXACT_NAME"


def test_feed_openapi_authenticated_api_and_ssrf_boundaries(discovery_db):
    _url, factory, _root = discovery_db
    rss = "<rss><channel><title>강서구청</title><description>공식 피드</description><item><title>ignored</title></item></channel></rss>".encode()
    with factory() as session:
        result = SourceAgencyDiscoveryService(session, fetcher(lambda request: httpx.Response(
            200, request=request, headers={"Content-Type": "application/rss+xml"}, content=rss,
        ))).discover(
            representative_url="https://example.org/feed.xml", collection_method=CollectionMethod.API,
            api_kind="RSS", auth_mode="NONE",
        )
        assert result["candidate_name"] == "강서구청"
        assert {item["type"] for item in result["evidence"]} <= {"feed_title", "feed_subtitle", "feed_description"}

        ambiguous = SourceAgencyDiscoveryService(session, fetcher(lambda request: httpx.Response(
            200, request=request, headers={"Content-Type": "application/json"}, json={"data": []},
        ))).discover(
            representative_url="https://example.org/openapi", collection_method=CollectionMethod.API,
            api_kind="OPEN_API", auth_mode="NONE",
        )
        assert ambiguous["candidate_name"] is None and ambiguous["existing_agency"] is None

        def forbidden_fetch(_request):
            raise AssertionError("authenticated APIs must not be fetched")

        authenticated = SourceAgencyDiscoveryService(session, fetcher(forbidden_fetch)).discover(
            representative_url="https://example.org/private?api_key=must-not-return", collection_method=CollectionMethod.API,
            api_kind="open_api", auth_mode="api_key",
        )
        assert authenticated["candidate_name"] is None
        assert "자동 호출하지 않습니다" in authenticated["message"]
        assert "must-not-return" not in str(authenticated)

        blocked = SourceAgencyDiscoveryService(
            session, fetcher(lambda request: httpx.Response(200, request=request, content=HTML), resolver=lambda _host: {"127.0.0.1"}),
        )
        with pytest.raises(AgencyDiscoveryError):
            blocked.discover(representative_url="https://example.org", collection_method=CollectionMethod.WEB_PAGE)

        redirected = SourceAgencyDiscoveryService(session, fetcher(
            lambda request: httpx.Response(302, request=request, headers={"Location": "http://127.0.0.1/private"}),
        ))
        with pytest.raises(AgencyDiscoveryError):
            redirected.discover(representative_url="https://example.org", collection_method=CollectionMethod.WEB_PAGE)


def test_discovery_api_auth_csrf_and_inline_agency_org_writers(discovery_db):
    database_url, factory, root = discovery_db
    app = create_app(database_url, project_root=root)
    app.state.agency_discovery_fetcher_factory = html_fetcher
    request = {
        "collection_method": "WEB_PAGE", "representative_url": "https://example.org/staff",
        "api_kind": None, "auth_mode": None,
    }
    with factory() as session:
        UserService(session).create(username="viewer", password="CorrectHorse12", role=UserRole.VIEWER)
        UserService(session).create(username="operator", password="CorrectHorse12", role=UserRole.OPERATOR)

    def login(client, username):
        login_page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        assert client.post(
            "/login", data={"username": username, "password": "CorrectHorse12", "csrf_token": token},
            follow_redirects=False,
        ).status_code == 303
        page = client.get("/")
        return re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    with TestClient(app) as viewer:
        viewer_token = login(viewer, "viewer")
        assert viewer.post("/api/source-agency-discovery", json=request).status_code == 403
        assert viewer.post(
            "/api/source-agency-discovery", headers={"X-CSRF-Token": viewer_token}, json=request,
        ).status_code == 403

    with TestClient(app) as client:
        token = login(client, "operator")
        assert client.post("/api/source-agency-discovery", json=request).status_code == 403
        discovered = client.post(
            "/api/source-agency-discovery", headers={"X-CSRF-Token": token}, json=request,
        )
        assert discovered.status_code == 200
        with factory() as session:
            assert counts(session) == {name: 0 for name in counts(session)}

        agency_payload = {
            "official_name": "인라인 기관", "agency_type": "OTHER", "region_code": "SEOUL",
            "external_identifier": "inline-1", "address": "서울",
        }
        first = client.post("/api/agencies", headers={"X-CSRF-Token": token}, json=agency_payload)
        second = client.post("/api/agencies", headers={"X-CSRF-Token": token}, json=agency_payload)
        assert first.status_code == second.status_code == 201
        assert first.json()["created"] is True and second.json()["created"] is False
        agency_id = first.json()["item"]["id"]
        conflict = client.post(
            "/api/agencies", headers={"X-CSRF-Token": token},
            json={**agency_payload, "agency_type": "PUBLIC_INSTITUTION"},
        )
        assert conflict.status_code == 400
        org = client.post(
            f"/api/agencies/{agency_id}/org-units", headers={"X-CSRF-Token": token},
            json={"name": "정보화담당관", "unit_type": "DEPARTMENT", "parent_org_unit_id": None},
        )
        assert org.status_code == 201 and org.json()["item"]["agency_id"] == agency_id
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Agency)) == 1
        assert session.scalar(select(func.count()).select_from(OrgUnit)) == 1
        assert session.scalar(select(func.count()).select_from(Source)) == 0


def test_step_three_discovery_and_inline_ui_contract(discovery_db):
    database_url, _factory, root = discovery_db
    page = TestClient(regression_app(database_url, project_root=root)).get("/sources")
    assert page.status_code == 200
    form = BeautifulSoup(page.text, "html.parser").select_one('[data-modal="source-create"] [data-source-wizard]')
    connection = form.select_one('[data-wizard-kind="connection"]')
    assert connection.select_one("[data-agency-discover]").get_text(strip=True) == "페이지에서 기관 찾기"
    assert connection.select_one("[data-discovery-representative]").has_attr("hidden")
    assert connection.select_one("[data-inline-agency-form]").has_attr("hidden")
    assert connection.select_one("[data-inline-org-form]").has_attr("hidden")
    assert connection.select_one('[data-inline-agency-type] option[value="BASIC_LOCAL_GOVERNMENT"]')
    assert connection.select_one("[data-lookup-kind=agency]") and connection.select_one("[data-lookup-kind=org]")
    js = Path("app/web/static/js/app.js").read_text(encoding="utf-8")
    assert 'new Set(urls.map((item) => item.host))' in js
    assert 'fetch("/api/source-agency-discovery"' in js
    assert 'form.dataset.discoveryFingerprint' in js
    assert 'addEventListener("click", async () =>' in js
    assert 'addEventListener("input"' in js
