from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.collectors.http_fetcher import HTTPFetcher
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    AgencyType, ApiSourceKind, CollectionMethod, CrawlRun, ExtractedDirectoryRecord,
    ExtractedFeedItem, ExtractionRun, Observation, RunStatus, Source,
    SourceApiConfig, SourceCrawlConfig, SourceScrapeConfig,
)
from app.models.common import utc_now
from app.services.agency_service import AgencyService
from app.services.api_credential_store import ApiCredentialStore
from app.services.catalog_service import CatalogService
from app.services.collection_service import CollectionCoordinator, CollectionService
from app.services.source_import_service import parse_import_file, SourceImportService
from app.services.source_method_service import MethodConfigError, SourceMethodService
from app.services.source_service import SourceService
from app.main import create_app
from app.models import UserRole
from app.services.user_service import UserService


PUBLIC_DNS = lambda _host: {"93.184.216.34"}
DIRECTORY = b"""<html><body><table><tr><th>\xeb\xb6\x80\xec\x84\x9c</th><th>\xec\x97\x85\xeb\xac\xb4</th><th>\xec\xa0\x84\xed\x99\x94</th></tr>
<tr><td>Data</td><td>Open</td><td>02-1234-5678</td></tr></table></body></html>"""


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    with factory() as session:
        yield session, tmp_path
    engine.dispose()


def add_source(session, method=CollectionMethod.WEB_PAGE, config=None, url="https://example.org/root/index.html"):
    agency, _ = AgencyService(session).create_agency(official_name=f"Agency-{uuid.uuid4()}", agency_type=AgencyType.OTHER)
    binding, _ = SourceService(session).register_binding(
        url=url, agency_id=uuid.UUID(agency["id"]),
        collection_method=method, method_config=config or {},
    )
    return session.get(Source, uuid.UUID(binding["source_id"]))


def collector(session, root: Path, handler, **values):
    return CollectionService(
        session, project_root=root, raw_root=root / "data" / "raw",
        coordinator=CollectionCoordinator(),
        fetcher=HTTPFetcher(transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS),
        sleeper=values.pop("sleeper", lambda _seconds: None), **values,
    )


def test_typed_config_defaults_validation_and_historical_snapshot(db):
    session, root = db
    source = add_source(session)
    assert isinstance(source.scrape_config, SourceScrapeConfig)
    assert source.scrape_config.extract_contacts and source.scrape_config.extract_directory
    with pytest.raises(MethodConfigError):
        SourceMethodService(session).configure(
            source, CollectionMethod.WEB_PAGE,
            {"extract_contacts": False, "extract_directory": False},
        )
    result = collector(
        session, root,
        lambda _request: httpx.Response(200, headers={"Content-Type": "text/html"}, content=DIRECTORY),
    ).collect(source.id)
    assert result.crawl_run.collection_method_snapshot is CollectionMethod.WEB_PAGE
    old_snapshot = dict(result.crawl_run.collection_config_snapshot)
    SourceMethodService(session).configure(source, CollectionMethod.WEB_CRAWL, {
        "scope": "PATH_PREFIX", "allowed_path": "/root", "max_depth": 1,
        "max_pages": 3, "request_delay_ms": 500,
    })
    session.refresh(result.crawl_run)
    assert result.crawl_run.collection_method_snapshot is CollectionMethod.WEB_PAGE
    assert result.crawl_run.collection_config_snapshot == old_snapshot
    assert source.scrape_config is None
    assert isinstance(source.crawl_config, SourceCrawlConfig)


def test_scrape_toggle_runs_only_enabled_extractor(db):
    session, root = db
    source = add_source(session, config={"extract_contacts": False, "extract_directory": True})
    result = collector(
        session, root,
        lambda _request: httpx.Response(200, headers={"Content-Type": "text/html"}, content=DIRECTORY),
    ).collect(source.id)
    names = set(session.scalars(select(ExtractionRun.extractor_name)))
    assert names == {"staff_directory"}
    assert result.crawl_run.status is RunStatus.SUCCESS


