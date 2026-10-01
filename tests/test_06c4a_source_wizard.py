from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import Agency, AgencyType, CollectionJob, CollectionMethod, Source, SourceBinding
from tests.support import regression_app


@pytest.fixture()
def wizard_db(tmp_path, monkeypatch):
    path = tmp_path / "wizard.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield url, factory, tmp_path
    engine.dispose()


def seed_source(factory, method=CollectionMethod.WEB_PAGE):
    with factory() as session:
        agency = Agency(
            official_name="테스트 기관", normalized_name="테스트기관",
            agency_type=AgencyType.OTHER, region_code="SEOUL", active=True,
        )
        source = Source(
            url="https://example.org/directory", normalized_url="https://example.org/directory",
            title="직원 안내", collection_method=method, scheduled_refresh_enabled=True, active=True,
        )
        session.add_all([agency, source])
        session.flush()
        session.add(SourceBinding(
            source_id=source.id, agency_id=agency.id, org_unit_id=None,
            scope_key=f"agency:{agency.id}", description="기관 공통", active=True,
        ))
        session.commit()


def test_source_create_is_three_step_exclusive_wizard(wizard_db):
    url, _factory, root = wizard_db
    page = TestClient(regression_app(url, project_root=root)).get("/sources")
    assert page.status_code == 200
    soup = BeautifulSoup(page.text, "html.parser")
    modal = soup.select_one('[data-modal="source-create"]')
    form = modal.select_one("[data-source-wizard]")
    steps = form.select("[data-wizard-step]")
    assert len(steps) == 3
    assert not steps[0].has_attr("hidden")
    assert steps[1].has_attr("hidden") and steps[2].has_attr("hidden")
    assert len(form.select("[data-method-choice]")) == 3
    panels = form.select("[data-method-panel]")
    assert [panel["data-method-panel"] for panel in panels] == ["WEB_PAGE", "WEB_CRAWL", "API"]
    assert not panels[0].has_attr("hidden")
    assert panels[1].has_attr("hidden") and panels[2].has_attr("hidden")
    assert len(form.select(".auto-refresh-section")) == 1
    assert form.select_one('[data-api-subtype="FEED"]').has_attr("hidden")
    assert form.select_one("[data-credential-fields]").has_attr("hidden")
    assert form.select_one('[name="method_config.discovery_only"]').has_attr("checked")
    assert all(not details.has_attr("open") for details in form.select("details.advanced-settings"))
    assert "AUTO" not in [option.get("value") for option in form.select('[name="method_config.response_format"] option')]

    css = Path("app/web/static/css/workspace.css").read_text(encoding="utf-8")
    js = Path("app/web/static/js/app.js").read_text(encoding="utf-8")
    assert "[data-method-panel][hidden]" in css
    assert "panel.hidden = !active" in js
    assert "control.disabled = !active" in js
    assert '[name]:not(:disabled):not([data-ui-only])' in js
    assert 'data-job-all-open' in page.text and 'data-job-filtered-open' in page.text


def test_zero_database_and_zero_filter_are_distinct_and_actions_safe(wizard_db):
    url, factory, root = wizard_db
    client = TestClient(regression_app(url, project_root=root))
    empty = client.get("/sources")
    assert "등록된 수집 소스가 없습니다. 첫 수집 소스를 추가해 주세요." in empty.text
    empty_soup = BeautifulSoup(empty.text, "html.parser")
    assert empty_soup.select_one("[data-job-filtered-open]").has_attr("disabled")
    assert empty_soup.select_one("[data-job-all-open]").has_attr("disabled")
    assert empty_soup.select_one("[data-job-status]").has_attr("hidden")

    seed_source(factory)
    filtered = client.get("/sources", params={"methods": "API"})
    assert "현재 필터 조건에 맞는 수집 소스가 없습니다." in filtered.text
    filtered_soup = BeautifulSoup(filtered.text, "html.parser")
    assert filtered_soup.select_one("[data-job-filtered-open]").has_attr("disabled")
    assert not filtered_soup.select_one("[data-job-all-open]").has_attr("disabled")


def test_server_preview_and_confirm_create_exactly_one_job(wizard_db):
    url, factory, root = wizard_db
    seed_source(factory)
    client = TestClient(regression_app(url, project_root=root))
    filtered = client.get("/api/source-index/preview", params={"methods": "WEB_PAGE"})
    all_sources = client.get("/api/source-index/preview", params={"scope": "all"})
    assert filtered.status_code == 200 and filtered.json()["eligible_total"] == 1
    assert filtered.json()["method_counts"] == {"WEB_PAGE": 1}
    assert all_sources.status_code == 200 and all_sources.json()["eligible_total"] == 1
    assert all_sources.json()["scope"] == "all"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CollectionJob)) == 0
    created = client.post(
        "/api/collection-jobs", headers={"X-CSRF-Token": "test-csrf"},
        json={"trigger_type": "MANUAL_ALL"},
    )
    assert created.status_code == 202
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CollectionJob)) == 1


def test_edit_wizard_preloads_selected_method_and_keeps_other_panels_inactive(wizard_db):
    url, factory, root = wizard_db
    seed_source(factory, CollectionMethod.API)
    page = TestClient(regression_app(url, project_root=root)).get("/sources")
    soup = BeautifulSoup(page.text, "html.parser")
    edit = soup.select_one('[data-modal^="edit-"] [data-source-wizard]')
    assert edit.select_one("[data-method-selector]")["value"] == "API"
    assert edit.select_one('[data-method-choice][value="API"]').has_attr("checked")
    assert edit.select_one('[data-method-panel="API"]') is not None
    assert not edit.select_one('[data-method-panel="API"]').has_attr("hidden")
    assert edit.select_one('[data-method-panel="WEB_PAGE"]').has_attr("hidden")
    assert edit.select_one('[data-method-panel="WEB_CRAWL"]').has_attr("hidden")
    assert edit.select_one("[data-initial-method-config]") is not None
    js = Path("app/web/static/js/app.js").read_text(encoding="utf-8")
    assert "selectedPanel()?.querySelectorAll" in js
    assert "const control = selectedPanel()?.querySelector" in js


def test_source_table_and_heading_contracts_are_scoped(wizard_db):
    url, _factory, root = wizard_db
    client = TestClient(regression_app(url, project_root=root))
    sources = client.get("/sources").text
    assert "PublicDB2 DB" not in sources
    assert "<th>자동수집</th>" in sources
    assert "필터 결과 0개" in sources
    css = Path("app/web/static/css/workspace.css").read_text(encoding="utf-8")
    assert ".workspace-table--sources { width: 100%; min-width: 760px; table-layout: fixed; }" in css
    for route in ("/agencies", "/contacts", "/runs", "/review"):
        assert "PublicDB2 DB" not in client.get(route).text
