from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse

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
    assert len(form.select("[data-source-wizard-scroll]")) == 1
    scroll_region = form.select_one("[data-source-wizard-scroll]")
    actions = form.select_one(".source-wizard-actions")
    assert scroll_region is not None and scroll_region.parent is form
    assert actions is not None and actions.parent is form
    assert actions not in scroll_region.select(".source-wizard-actions")
    crawl_advanced = scroll_region.select_one(
        '[data-method-panel="WEB_CRAWL"] details.advanced-settings'
    )
    assert crawl_advanced is not None
    for field in (
        "scope", "allowed_paths", "excluded_paths", "max_depth",
        "max_pages", "request_delay_ms",
    ):
        assert crawl_advanced.select_one(f'[name="method_config.{field}"]') is not None
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
    assert not actions.select_one("[data-modal-close]").has_attr("hidden")
    assert actions.select_one("[data-wizard-prev]").has_attr("hidden")
    assert not actions.select_one("[data-wizard-next]").has_attr("hidden")
    assert actions.select_one("[data-wizard-submit]").has_attr("hidden")
    assert ".source-wizard-actions .button[hidden] { display: none; }" in css


def test_zero_database_and_zero_filter_are_distinct_and_actions_safe(wizard_db):
    url, factory, root = wizard_db
    client = TestClient(regression_app(url, project_root=root))
    empty = client.get("/sources")
    assert "등록된 수집 소스가 없습니다." in empty.text
    assert "수집 소스 추가에서 첫 소스를 등록하세요." in empty.text
    empty_soup = BeautifulSoup(empty.text, "html.parser")
    assert empty_soup.select_one("[data-job-filtered-open]").has_attr("disabled")
    assert empty_soup.select_one("[data-job-all-open]").has_attr("disabled")
    assert empty_soup.select_one("[data-job-status]").has_attr("hidden")

    seed_source(factory)
    filtered = client.get("/sources", params={"methods": "API"})
    assert "현재 조건에 맞는 수집 소스가 없습니다." in filtered.text
    assert "필터 조건을 변경하거나 초기화해 보세요." in filtered.text
    filtered_soup = BeautifulSoup(filtered.text, "html.parser")
    assert filtered_soup.select_one("[data-job-filtered-open]").has_attr("disabled")
    assert not filtered_soup.select_one("[data-job-all-open]").has_attr("disabled")


def test_server_preview_cancel_and_each_confirmation_create_one_job(wizard_db):
    url, factory, root = wizard_db
    seed_source(factory)
    client = TestClient(regression_app(url, project_root=root))
    page = BeautifulSoup(client.get("/sources").text, "html.parser")
    for name in ("filtered-job-confirm", "all-job-confirm"):
        modal = page.select_one(f'[data-modal="{name}"]')
        assert modal.select_one("[data-bulk-total]")
        assert modal.select_one("[data-bulk-web-page]")
        assert modal.select_one("[data-bulk-web-crawl]")
        assert modal.select_one("[data-bulk-api]")
        assert modal.select_one("[data-job-filter-confirm], [data-job-all-confirm]").get_text(strip=True) == "수집 시작"
    filtered = client.get("/api/source-index/preview", params={"methods": "WEB_PAGE"})
    all_sources = client.get("/api/source-index/preview", params={"scope": "all"})
    assert filtered.status_code == 200 and filtered.json()["eligible_total"] == 1
    assert filtered.json()["method_counts"] == {"WEB_PAGE": 1}
    assert all_sources.status_code == 200 and all_sources.json()["eligible_total"] == 1
    assert all_sources.json()["scope"] == "all"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CollectionJob)) == 0
    filtered_created = client.post(
        "/api/collection-jobs", headers={"X-CSRF-Token": "test-csrf"},
        json={"trigger_type": "MANUAL_FILTER", "filter": {"methods": ["WEB_PAGE"]}},
    )
    assert filtered_created.status_code == 202
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CollectionJob)) == 1
    all_created = client.post(
        "/api/collection-jobs", headers={"X-CSRF-Token": "test-csrf"},
        json={"trigger_type": "MANUAL_ALL"},
    )
    assert all_created.status_code == 202
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CollectionJob)) == 2