def test_crawl_is_bounded_deduplicated_multi_observation_and_raw_safe(db):
    session, root = db
    source = add_source(session, CollectionMethod.WEB_CRAWL, {
        "scope": "PATH_PREFIX", "allowed_path": "/root", "max_depth": 1,
        "max_pages": 2, "request_delay_ms": 500,
    })
    delays = []
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path.endswith("index.html"):
            return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b'<a href="child.html#one">a</a><a href="child.html#two">b</a><a href="/outside">x</a>')
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=DIRECTORY)

    result = collector(session, root, handler, sleeper=delays.append).collect(source.id)
    assert result.crawl_run.status is RunStatus.SUCCESS
    assert result.crawl_run.collection_method_snapshot is CollectionMethod.WEB_CRAWL
    assert result.crawl_run.collection_statistics["pages_attempted"] == 2
    assert result.crawl_run.collection_statistics["pages_discovered"] == 2
    observations = list(session.scalars(select(Observation).where(Observation.crawl_run_id == result.crawl_run.id)))
    assert len(observations) == 2
    assert len({item.artifact_path for item in observations}) == 2
    assert all((root / item.artifact_path).is_file() for item in observations)
    assert delays == [0.5]
    assert calls.count("/root/child.html") == 1


def test_crawl_robots_and_partial_preserve_success(db):
    session, root = db
    denied = add_source(
        session, CollectionMethod.WEB_CRAWL,
        {"allowed_path": "/private", "max_depth": 1, "max_pages": 2, "request_delay_ms": 500},
        "https://example.org/private/index.html",
    )
    def denied_handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"User-agent: *\nDisallow: /private")
        raise AssertionError("robots-denied page was fetched")
    denied_result = collector(session, root, denied_handler).collect(denied.id)
    assert denied_result.crawl_run.status is RunStatus.FAILED
    assert session.scalar(select(func.count()).select_from(Observation).where(Observation.crawl_run_id == denied_result.crawl_run.id)) == 0

    partial = add_source(
        session, CollectionMethod.WEB_CRAWL,
        {"allowed_path": "/partial", "max_depth": 1, "max_pages": 3, "request_delay_ms": 500},
        "https://example.org/partial/index.html",
    )
    def partial_handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path.endswith("index.html"):
            return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b'<a href="ok.html">ok</a><a href="bad.html">bad</a>')
        if request.url.path.endswith("bad.html"):
            return httpx.Response(500)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=DIRECTORY)
    partial_result = collector(session, root, partial_handler).collect(partial.id)
    assert partial_result.crawl_run.status is RunStatus.PARTIAL
    assert partial_result.crawl_run.collection_statistics["pages_succeeded"] == 2
    assert partial_result.crawl_run.collection_statistics["pages_failed"] == 1
    assert session.scalar(select(func.count()).select_from(Observation).where(Observation.crawl_run_id == partial_result.crawl_run.id)) == 2


@pytest.mark.parametrize(
    ("response_format", "content_type", "record_path", "body", "mapping"),
    [
        ("JSON", "application/json", "body.items", b'{"body":{"items":[{"dept":"Data","task":"Open","tel":"02-1234-5678"}]}}', {"org_unit": "dept", "duty": "task", "phone": "tel"}),
        ("XML", "application/xml", "items/item", b"<root><items><item><dept>Data</dept><task>Open</task><tel>02-1234-5678</tel></item></items></root>", {"org_unit": "dept", "duty": "task", "phone": "tel"}),
        ("CSV", "text/csv", None, b"dept,task,tel\r\nData,Open,02-1234-5678\r\n", {"org_unit": "dept", "duty": "task", "phone": "tel"}),
    ],
)
def test_openapi_formats_map_into_existing_directory_discovery(db, response_format, content_type, record_path, body, mapping):
    session, root = db
    source = add_source(session, CollectionMethod.API, {
        "kind": "OPEN_API", "response_format": response_format,
        "record_path": record_path, "field_mapping": mapping, "auth_mode": "NONE",
    }, f"https://example.org/{response_format.lower()}")
    result = collector(
        session, root,
        lambda _request: httpx.Response(200, headers={"Content-Type": content_type}, content=body),
    ).collect(source.id)
    assert result.crawl_run.status is RunStatus.SUCCESS
    assert result.crawl_run.collection_kind_snapshot == "OPEN_API"
    assert result.crawl_run.collection_statistics == {
        "requests_attempted": 1, "requests_succeeded": 1, "raw_records": 1, "mapped_records": 1,
    }
    assert session.scalar(select(func.count()).select_from(ExtractedDirectoryRecord).where(ExtractedDirectoryRecord.observation_id == result.observation.id)) == 1


