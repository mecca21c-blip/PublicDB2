from __future__ import annotations

import json
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
    CollectionMethod,
    ContactPoint,
    Duty,
    ExtractedFeedItem,
    Observation,
    Person,
    PersonAssignment,
    RunStatus,
    SourceOccurrence,
)
from app.services.api_credential_store import ApiCredentialStore
from app.services.semantic_discovery_service import SemanticDiscoveryProjector
from tests.support import regression_app
from tests.test_05b_three_way import add_source, collector


PUBLIC_DNS = lambda _host: {"93.184.216.34"}


@pytest.fixture()
def runtime_db(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    with factory() as session:
        yield session, tmp_path
    engine.dispose()


def _rss_fixture(count: int = 6) -> bytes:
    items = "".join(
        f"<item><guid>{index}</guid><title>Feed {index}</title>"
        f"<link>https://example.org/feed/{index}</link>"
        f"<description>Public notice {index}</description></item>"
        for index in range(1, count + 1)
    )
    return f"<rss><channel>{items}</channel></rss>".encode()


def _json_items(*items: dict) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        content=json.dumps({"items": list(items)}).encode(),
    )


def test_http_fetcher_preserves_registered_query_when_runtime_params_are_empty():
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, headers={"Content-Type": "application/rss+xml"}, content=b"<rss />")

    HTTPFetcher(
        transport=httpx.MockTransport(handler),
        resolver=PUBLIC_DNS,
    ).fetch(
        "https://data.seoul.go.kr/rss/rssView.do?searchType=0000",
        params={},
    )

    assert requested_urls == [
        "https://data.seoul.go.kr/rss/rssView.do?searchType=0000"
    ]


def test_http_fetcher_overlays_params_preserving_multivalue_encoding_and_blank_values():
    queries: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.query)
        return httpx.Response(200, content=b"ok")

    HTTPFetcher(
        transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS,
    ).fetch(
        "https://example.org/data?category=a&category=b&lang=%ED%95%9C%EA%B8%80"
        "&space=a+b&encoded=%2Fvalue%25&blank=&page=99#section",
        params={"page": 2, "format": "json data"},
    )

    assert queries == [
        b"category=a&category=b&lang=%ED%95%9C%EA%B8%80&space=a+b"
        b"&encoded=%2Fvalue%25&blank=&page=2&format=json+data"
    ]


def test_redirect_uses_server_location_without_reapplying_initial_params():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://example.org/final?server=1"})
        return httpx.Response(200, content=b"ok")

    result = HTTPFetcher(
        transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS,
    ).fetch("https://example.org/start?service=health", params={"format": "json"})

    assert requested == [
        "https://example.org/start?service=health&format=json",
        "https://example.org/final?server=1",
    ]
    assert result.final_url == "https://example.org/final?server=1"


def test_rss_runtime_preserves_real_query_extracts_six_and_remains_discovery_only(runtime_db):
    session, root = runtime_db
    registered_url = "https://data.seoul.go.kr/rss/rssView.do?searchType=0000"
    source = add_source(session, CollectionMethod.API, {
        "kind": "RSS", "auth_mode": "NONE", "static_params": {},
        "pagination_mode": "NONE",
    }, registered_url)
    actual_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        actual_queries.append(request.url.query.decode())
        if request.url.params.get("searchType") == "0000":
            return httpx.Response(
                200, headers={"Content-Type": "application/rss+xml"},
                content=_rss_fixture(),
            )
        return httpx.Response(
            200, headers={"Content-Type": "text/html"},
            content=b"<html><body>not a feed</body></html>",
        )

    before = {
        model: session.scalar(select(func.count()).select_from(model))
        for model in (Person, PersonAssignment, Duty, ContactPoint)
    }
    result = collector(session, root, handler).collect(source.id)
    after = {
        model: session.scalar(select(func.count()).select_from(model))
        for model in before
    }

    assert source.url == registered_url
    assert actual_queries == ["searchType=0000"]
    assert result.crawl_run.status is RunStatus.SUCCESS
    assert result.crawl_run.connection_status.value == "SUCCESS"
    assert result.crawl_run.raw_status.value == "SUCCESS"
    assert result.crawl_run.extraction_status.value == "SUCCESS"
    assert result.crawl_run.collection_statistics == {
        "requests_attempted": 1,
        "requests_succeeded": 1,
        "raw_records": 6,
        "mapped_records": 6,
        "feed_items": 6,
    }
    assert result.observation is not None
    assert result.observation.page_url == registered_url
    assert result.observation.final_url == registered_url
    assert session.scalar(
        select(func.count()).select_from(ExtractedFeedItem)
        .where(ExtractedFeedItem.observation_id == result.observation.id)
    ) == 6
    discovery = SemanticDiscoveryProjector(session).summary(result.crawl_run.id)
    assert discovery["feed_items"] == 6
    assert discovery["semantic_discovery_count"] == 6
    assert after == before