def test_source_wizard_layout_and_copy_contracts(wizard_db):
    url, _factory, root = wizard_db
    page = TestClient(regression_app(url, project_root=root)).get("/sources")
    soup = BeautifulSoup(page.text, "html.parser")
    form = soup.select_one('[data-modal="source-create"] [data-source-wizard]')
    assert "허용 경로 prefix" not in page.text
    assert "제외 경로 prefix" not in page.text
    assert "포함할 경로" in page.text and "제외할 경로" in page.text
    assert "시작 URL 경로 안에서 최대 깊이 2" in page.text
    assert form.select_one(".crawl-number-grid")
    assert all(not item.has_attr("open") for item in form.select("details.advanced-settings"))
    assert form.select_one('[data-api-add-mode][value="CATALOG"]').has_attr("disabled")
    assert "현재 검증된 기본 공개 소스가 없습니다. 직접 추가를 이용하세요." in page.text
    feed = form.select_one('[data-api-subtype="FEED"]')
    assert feed.select_one("details.advanced-settings") is None
    assert feed.select_one('[name="method_config.auth_mode"]') is None

    css = Path("app/web/static/css/workspace.css").read_text(encoding="utf-8")
    js = Path("app/web/static/js/app.js").read_text(encoding="utf-8")
    assert ".source-create-form { min-height: 0; flex: 1 1 auto; display: flex; flex-direction: column;" in css
    assert ".source-wizard-scroll-region { min-height: 0; flex: 1 1 auto;" in css
    assert "overflow-y: auto; overscroll-behavior: contain;" in css
    assert ".source-create-form .source-wizard-actions { min-height: 71px; flex: 0 0 auto;" in css
    assert ".source-create-form .source-wizard-actions {" in css
    assert "position: sticky" not in css.split(".source-create-form .source-wizard-actions", 1)[1].split("}", 1)[0]
    assert ".source-wizard-step { display: grid; gap: 12px; min-height: 0;" in css
    assert "[data-job-status][hidden] { display: none; }" in css
    assert 'form.querySelector("[data-wizard-prev]").hidden = step === 1' in js
    assert 'form.querySelector("[data-wizard-next]").hidden = step === 3' in js
    assert "submit.hidden = step !== 3" in js
    assert 'const region = form.querySelector("[data-source-wizard-scroll]")' in js
    assert "region.scrollTop = 0" in js
    assert 'details.addEventListener("toggle"' in js
    assert 'confirm.textContent = "수집 시작"' in js


def test_source_wizard_structured_api_errors_never_render_object_object(wizard_db):
    url, _factory, root = wizard_db
    client = TestClient(regression_app(url, project_root=root))
    page = client.get("/sources")
    assert page.status_code == 200
    assert "[object Object]" not in page.text

    soup = BeautifulSoup(page.text, "html.parser")
    create = soup.select_one('[data-modal="source-create"] [data-source-wizard]')
    assert create is not None
    assert [item["value"] for item in create.select("[data-method-choice]")] == [
        "WEB_PAGE", "WEB_CRAWL", "API",
    ]
    assert len(create.select("[data-wizard-step]")) == 3
    assert create.select_one('[data-method-panel="WEB_CRAWL"] details.advanced-settings')
    assert create.select_one(".source-wizard-actions").parent is create

    invalid = client.post(
        "/api/source-bindings/interactive/preview",
        headers={"X-CSRF-Token": "test-csrf"},
        json={},
    )
    assert invalid.status_code == 422
    payload = invalid.json()
    assert isinstance(payload.get("detail"), list)
    assert isinstance(payload["detail"][0], dict)

    js = Path("app/web/static/js/app.js").read_text(encoding="utf-8")
    start = js.index("  function sourceWizardErrorMessage")
    end = js.index("\n\n  const activateDetail", start)
    helper = js[start:end]
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Source Wizard JavaScript regression contract")
    script = (
        helper
        + "\nprocess.stdout.write(sourceWizardErrorMessage("
        + json.dumps(payload, ensure_ascii=False)
        + ", '입력값을 확인하세요.'));"
    )
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.stdout
    assert "[object Object]" not in result.stdout
    assert "rows" in result.stdout
    assert 'sourceWizardErrorMessage(preview, "등록 행을 확인하지 못했습니다.")' in js
    assert 'form.querySelector(".source-wizard-actions").hidden = true' in js
    assert 'form.querySelector(".source-wizard-actions").hidden = false' in js