def test_openapi_mapping_failure_is_error_and_zero_records_is_empty(db):
    session, root = db
    broken = add_source(session, CollectionMethod.API, {
        "kind": "OPEN_API", "response_format": "JSON", "record_path": "missing.items",
        "field_mapping": {"org_unit": "dept"}, "auth_mode": "NONE",
    }, "https://example.org/broken")
    failed = collector(
        session, root,
        lambda _request: httpx.Response(200, headers={"Content-Type": "application/json"}, content=b'{"items":[]}'),
    ).collect(broken.id)
    assert failed.crawl_run.status is RunStatus.FAILED
    assert failed.crawl_run.connection_status.value == "SUCCESS"
    assert failed.crawl_run.raw_status.value == "SUCCESS"
    assert failed.crawl_run.extraction_status.value == "FAILED"

    empty = add_source(session, CollectionMethod.API, {
        "kind": "OPEN_API", "response_format": "JSON", "record_path": "items",
        "field_mapping": {"org_unit": "dept"}, "auth_mode": "NONE",
    }, "https://example.org/empty")
    empty_result = collector(
        session, root,
        lambda _request: httpx.Response(200, headers={"Content-Type": "application/json"}, content=b'{"items":[]}'),
    ).collect(empty.id)
    assert empty_result.crawl_run.status is RunStatus.SUCCESS
    assert empty_result.crawl_run.records_observed == 0
    binding = empty.bindings[0]
    assert SourceService(session).get_binding(binding.id)["status"] == "자료없음"

    discovery = add_source(session, CollectionMethod.API, {
        "kind": "OPEN_API", "response_format": "JSON", "record_path": "items",
        "field_mapping": {}, "discovery_only": True, "auth_mode": "NONE",
    }, "https://example.org/discovery")
    discovery_result = collector(
        session, root,
        lambda _request: httpx.Response(200, headers={"Content-Type": "application/json"}, content=b'{"items":[{"arbitrary":"value"}]}'),
    ).collect(discovery.id)
    assert discovery_result.crawl_run.status is RunStatus.SUCCESS
    assert discovery_result.crawl_run.records_observed == 1
    assert SourceService(session).get_binding(discovery.bindings[0].id)["status"] == "정상"


