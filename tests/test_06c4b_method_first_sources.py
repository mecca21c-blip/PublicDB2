from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.collectors.http_fetcher import UnsafeRequestTarget
from app.db.base import Base
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    Agency, AgencyType, CollectionMethod, CrawlRun, OrgUnit, OrgUnitType,
    Source, SourceBinding, SourceCrawlConfig, SourceScrapeConfig,
)
from app.services.collection_service import CollectionService
from app.services.crawl_scope import crawl_path_allowed
from app.services.normalization import SourceURLValidationError, normalize_source_url
from app.services.scrape_batch_service import MAX_INTERACTIVE_SCRAPE_URLS, ScrapeBatchError, ScrapeBatchService
from app.services.source_service import SourceService, SourceServiceError
from tests.support import regression_app


@pytest.fixture()
def method_first_db(tmp_path, monkeypatch):
    path = tmp_path / "method-first.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield url, factory, tmp_path
    engine.dispose()


def add_agency(session, name: str) -> Agency:
    agency = Agency(
        official_name=name, normalized_name=name.replace(" ", ""),
        agency_type=AgencyType.OTHER, region_code="SEOUL", active=True,
    )
    session.add(agency)
    session.flush()
    return agency


def add_unit(session, agency: Agency, name: str) -> OrgUnit:
    unit = OrgUnit(
        agency_id=agency.id, name=name, normalized_name=name.replace(" ", ""),
        unit_type=OrgUnitType.DEPARTMENT, active=True,
    )
    session.add(unit)
    session.flush()
    return unit