def test_source_wizard_runs_in_headless_browser_without_object_render(wizard_db):
    url, factory, tmp_path = wizard_db
    seed_source(factory, CollectionMethod.API)
    page = TestClient(regression_app(url, project_root=tmp_path)).get("/sources")
    assert page.status_code == 200

    browser = next((candidate for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ) if candidate.exists()), None)
    if browser is None:
        pytest.skip("Chrome or Edge is required for the Source Wizard browser contract")

    soup = BeautifulSoup(page.text, "html.parser")
    for link in list(soup.select('link[rel="stylesheet"]')):
        static_path = urlparse(link["href"]).path
        if not static_path.startswith("/static/"):
            continue
        asset = Path("app/web/static") / static_path.removeprefix("/static/")
        style = soup.new_tag("style")
        style.string = asset.read_text(encoding="utf-8")
        link.replace_with(style)
    app_js = Path("app/web/static/js/app.js").read_text(encoding="utf-8")
    for script in list(soup.select('script[src$="/static/js/app.js"]')):
        inline = soup.new_tag("script")
        inline.string = app_js
        script.replace_with(inline)
    harness = soup.new_tag("script")
    harness.string = r"""
      setTimeout(() => {
        const modal = document.querySelector('[data-modal="source-create"]');
        modal.hidden = false;
        const form = modal.querySelector('[data-source-wizard]');
        const steps = [...form.querySelectorAll('[data-wizard-step]')];
        steps.forEach((item, index) => { item.hidden = index !== 1; });
        const modes = {};
        for (const method of ['WEB_PAGE', 'WEB_CRAWL', 'API']) {
          const choice = form.querySelector('[data-method-choice][value="' + method + '"]');
          choice.checked = true;
          choice.dispatchEvent(new Event('change', {bubbles: true}));
          modes[method] = !form.querySelector('[data-method-panel="' + method + '"]').hidden;
        }
        const crawlChoice = form.querySelector('[data-method-choice][value="WEB_CRAWL"]');
        crawlChoice.checked = true;
        crawlChoice.dispatchEvent(new Event('change', {bubbles: true}));
        const advanced = form.querySelector('[data-method-panel="WEB_CRAWL"] details.advanced-settings');
        advanced.open = true;
        advanced.dispatchEvent(new Event('toggle'));
        setTimeout(() => {
          const scroll = form.querySelector('[data-source-wizard-scroll]');
          const footer = form.querySelector('.source-wizard-actions');
          const modalRect = modal.querySelector('.modal-shell').getBoundingClientRect();
          const footerRect = footer.getBoundingClientRect();
          const visibleScrollOwners = [...form.querySelectorAll('*')].filter((node) => {
            const style = getComputedStyle(node);
            return node.matches('div, section, form') && node.getClientRects().length
              && ['auto', 'scroll'].includes(style.overflowY);
          });
          const edit = document.querySelector('[data-modal^="edit-"] [data-source-wizard]');
          document.body.dataset.wizardBrowserContract = JSON.stringify({
            noObjectText: !form.innerText.includes('[object Object]'),
            modes,
            steps: steps.length,
            advancedFields: advanced.querySelectorAll('[name^="method_config."]').length,
            footerOutsideScroll: !scroll.contains(footer),
            footerVisible: footerRect.height > 0 && footerRect.bottom <= modalRect.bottom + 1,
            scrollOwners: visibleScrollOwners.length,
            editRendered: Boolean(edit) && !edit.innerText.includes('[object Object]'),
          });
        }, 100);
      }, 50);
    """
    soup.body.append(harness)
    html_path = tmp_path / "source-wizard-browser-contract.html"
    html_path.write_text(str(soup), encoding="utf-8")
    completed = subprocess.run(
        [
            str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={tmp_path / 'chrome-profile'}",
            "--virtual-time-budget=1000", "--dump-dom", html_path.as_uri(),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    rendered = BeautifulSoup(completed.stdout, "html.parser")
    contract = json.loads(rendered.body["data-wizard-browser-contract"])
    assert contract == {
        "noObjectText": True,
        "modes": {"WEB_PAGE": True, "WEB_CRAWL": True, "API": True},
        "steps": 3,
        "advancedFields": 6,
        "footerOutsideScroll": True,
        "footerVisible": True,
        "scrollOwners": 1,
        "editRendered": True,
    }


def test_edit_wizard_preloads_selected_method_and_keeps_other_panels_inactive(wizard_db):
    url, factory, root = wizard_db
    seed_source(factory, CollectionMethod.API)
    page = TestClient(regression_app(url, project_root=root)).get("/sources")
    soup = BeautifulSoup(page.text, "html.parser")
    edit = soup.select_one('[data-modal^="edit-"] [data-source-wizard]')
    edit_scroll = edit.select_one("[data-source-wizard-scroll]")
    edit_actions = edit.select_one(".source-wizard-actions")
    assert edit_scroll is not None and edit_scroll.parent is edit
    assert edit_actions is not None and edit_actions.parent is edit
    assert edit_actions not in edit_scroll.select(".source-wizard-actions")
    assert edit_scroll.select_one('[data-method-panel="WEB_CRAWL"] details.advanced-settings')
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
    assert ".workspace-table--sources { width: 100%; min-width: 680px; table-layout: fixed; }" in css
    for route in ("/agencies", "/contacts", "/runs", "/review"):
        assert "PublicDB2 DB" not in client.get(route).text