def test_api_pagination_credentials_expiry_and_secret_non_persistence(db):
    session, root = db
    store = ApiCredentialStore(root / "config")
    ref = store.save("TOP-SECRET")["credential_ref"]
    source = add_source(session, CollectionMethod.API, {
        "kind": "OPEN_API", "response_format": "JSON", "record_path": "items",
        "field_mapping": {"org_unit": "dept", "duty": "task"},
        "pagination_mode": "PAGE_NUMBER", "page_parameter": "page",
        "page_size_parameter": "size", "page_size": 10, "start_page": 1,
        "max_pages": 2, "request_delay_ms": 500,
        "static_params": {"dataset": "staff"},
        "auth_mode": "QUERY_API_KEY", "credential_ref": ref, "credential_name": "serviceKey",
    }, "https://example.org/api")
    seen = []
    def handler(request):
        seen.append(dict(request.url.params))
        page = request.url.params["page"]
        return httpx.Response(200, headers={"Content-Type": "application/json"}, content=json.dumps({"items": [{"dept": f"D{page}", "task": "T"}]}).encode())
    result = collector(session, root, handler, credential_store=store).collect(source.id)
    assert result.crawl_run.status is RunStatus.SUCCESS
    assert [item["page"] for item in seen] == ["1", "2"]
    assert all(item["serviceKey"] == "TOP-SECRET" for item in seen)
    assert all(item["dataset"] == "staff" for item in seen)
    assert "TOP-SECRET" not in json.dumps(result.crawl_run.collection_config_snapshot)
    assert "TOP-SECRET" not in source.url
    assert "TOP-SECRET" not in repr(session.get(SourceApiConfig, source.api_config.id).__dict__)

    source.api_config.credential_expires_on = date.today() - timedelta(days=1)
    session.commit()
    before = len(seen)
    expired = collector(session, root, handler, credential_store=store).collect(source.id)
    assert expired.crawl_run.status is RunStatus.FAILED
    assert len(seen) == before

    header_source = add_source(session, CollectionMethod.API, {
        "kind": "OPEN_API", "response_format": "JSON", "record_path": "items",
        "field_mapping": {"org_unit": "dept"}, "auth_mode": "HEADER_API_KEY",
        "credential_ref": ref, "credential_name": "X-Api-Key",
    }, "https://example.org/header-api")
    header_values = []
    header_result = collector(
        session, root,
        lambda request: (
            header_values.append(request.headers.get("X-Api-Key"))
            or httpx.Response(200, headers={"Content-Type": "application/json"}, content=b'{"items":[{"dept":"D"}]}')
        ),
        credential_store=store,
    ).collect(header_source.id)
    assert header_result.crawl_run.status is RunStatus.SUCCESS
    assert header_values == ["TOP-SECRET"]
    assert "TOP-SECRET" not in json.dumps(header_result.crawl_run.collection_config_snapshot)


@pytest.mark.parametrize(("kind", "body", "expected"), [
    ("RSS", b"<rss><channel><item><guid>1</guid><title>A</title><link>https://example.org/a</link></item></channel></rss>", 1),
    ("ATOM", b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id><title>A</title><link href="https://example.org/a"/></entry></feed>', 1),
    ("RSS", b"<rss><channel></channel></rss>", 0),
])
def test_rss_atom_discovery_only_and_zero_semantics(db, kind, body, expected):
    session, root = db
    source = add_source(session, CollectionMethod.API, {"kind": kind}, f"https://example.org/{kind.lower()}")
    result = collector(
        session, root,
        lambda _request: httpx.Response(200, headers={"Content-Type": "application/xml"}, content=body),
    ).collect(source.id)
    assert result.crawl_run.status is RunStatus.SUCCESS
    assert result.crawl_run.records_observed == expected
    assert result.crawl_run.collection_statistics["feed_items"] == expected
    assert session.scalar(select(func.count()).select_from(ExtractedFeedItem).where(ExtractedFeedItem.observation_id == result.observation.id)) == expected
    assert session.scalar(select(func.count()).select_from(ExtractedDirectoryRecord).where(ExtractedDirectoryRecord.observation_id == result.observation.id)) == 0


def test_catalog_is_truthfully_empty_and_excel_method_is_optional():
    assert CatalogService(None).list() == ()
    old = parse_import_file("old.csv", "기관명,부서명,URL,소스 설명\n기관,,https://example.org/a,설명".encode("utf-8"))
    assert old[0]["raw_collection_method"] == ""
    new = parse_import_file("new.csv", "기관명,URL,수집 방식\n기관,https://example.org/a,크롤링".encode("utf-8"))
    assert new[0]["raw_collection_method"] == "크롤링"


def test_excel_rejects_advanced_api_and_detects_canonical_method_conflict(db):
    session, _root = db
    api_rows = parse_import_file(
        "api.csv", "기관명,URL,수집 방식\n기관,https://example.org/api,API".encode("utf-8")
    )
    api_preview = SourceImportService(session).classify(api_rows)
    assert api_preview["rows"][0]["classification"] == "INVALID"
    mixed = parse_import_file(
        "mixed.csv",
        (
            "기관명,URL,수집 방식\n"
            "기관A,https://example.org/shared,스크래핑\n"
            "기관B,https://example.org/shared,크롤링\n"
        ).encode("utf-8"),
    )
    preview = SourceImportService(session).classify(mixed)
    assert preview["rows"][1]["classification"] == "CONFLICT"


def _login(client, username):
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    return client.post(
        "/login",
        data={"username": username, "password": "CorrectHorse12", "csrf_token": token},
        follow_redirects=False,
    )


def _csrf(client):
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text).group(1)