def test_openapi_base_query_static_pagination_and_same_key_override(runtime_db):
    session, root = runtime_db
    static_source = add_source(session, CollectionMethod.API, {
        "kind": "OPEN_API", "response_format": "JSON", "record_path": "items",
        "field_mapping": {}, "discovery_only": True,
        "static_params": {"format": "json"}, "auth_mode": "NONE",
    }, "https://api.example.go.kr/data?service=health&lang=ko")
    static_queries: list[list[tuple[str, str]]] = []
    static_result = collector(
        session, root,
        lambda request: (
            static_queries.append(list(request.url.params.multi_items()))
            or _json_items({"record": 1})
        ),
    ).collect(static_source.id)

    paged_source = add_source(session, CollectionMethod.API, {
        "kind": "OPEN_API", "response_format": "JSON", "record_path": "items",
        "field_mapping": {}, "discovery_only": True,
        "pagination_mode": "PAGE_NUMBER", "page_parameter": "page",
        "start_page": 1, "max_pages": 3, "request_delay_ms": 500,
        "auth_mode": "NONE",
    }, "https://api.example.go.kr/data?page=99&service=health")
    paged_queries: list[list[tuple[str, str]]] = []
    paged_result = collector(
        session, root,
        lambda request: (
            paged_queries.append(list(request.url.params.multi_items()))
            or _json_items({"record": request.url.params["page"]})
        ),
    ).collect(paged_source.id)

    assert static_result.crawl_run.status is RunStatus.SUCCESS
    assert static_queries == [[
        ("service", "health"), ("lang", "ko"), ("format", "json"),
    ]]
    assert paged_result.crawl_run.status is RunStatus.SUCCESS
    assert paged_queries == [
        [("service", "health"), ("page", "1")],
        [("service", "health"), ("page", "2")],
        [("service", "health"), ("page", "3")],
    ]


def test_query_and_header_api_keys_preserve_query_and_redact_secret(runtime_db):
    session, root = runtime_db
    store = ApiCredentialStore(Path(root) / "config")
    secret = "RUNTIME-TOP-SECRET"
    credential_ref = store.save(secret)["credential_ref"]
    query_source = add_source(session, CollectionMethod.API, {
        "kind": "OPEN_API", "response_format": "JSON", "record_path": "items",
        "field_mapping": {}, "discovery_only": True,
        "auth_mode": "QUERY_API_KEY", "credential_ref": credential_ref,
        "credential_name": "key",
    }, "https://api.example.go.kr/data?service=health")
    sent_query: list[list[tuple[str, str]]] = []
    query_result = collector(
        session, root,
        lambda request: (
            sent_query.append(list(request.url.params.multi_items()))
            or _json_items({"record": 1})
        ),
        credential_store=store,
    ).collect(query_source.id)
    failed_query_result = collector(
        session, root,
        lambda _request: httpx.Response(503),
        credential_store=store,
    ).collect(query_source.id)

    header_source = add_source(session, CollectionMethod.API, {
        "kind": "OPEN_API", "response_format": "JSON", "record_path": "items",
        "field_mapping": {}, "discovery_only": True,
        "auth_mode": "HEADER_API_KEY", "credential_ref": credential_ref,
        "credential_name": "X-Api-Key",
    }, "https://api.example.go.kr/header?service=health")
    header_evidence: list[tuple[str | None, str]] = []
    header_result = collector(
        session, root,
        lambda request: (
            header_evidence.append((request.headers.get("X-Api-Key"), str(request.url)))
            or _json_items({"record": 1})
        ),
        credential_store=store,
    ).collect(header_source.id)

    assert query_result.crawl_run.status is RunStatus.SUCCESS
    assert sent_query == [[("service", "health"), ("key", secret)]]
    assert query_result.observation is not None
    assert query_result.observation.final_url == "https://api.example.go.kr/data?service=health"
    persisted_query_values = [
        query_source.url,
        query_result.observation.page_url,
        query_result.observation.final_url,
        query_result.crawl_run.error_summary or "",
        failed_query_result.crawl_run.error_summary or "",
        json.dumps(query_result.crawl_run.collection_config_snapshot),
        json.dumps(failed_query_result.crawl_run.collection_config_snapshot),
        json.dumps(query_result.crawl_run.collection_statistics),
        json.dumps(failed_query_result.crawl_run.collection_statistics),
        "".join(repr(item.__dict__) for item in session.scalars(select(SourceOccurrence))),
    ]
    assert failed_query_result.crawl_run.status is RunStatus.FAILED
    assert sum(secret in value for value in persisted_query_values) == 0
    assert header_result.crawl_run.status is RunStatus.SUCCESS
    assert header_evidence == [
        (secret, "https://api.example.go.kr/header?service=health"),
    ]


def test_preview_and_runtime_share_identical_query_semantics(runtime_db):
    session, root = runtime_db
    url = "https://api.example.go.kr/data?service=health&category=a&category=b"
    config = {
        "kind": "OPEN_API", "response_format": "JSON", "record_path": "items",
        "field_mapping": {}, "discovery_only": True,
        "static_params": {"format": "json"}, "auth_mode": "NONE",
    }
    source = add_source(session, CollectionMethod.API, config, url)
    seen: list[tuple[str, bytes]] = []

    def preview_handler(request: httpx.Request) -> httpx.Response:
        seen.append(("preview", request.url.query))
        return _json_items({"record": 1})

    app = regression_app(
        session.get_bind().url.render_as_string(hide_password=False),
        project_root=root,
    )
    app.state.preview_fetcher_factory = lambda: HTTPFetcher(
        transport=httpx.MockTransport(preview_handler), resolver=PUBLIC_DNS,
    )
    with TestClient(app) as client:
        preview = client.post("/api/source-config/preview", json={
            "url": url, "method_config": config,
        })
    assert preview.status_code == 200

    def runtime_handler(request: httpx.Request) -> httpx.Response:
        seen.append(("runtime", request.url.query))
        return _json_items({"record": 1})

    runtime = collector(session, root, runtime_handler).collect(source.id)
    assert runtime.crawl_run.status is RunStatus.SUCCESS
    assert seen == [
        ("preview", b"service=health&category=a&category=b&format=json"),
        ("runtime", b"service=health&category=a&category=b&format=json"),
    ]
