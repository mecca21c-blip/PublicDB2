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
from app.models import AgencyType, CollectionMethod, Observation, RunStatus, Source
from app.services.agency_service import AgencyService
from app.services.collection_service import CollectionCoordinator, CollectionService
from app.services.robots_policy import MAX_CRAWL_DELAY_SECONDS, RobotsPolicy
from app.services.source_service import SourceService


PUBLIC_DNS = lambda _host: {"93.184.216.34"}
DIRECTORY = b"""<table><tr><th>Department</th><th>Duty</th><th>Phone</th></tr>
<tr><td>Health</td><td>Inspection</td><td>02-2600-5861</td></tr></table>"""


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


def add_crawl_source(session, url: str, config: dict | None = None) -> Source:
    agency, _ = AgencyService(session).create_agency(
        official_name=f"Agency-{uuid.uuid4()}", agency_type=AgencyType.OTHER,
    )
    binding, _ = SourceService(session).register_binding(
        url=url, agency_id=uuid.UUID(agency["id"]), collection_method=CollectionMethod.WEB_CRAWL,
        method_config=config or {
            "scope": "PATH_PREFIX", "allowed_path": "/", "max_depth": 1,
            "max_pages": 3, "request_delay_ms": 1000,
        },
    )
    return session.get(Source, uuid.UUID(binding["source_id"]))


def collector(session, root: Path, handler, *, sleeper=lambda _seconds: None) -> CollectionService:
    return CollectionService(
        session, project_root=root, raw_root=root / "data" / "raw",
        coordinator=CollectionCoordinator(), sleeper=sleeper,
        fetcher=HTTPFetcher(transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS),
    )


def test_rfc9309_most_specific_rule_and_allow_tie_win():
    policy = RobotsPolicy.parse("""User-agent: *
Disallow: /
Allow: /health
Allow: /env
Disallow: /env/env030101/
Allow: /same
Disallow: /same
""")
    assert policy.allows("https://example.org/health/a") is True
    assert policy.allows("https://example.org/foo") is False
    assert policy.allows("https://example.org/env/page") is True
    assert policy.allows("https://example.org/env/env030101/") is False
    assert policy.allows("https://example.org/env/env030101/a") is False
    assert policy.allows("https://example.org/same") is True
    assert policy.allows("https://example.org/unmatched") is False  # root disallow still matches
    assert RobotsPolicy.parse("User-agent: *\nDisallow: /private").allows("https://example.org/public") is True


def test_rfc9309_product_group_wildcards_anchor_and_percent_normalization():
    document = """User-agent: GPTBot
Disallow: /
User-agent: PublicDB2
Disallow: /private/*
Allow: /private/public$
Allow: /caf%C3%A9
User-agent: *
Disallow: /wildcard-only
"""
    policy = RobotsPolicy.parse(document)
    assert policy.selected_group == "EXACT"
    assert policy.allows("https://example.org/private/public") is True
    assert policy.allows("https://example.org/private/public/child") is False
    assert policy.allows("https://example.org/caf%C3%A9") is True
    assert policy.allows("https://example.org/café") is True
    assert policy.allows("https://example.org/wildcard-only") is True

    combined = RobotsPolicy.parse("""User-agent: PublicDB2
Disallow: /combined
User-agent: *
Disallow: /
User-agent: publicdb2
Allow: /combined/public
""")
    assert combined.selected_group == "EXACT"
    assert combined.allows("https://example.org/combined/private") is False
    assert combined.allows("https://example.org/combined/public") is True
    assert combined.allows("https://example.org/not-in-wildcard") is True

    empty_exact = RobotsPolicy.parse("""User-agent: PublicDB2
Sitemap: https://example.org/sitemap.xml
User-agent: *
Disallow: /
""")
    assert empty_exact.selected_group == "EXACT"
    assert empty_exact.allows("https://example.org/anything") is True


def test_pre_wrapper_group_selection_and_bounded_crawl_delay():
    wrapped = """<pre>User-agent: GPTBot
Disallow: /

User-agent: *
Disallow: /
Allow: /health
Crawl-delay: 10
</pre>"""
    policy = RobotsPolicy.parse(wrapped)
    assert policy.selected_group == "WILDCARD"
    assert policy.allows("https://www.gangseo.seoul.kr/health/ht060301") is True
    assert policy.crawl_delay_seconds == 10
    assert RobotsPolicy.parse("User-agent: *\nCrawl-delay: invalid").crawl_delay_seconds is None
    assert RobotsPolicy.parse(
        f"User-agent: *\nCrawl-delay: {MAX_CRAWL_DELAY_SECONDS + 1}"
    ).crawl_delay_seconds is None