def test_credential_admin_boundary_csrf_and_no_secret_response(tmp_path, monkeypatch):
    database = tmp_path / "data" / "db" / "auth.sqlite3"
    database.parent.mkdir(parents=True)
    url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    monkeypatch.setenv("PUBLICDB2_SESSION_SECRET", "test-" + "x" * 64)
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(url, project_root=tmp_path)
    factory = create_session_factory(app.state.engine)
    with factory() as session:
        UserService(session).create(username="operator", password="CorrectHorse12", role=UserRole.OPERATOR)
        UserService(session).create(username="admin", password="CorrectHorse12", role=UserRole.ADMIN)
    with TestClient(app) as client:
        _login(client, "operator")
        assert client.post("/api/api-credentials", json={"secret_value": "hidden"}).status_code == 403
        assert client.post("/api/api-credentials", json={"secret_value": "hidden"}, headers={"X-CSRF-Token": _csrf(client)}).status_code == 403
    with TestClient(app) as client:
        _login(client, "admin")
        token = _csrf(client)
        saved = client.post("/api/api-credentials", json={"secret_value": "hidden"}, headers={"X-CSRF-Token": token})
        assert saved.status_code == 201
        assert "hidden" not in saved.text
        ref = saved.json()["credential_ref"]
        status = client.get(f"/api/api-credentials/{ref}")
        assert status.json()["configured"] is True and "hidden" not in status.text
        assert client.delete(f"/api/api-credentials/{ref}", headers={"X-CSRF-Token": token}).status_code == 200
    app.state.engine.dispose()


def test_api_preview_is_one_request_and_writes_no_evidence(db):
    session, root = db
    from tests.support import regression_app
    app = regression_app(
        session.get_bind().url.render_as_string(hide_password=False),
        project_root=root,
    )
    calls = []
    app.state.preview_fetcher_factory = lambda: HTTPFetcher(
        transport=httpx.MockTransport(lambda request: (
            calls.append(str(request.url))
            or httpx.Response(200, headers={"Content-Type": "application/json"}, content=b'{"items":[{"dept":"Data"}]}')
        )),
        resolver=PUBLIC_DNS,
    )
    before_runs = session.scalar(select(func.count()).select_from(CrawlRun))
    before_observations = session.scalar(select(func.count()).select_from(Observation))
    with TestClient(app) as client:
        response = client.post("/api/source-config/preview", json={
            "url": "https://example.org/api",
            "method_config": {
                "kind": "OPEN_API", "response_format": "JSON", "record_path": "items",
                "field_mapping": {"org_unit": "dept"}, "auth_mode": "NONE",
            },
        })
    assert response.status_code == 200 and response.json()["raw_count"] == 1
    assert len(calls) == 1
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(CrawlRun)) == before_runs
    assert session.scalar(select(func.count()).select_from(Observation)) == before_observations


def test_migration_materializes_existing_web_page_config(tmp_path, monkeypatch):
    database = tmp_path / "legacy-05a.sqlite3"
    url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "a5c105a05a01")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    source_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO sources "
            "(id, url, normalized_url, source_type, collection_method, data_format, coverage_mode, active, created_at, updated_at) "
            "VALUES (:id, :url, :url, 'GENERAL_PAGE', 'WEB_PAGE', 'HTML', 'UNKNOWN', 1, :now, :now)"
        ), {"id": source_id.hex, "url": "https://example.org/legacy", "now": utc_now()})
        assert connection.execute(text("SELECT id, collection_method FROM sources")).all() == [(source_id.hex, "WEB_PAGE")]
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    with factory() as session:
        migrated = session.scalar(select(SourceScrapeConfig))
        assert migrated is not None
        assert migrated.source_id == source_id
        assert migrated.extract_contacts and migrated.extract_directory
    engine.dispose()