def test_scrape_ten_lines_classify_and_partially_register_without_http(method_first_db, monkeypatch):
    url, factory, root = method_first_db
    with factory() as session:
        agency = add_agency(session, "일괄 대상 기관")
        other = add_agency(session, "기존 연결 기관")
        SourceService(session).register_binding(
            url="https://example.org/existing", agency_id=other.id,
            collection_method=CollectionMethod.WEB_PAGE,
        )
        agency_id = agency.id
    urls = "\n".join([
        "https://example.org/new-1", "https://example.org/new-2",
        "https://example.org/new-3", "https://example.org/new-4",
        "https://example.org/new-5", "https://example.org/new-6",
        "https://example.org/existing", "https://EXAMPLE.org:443/new-1#again",
        "", "not-a-url",
    ])
    def no_http(*_args, **_kwargs):
        raise AssertionError("registration preview/confirm must not perform HTTP")
    monkeypatch.setattr("app.collectors.http_fetcher.HTTPFetcher.fetch", no_http)
    client = TestClient(regression_app(url, project_root=root))
    payload = {
        "urls": urls, "agency_id": str(agency_id), "org_unit_id": None,
        "binding_scope": "AGENCY_WIDE", "description": "공통 설명",
        "method_config": {"extract_contacts": False, "extract_directory": True},
        "scheduled_refresh_enabled": False,
    }
    preview = client.post(
        "/api/source-bindings/scrape-batch/preview",
        headers={"X-CSRF-Token": "test-csrf"}, json=payload,
    )
    assert preview.status_code == 200
    summary = preview.json()["summary"]
    assert summary == {
        "input_lines": 10, "input_urls": 9, "new_sources": 6,
        "existing_sources": 1, "duplicate_input": 1, "invalid_urls": 1,
        "new_bindings": 7, "exact_binding_duplicates": 0, "conflicts": 0,
        "importable": 7, "blank_lines_ignored": 1,
    }
    rows = preview.json()["rows"]
    assert {row["classification"] for row in rows} == {
        "NEW_SOURCE", "EXISTING_SOURCE", "DUPLICATE_INPUT", "INVALID_URL",
    }
    invalid = next(row for row in rows if row["classification"] == "INVALID_URL")
    assert invalid["raw_url"] == "not-a-url" and invalid["message"]

    confirmed = client.post(
        "/api/source-bindings/scrape-batch/confirm",
        headers={"X-CSRF-Token": "test-csrf"}, json=payload,
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["summary"] == {
        "input": 9, "created_sources": 6, "existing_sources_reused": 1,
        "created_bindings": 7, "duplicates_skipped": 1, "errors": 1,
    }
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 7
        bindings = list(session.scalars(select(SourceBinding).where(SourceBinding.agency_id == agency_id)))
        assert len(bindings) == 7 and all(binding.org_unit_id is None for binding in bindings)
        configs = list(session.scalars(select(SourceScrapeConfig)))
        assert len(configs) == 7
        assert all(not config.extract_contacts and config.extract_directory for config in configs)
        sources = list(session.scalars(select(Source)))
        assert all(not source.scheduled_refresh_enabled for source in sources)
        assert session.scalar(select(func.count()).select_from(CrawlRun)) == 0


def test_scrape_batch_specific_org_scope_limit_and_cross_agency_revalidation(method_first_db):
    _url, factory, _root = method_first_db
    with factory() as session:
        agency = add_agency(session, "부서 대상 기관")
        unit = add_unit(session, agency, "정보 부서")
        other = add_agency(session, "다른 기관")
        other_unit = add_unit(session, other, "다른 부서")
        session.commit()
        result = ScrapeBatchService(session).confirm(
            urls="https://example.org/a\nhttps://example.org/b",
            agency_id=agency.id, org_unit_id=unit.id, binding_scope="SPECIFIC_ORG_UNIT",
            description=None, method_config={}, scheduled_refresh_enabled=True,
        )
        assert result["summary"]["created_bindings"] == 2
        bindings = list(session.scalars(select(SourceBinding).where(SourceBinding.agency_id == agency.id)))
        assert len(bindings) == 2 and all(binding.org_unit_id == unit.id for binding in bindings)
        with pytest.raises(SourceServiceError, match="지정한 기관"):
            ScrapeBatchService(session).preview(
                urls="https://example.org/c", agency_id=agency.id,
                org_unit_id=other_unit.id, binding_scope="SPECIFIC_ORG_UNIT",
            )
        with pytest.raises(ScrapeBatchError, match=str(MAX_INTERACTIVE_SCRAPE_URLS)):
            ScrapeBatchService(session).preview(
                urls="\n".join(f"https://example.org/{index}" for index in range(201)),
                agency_id=agency.id, org_unit_id=None, binding_scope="AGENCY_WIDE",
            )


def test_crawl_path_lists_are_exclude_first_and_legacy_path_remains_readable(method_first_db):
    _url, factory, _root = method_first_db
    with factory() as session:
        agency = add_agency(session, "크롤 기관")
        binding, _ = SourceService(session).register_binding(
            url="https://example.org/root/index", agency_id=agency.id,
            collection_method=CollectionMethod.WEB_CRAWL,
            method_config={
                "allowed_paths": ["/root", "/shared"],
                "excluded_paths": ["/root/private"],
                "max_depth": 2, "max_pages": 50, "request_delay_ms": 1000,
            },
        )
        source = session.get(Source, uuid.UUID(binding["source_id"]))
        assert source.crawl_config.allowed_path == "/root"
        assert source.crawl_config.allowed_paths == ["/root", "/shared"]
        assert source.crawl_config.excluded_paths == ["/root/private"]
        assert crawl_path_allowed("/root/team", source.crawl_config.allowed_paths, source.crawl_config.excluded_paths)
        assert crawl_path_allowed("/shared/team", source.crawl_config.allowed_paths, source.crawl_config.excluded_paths)
        assert not crawl_path_allowed("/outside", source.crawl_config.allowed_paths, source.crawl_config.excluded_paths)
        assert not crawl_path_allowed("/root/private/data", source.crawl_config.allowed_paths, source.crawl_config.excluded_paths)
        CollectionService._validate_scope(
            source.url, "https://example.org/root/team", "PATH_PREFIX",
            source.crawl_config.allowed_paths, source.crawl_config.excluded_paths,
        )
        with pytest.raises(UnsafeRequestTarget):
            CollectionService._validate_scope(
                source.url, "https://example.org/root/private/data", "PATH_PREFIX",
                source.crawl_config.allowed_paths, source.crawl_config.excluded_paths,
            )
        CollectionService._validate_scope(
            source.url, "https://example.org/outside", "SAME_DOMAIN",
            source.crawl_config.allowed_paths, source.crawl_config.excluded_paths,
        )
        with pytest.raises(UnsafeRequestTarget):
            CollectionService._validate_scope(
                source.url, "https://example.org/root/private/data", "SAME_DOMAIN",
                source.crawl_config.allowed_paths, source.crawl_config.excluded_paths,
            )
        legacy = Source(
            url="https://example.org/legacy/index", normalized_url="https://example.org/legacy/index",
            collection_method=CollectionMethod.WEB_CRAWL,
        )
        legacy.crawl_config = SourceCrawlConfig(allowed_path="/legacy", allowed_paths=[], excluded_paths=[])
        session.add(legacy); session.flush()
        projection = SourceService._method_projection(legacy)
        assert projection["allowed_paths"] == ["/legacy"] and projection["excluded_paths"] == []


def test_single_source_normalization_rejects_multiline_url():
    with pytest.raises(SourceURLValidationError, match="exactly one URL"):
        normalize_source_url("https://example.org/one\nhttps://example.org/two")


def test_rss_direct_registration_is_one_source_and_does_not_fetch(method_first_db, monkeypatch):
    url, factory, root = method_first_db
    with factory() as session:
        agency = add_agency(session, "RSS 등록 기관")
        session.commit()
        agency_id = agency.id

    def no_http(*_args, **_kwargs):
        raise AssertionError("source registration must not perform HTTP")

    monkeypatch.setattr("app.collectors.http_fetcher.HTTPFetcher.fetch", no_http)
    response = TestClient(regression_app(url, project_root=root)).post(
        "/api/source-bindings",
        headers={"X-CSRF-Token": "test-csrf"},
        json={
            "agency_id": str(agency_id), "org_unit_id": None,
            "url": "https://example.org/feed.xml", "description": None,
            "collection_method": "API", "scheduled_refresh_enabled": True,
            "method_config": {
                "kind": "RSS", "response_format": "XML", "auth_mode": "NONE",
                "field_mapping": {}, "static_params": {}, "discovery_only": True,
            },
        },
    )
    assert response.status_code == 201
    with factory() as session:
        sources = list(session.scalars(select(Source)))
        assert len(sources) == 1
        assert sources[0].url == "https://example.org/feed.xml"
        assert sources[0].api_config.kind.value == "RSS"
        assert session.scalar(select(func.count()).select_from(CrawlRun)) == 0


def test_method_first_wizard_dom_and_api_editors(method_first_db):
    url, _factory, root = method_first_db
    response = TestClient(regression_app(url, project_root=root)).get("/sources")
    assert response.status_code == 200
    form = BeautifulSoup(response.text, "html.parser").select_one('[data-modal="source-create"] [data-source-wizard]')
    steps = form.select("[data-wizard-step]")
    assert [step["data-wizard-kind"] for step in steps] == ["row-basic", "config", "review"]
    assert not steps[0].has_attr("hidden") and steps[1].has_attr("hidden") and steps[2].has_attr("hidden")
    assert [card.strong.get_text(strip=True) for card in form.select(".method-card")] == [
        "개별 URL · 스크래핑", "Index URL · 크롤링", "공개 API / RSS",
    ]
    assert steps[0].select_one('[name="agency_id"]') is None
    assert form.select_one("[data-source-intake]")
    assert form.select_one('[data-wizard-step="2"] textarea[name="urls"]') is None
    assert form.select_one('[data-method-panel="WEB_PAGE"] input[name="url"]') is None
    assert len(form.select('[data-method-panel="WEB_CRAWL"] [data-single-url]')) == 0
    assert form.select_one('[name="method_config.allowed_paths"][data-lines-field]')
    assert form.select_one('[name="method_config.excluded_paths"][data-lines-field]')
    assert form.select_one("[data-static-params-json]")["type"] == "hidden"
    assert form.select_one("[data-param-add]")
    assert len(form.select(".mapping-table input")) == 8
    assert form.select_one("[data-pagination-fields]").has_attr("hidden")
    assert form.select_one('[data-api-subtype="FEED"] .mapping-table') is None
    assert form.select_one("[data-wizard-prev]").has_attr("hidden")
    assert not form.select_one("[data-wizard-next]").has_attr("hidden")
    assert form.select_one("[data-wizard-submit]").has_attr("hidden")
    assert form.select_one("[data-row-org]")

    js = Path("app/web/static/js/app.js").read_text(encoding="utf-8")
    assert "syncStaticParams" in js and "dataset.linesField" in js
    assert 'control.disabled = !active' in js
    assert 'trigger_type: "MANUAL_SELECTION"' in js


def test_06c4b_migration_prior_upgrade_and_metadata(tmp_path, monkeypatch):
    config = Config("alembic.ini")
    url = f"sqlite:///{(tmp_path / 'migration.sqlite3').as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(config, "a0c406c40001")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    with factory() as session:
        agency = add_agency(session, "이전 버전 기관")
        source = Source(
            url="https://example.org/legacy/path", normalized_url="https://example.org/legacy/path",
            collection_method=CollectionMethod.WEB_CRAWL,
        )
        session.add(source); session.flush()
        session.execute(text("""
            INSERT INTO source_crawl_configs (
                id, source_id, scope, allowed_path, max_depth, max_pages,
                request_delay_ms, extract_contacts, extract_directory,
                created_at, updated_at
            ) VALUES (
                :id, :source_id, 'PATH_PREFIX', '/legacy', 2, 50,
                1000, 1, 1, :created_at, :updated_at
            )
        """), {
            "id": uuid.uuid4().hex,
            "source_id": source.id.hex,
            "created_at": source.created_at,
            "updated_at": source.updated_at,
        })
        session.commit()
    engine.dispose()
    command.upgrade(config, "head")
    upgraded = create_db_engine(url)
    with upgraded.connect() as connection:
        row = connection.exec_driver_sql("SELECT allowed_path, allowed_paths, excluded_paths FROM source_crawl_configs").one()
        assert row[0] == "/legacy" and row[1] == '["/legacy"]' and row[2] == "[]"
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    upgraded.dispose()