def test_gangseo_pre_wrapped_policy_crawl_integration(db):
    session, root = db
    source = add_crawl_source(session, "https://www.gangseo.seoul.kr/health/ht060301", {
        "scope": "PATH_PREFIX",
        "allowed_paths": ["/health/ht060301", "/health/ht060302", "/health/ht060303"],
        "excluded_paths": ["/health/ht060304"],
        "max_depth": 1, "max_pages": 3, "request_delay_ms": 1000,
        "extract_contacts": True, "extract_directory": True,
    })
    robots = b"""<pre>User-agent: GPTBot
User-agent: Applebot
User-agent: BacklinkDiscovery/1.0 (public HTML discovery)
Disallow: /

User-agent: *
Disallow: /
Allow: /$
Allow: /index
Allow: /health
Allow: /kids
Allow: /munhwa
Allow: /mayor
Allow: /eng
Allow: /eco
Allow: /edu
Allow: /welfare
Allow: /build
Allow: /env
Disallow: /env/env030101/
Allow: /smart
Allow: /reserve
Allow: /covid19
Crawl-delay: 10</pre>"""
    calls: list[str] = []
    delays: list[float] = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, content=robots)
        links = b""
        if request.url.path.endswith("ht060301"):
            links = (
                b'<a href="/health/ht060302">302</a>'
                b'<a href="/health/ht060303">303</a>'
                b'<a href="/health/ht060304">304</a>'
            )
        return httpx.Response(
            200, headers={"Content-Type": "text/html"},
            content=b"<html><body>" + links + DIRECTORY + b"</body></html>",
        )

    result = collector(session, root, handler, sleeper=delays.append).collect(source.id)
    stats = result.crawl_run.collection_statistics
    assert result.crawl_run.status is RunStatus.SUCCESS
    assert calls.count("/robots.txt") == 1
    assert calls.count("/health/ht060301") == 1
    assert calls.count("/health/ht060302") == 1
    assert calls.count("/health/ht060303") == 1
    assert calls.count("/health/ht060304") == 0
    assert stats["pages_attempted"] == 3
    assert stats["pages_succeeded"] == 3
    assert stats["robots_fetch_count"] == 1
    assert stats["configured_request_delay_ms"] == 1000
    assert stats["robots_crawl_delay_ms"] == 10000
    assert stats["effective_request_delay_ms"] == 10000
    assert delays == [10.0, 10.0]
    assert session.scalar(select(func.count()).select_from(Observation).where(
        Observation.crawl_run_id == result.crawl_run.id
    )) == 3


def test_robots_unavailable_denial_and_fetch_failure_are_distinct(db):
    session, root = db
    for status in (404, 410):
        source = add_crawl_source(session, f"https://status-{status}.example.org/root")
        calls: list[str] = []

        def unavailable(request, response_status=status):
            calls.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(response_status)
            return httpx.Response(200, headers={"Content-Type": "text/html"}, content=DIRECTORY)

        result = collector(session, root, unavailable).collect(source.id)
        assert result.crawl_run.status is RunStatus.SUCCESS
        assert calls.count("/root") == 1

    denied = add_crawl_source(session, "https://denied.example.org/private")
    denied_calls: list[str] = []

    def deny(request):
        denied_calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"User-agent: *\nDisallow: /")
        raise AssertionError("explicitly denied seed must not be fetched")

    denied_result = collector(session, root, deny).collect(denied.id)
    assert denied_result.crawl_run.status is RunStatus.FAILED
    assert denied_result.crawl_run.error_summary == "robots.txt 정책에 의해 시작 URL 수집이 허용되지 않습니다."
    assert denied_result.crawl_run.collection_statistics["error_code"] == "ROBOTS_DISALLOWED"
    assert denied_calls == ["/robots.txt"]

    failed = add_crawl_source(session, "https://failure.example.org/root")
    failure_calls: list[str] = []

    def fail_closed(request):
        failure_calls.append(request.url.path)
        return httpx.Response(503)

    failed_result = collector(session, root, fail_closed).collect(failed.id)
    assert failed_result.crawl_run.status is RunStatus.FAILED
    assert failed_result.crawl_run.collection_statistics["error_code"] == "ROBOTS_FETCH_FAILED"
    assert failed_result.crawl_run.error_summary.startswith("robots.txt를 가져오지 못했습니다:")
    assert failure_calls == ["/robots.txt"]
