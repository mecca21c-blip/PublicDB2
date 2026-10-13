from __future__ import annotations

import json
from pathlib import Path
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
from app.models import (
    Agency,
    AgencyType,
    CollectionJob,
    CollectionMethod,
    ContactPoint,
    CrawlRun,
    Duty,
    ExtractedFeedItem,
    ExtractionRun,
    Observation,
    OrgUnit,
    Person,
    Source,
    SourceApiConfig,
    SourceBinding,
)
from tests.support import regression_app


RSS_URL = "https://data.seoul.go.kr/rss/rssView.do?searchType=0000"


@pytest.fixture()
def next_db(tmp_path, monkeypatch):
    database = tmp_path / "next-06c8b.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(database_url)
    factory = create_session_factory(engine)
    with factory() as session:
        session.add(Agency(
            official_name="서울특별시", normalized_name="서울특별시",
            agency_type=AgencyType.METROPOLITAN_GOVERNMENT,
            region_code="SEOUL", active=True,
        ))
        session.commit()
    yield database_url, factory, tmp_path
    engine.dispose()


def _browser() -> Path | None:
    return next((candidate for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ) if candidate.exists()), None)


def _domain_counts(factory) -> dict[str, int]:
    models = (
        Agency, OrgUnit, Source, SourceBinding, SourceApiConfig, CollectionJob,
        CrawlRun, Observation, ExtractionRun, ExtractedFeedItem,
        Person, Duty, ContactPoint,
    )
    with factory() as session:
        return {
            model.__name__: session.scalar(select(func.count()).select_from(model))
            for model in models
        }


def _production_page(database_url: str, tmp_path: Path) -> BeautifulSoup:
    page = TestClient(regression_app(database_url, project_root=tmp_path)).get("/sources")
    assert page.status_code == 200
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
    return soup


def _run_browser(soup: BeautifulSoup, tmp_path: Path, harness_source: str) -> dict:
    browser = _browser()
    if browser is None:
        pytest.skip("Chrome or Edge is required for the Source Wizard browser contract")
    harness = soup.new_tag("script")
    harness.string = harness_source
    soup.body.append(harness)
    html_path = tmp_path / "source-rss-next-contract.html"
    html_path.write_text(str(soup), encoding="utf-8")
    completed = subprocess.run(
        [
            str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={tmp_path / 'chrome-profile'}",
            "--virtual-time-budget=6000", "--dump-dom", html_path.as_uri(),
        ],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    rendered = BeautifulSoup(completed.stdout, "html.parser")
    assert "data-rss-next-error" not in rendered.body.attrs, rendered.body.get("data-rss-next-error")
    return json.loads(rendered.body["data-rss-next-contract"])


def test_rss_config_preview_next_reaches_step_three_and_preserves_identity(next_db):
    database_url, _factory, tmp_path = next_db
    soup = _production_page(database_url, tmp_path)
    contract = _run_browser(soup, tmp_path, r"""
      setTimeout(async () => {
        const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const response = (payload, ok = true, status = 200) => ({
          ok, status, json: async () => payload, text: async () => JSON.stringify(payload),
        });
        const events = {requests: [], consoleErrors: []};
        window.addEventListener('error', (event) => events.consoleErrors.push(event.message));
        window.addEventListener('unhandledrejection', (event) => events.consoleErrors.push(String(event.reason)));
        try {
          let discoveryCount = 0;
          let interactiveCount = 0;
          const previewFor = (payload) => {
            const row = payload.rows[0];
            const identityMatches = row.agency_id === 'agency-seoul' && row.agency_name === '서울특별시';
            const projected = {
              row_id: row.row_id, url: row.url, normalized_url: row.url,
              agency_id: identityMatches ? 'agency-seoul' : null,
              agency_name: row.agency_name, org_unit_id: null, org_unit_name: null,
              status: identityMatches ? 'RESOLVED' : 'ERROR', importable: identityMatches,
              message: identityMatches ? '연결 확인됨' : '선택한 기관 정보가 현재 입력과 일치하지 않습니다.',
            };
            return {
              summary: {
                input: 1, resolved: identityMatches ? 1 : 0, planned_new: 0,
                unresolved: 0, duplicates: 0, errors: identityMatches ? 0 : 1,
                importable: identityMatches ? 1 : 0, agency_count: identityMatches ? 1 : 0,
                org_connections: 0, agency_wide_connections: identityMatches ? 1 : 0,
                new_sources: identityMatches ? 1 : 0, existing_sources: 0,
                new_bindings: identityMatches ? 1 : 0, existing_bindings: 0,
                new_agencies_planned: 0, new_org_units_planned: 0,
              },
              rows: [projected], agency_resolutions: [], org_resolutions: [],
            };
          };
          window.fetch = async (target, options = {}) => {
            const path = String(target);
            if (path.includes('/api/source-agency-discovery')) {
              discoveryCount += 1;
              const payload = discoveryCount === 1
                ? {existing_agency: {id: 'agency-seoul', name: '서울특별시'}, existing_org_unit: null,
                   candidate_name: '서울특별시', org_unit_candidate: null, message: '기관 확인됨'}
                : {existing_agency: null, existing_org_unit: null,
                   candidate_name: '서울 열린데이터 광장', org_unit_candidate: null, message: '새 후보'};
              events.requests.push({path, kind: 'discovery', status: 200, payload});
              return response(payload);
            }
            if (path.includes('/api/source-config/preview')) {
              const body = JSON.parse(options.body);
              const payload = {http_status: 200, raw_count: 6, mapped_sample: [], mapping_errors: []};
              events.requests.push({path, kind: 'config', status: 200, body, payload});
              return response(payload);
            }
            if (path.includes('/api/source-bindings/interactive/preview')) {
              interactiveCount += 1;
              const body = JSON.parse(options.body);
              const payload = previewFor(body);
              events.requests.push({path, kind: 'interactive', status: 200, body, payload});
              if (interactiveCount === 2) await delay(180);
              return response(payload);
            }
            return response({job: null, jobs: []});
          };

          const form = document.querySelector('[data-modal="source-create"] [data-source-wizard]');
          document.querySelector('[data-modal-open="source-create"]').click();
          const method = form.querySelector('[data-method-choice][value="API"]');
          method.checked = true; method.dispatchEvent(new Event('change', {bubbles: true}));
          const url = form.querySelector('[data-row-url]');
          url.value = 'https://data.seoul.go.kr/rss/rssView.do?searchType=0000';
          url.dispatchEvent(new Event('input', {bubbles: true}));
          await delay(650);
          const next = form.querySelector('[data-wizard-next]');
          next.click(); await delay(160);

          const feed = form.querySelector('[data-api-kind-choice][value="FEED"]');
          feed.checked = true; feed.dispatchEvent(new Event('change', {bubbles: true}));
          const feedKind = form.querySelector('[data-feed-kind]');
          feedKind.value = 'RSS'; feedKind.dispatchEvent(new Event('change', {bubbles: true}));
          await delay(160);
          form.querySelector('[data-api-subtype="FEED"] [data-config-preview]').click();
          await delay(160);

          let nextClickEvents = 0;
          next.addEventListener('click', () => { nextClickEvents += 1; }, {capture: true});
          const before = {
            disabled: next.disabled,
            activeStep: form.querySelector('[data-wizard-indicator].is-active')?.dataset.wizardIndicator,
            rowStatus: form.querySelector('[data-row-status]').textContent.trim(),
            agencyName: form.querySelector('[data-row-agency]').value,
            agencyId: form.querySelector('[data-row-agency-id]').value,
          };
          next.click();
          const pending = {disabled: next.disabled, label: next.textContent};
          next.click();
          await delay(240);
          const errorBox = form.querySelector('[data-form-error]');
          const review = Object.fromEntries([...form.querySelectorAll('[data-final-method-config] [data-review-field]')]
            .map((item) => [item.dataset.reviewField, item.textContent]));
          const finalCells = [...form.querySelectorAll('[data-final-rows] tr:first-child td')].map((cell) => cell.textContent);
          const after = {
            disabled: next.disabled, label: next.textContent,
            activeStep: form.querySelector('[data-wizard-indicator].is-active')?.dataset.wizardIndicator,
            step2Hidden: form.querySelector('[data-wizard-step="2"]').hidden,
            step3Hidden: form.querySelector('[data-wizard-step="3"]').hidden,
            errorHidden: errorBox.hidden, errorText: errorBox.textContent,
            review, finalCells,
          };
          document.body.dataset.rssNextContract = JSON.stringify({
            nextClickEvents, discoveryCount, configText: form.querySelector('[data-api-subtype="FEED"] [data-preview-result]').textContent,
            before, pending, after, events,
            noObjectText: !form.innerText.includes('[object Object]'),
          });
        } catch (error) {
          document.body.dataset.rssNextError = error.stack || error.message || String(error);
        }
      }, 50);
    """)

    interactive = [item for item in contract["events"]["requests"] if item["kind"] == "interactive"]
    assert contract["nextClickEvents"] == 1
    assert contract["configText"] == "HTTP 200 · RSS 항목 6건"
    assert len(interactive) == 2
    assert contract["discoveryCount"] == 1
    assert contract["before"]["activeStep"] == "2"
    assert contract["before"]["disabled"] is False
    assert contract["before"]["rowStatus"] == "✓ 연결 확인됨"
    assert contract["before"]["agencyName"] == "서울특별시"
    assert contract["before"]["agencyId"] == "agency-seoul"
    assert contract["pending"] == {"disabled": True, "label": "확인 중..."}
    assert interactive[-1]["status"] == 200
    assert interactive[-1]["payload"]["summary"]["errors"] == 0
    assert interactive[-1]["body"]["method_config"] == {"kind": "RSS"}
    assert interactive[-1]["body"]["rows"][0]["agency_id"] == "agency-seoul"
    assert interactive[-1]["body"]["rows"][0]["agency_name"] == "서울특별시"
    assert contract["after"]["activeStep"] == "3"
    assert contract["after"]["step2Hidden"] is True
    assert contract["after"]["step3Hidden"] is False
    assert contract["after"]["errorHidden"] is True
    assert contract["after"]["review"] == {
        "method": "공개 API / RSS", "api_kind": "RSS", "endpoint": RSS_URL,
        "scheduled_refresh_enabled": "포함", "description": "-",
    }
    assert contract["after"]["finalCells"] == [RSS_URL, "서울특별시", "기관 전체", "연결 확인됨"]
    assert contract["after"]["review"].keys().isdisjoint({
        "response_format", "field_mapping", "pagination_mode", "auth_mode",
    })
    assert contract["events"]["consoleErrors"] == []
    assert contract["noObjectText"] is True


def test_rss_without_config_preview_atom_and_openapi_reach_step_three(next_db):
    database_url, _factory, tmp_path = next_db
    soup = _production_page(database_url, tmp_path)
    contract = _run_browser(soup, tmp_path, r"""
      setTimeout(async () => {
        const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const response = (payload) => ({ok: true, status: 200, json: async () => payload, text: async () => JSON.stringify(payload)});
        try {
          let discoveryCount = 0;
          const interactiveBodies = [];
          let configPreviewCount = 0;
          const previewFor = (body) => {
            const row = body.rows[0];
            return {
              summary: {input: 1, resolved: 1, planned_new: 0, unresolved: 0, duplicates: 0,
                errors: 0, importable: 1, agency_count: 1, org_connections: 0,
                agency_wide_connections: 1, new_sources: 1, existing_sources: 0,
                new_bindings: 1, existing_bindings: 0, new_agencies_planned: 0, new_org_units_planned: 0},
              rows: [{row_id: row.row_id, url: row.url, normalized_url: row.url,
                agency_id: 'agency-seoul', agency_name: '서울특별시', org_unit_id: null,
                org_unit_name: null, status: 'RESOLVED', importable: true, message: '연결 확인됨'}],
              agency_resolutions: [], org_resolutions: [],
            };
          };
          window.fetch = async (target, options = {}) => {
            const path = String(target);
            if (path.includes('/api/source-agency-discovery')) {
              discoveryCount += 1;
              return response({existing_agency: {id: 'agency-seoul', name: '서울특별시'},
                existing_org_unit: null, candidate_name: '서울특별시', org_unit_candidate: null});
            }
            if (path.includes('/api/source-bindings/interactive/preview')) {
              const body = JSON.parse(options.body); interactiveBodies.push(body); return response(previewFor(body));
            }
            if (path.includes('/api/source-config/preview')) {
              configPreviewCount += 1; return response({http_status: 200, raw_count: 6, mapped_sample: []});
            }
            return response({job: null, jobs: []});
          };
          const form = document.querySelector('[data-modal="source-create"] [data-source-wizard]');
          document.querySelector('[data-modal-open="source-create"]').click();
          const api = form.querySelector('[data-method-choice][value="API"]');
          api.checked = true; api.dispatchEvent(new Event('change', {bubbles: true}));
          const url = form.querySelector('[data-row-url]');
          url.value = 'https://data.seoul.go.kr/rss/rssView.do?searchType=0000';
          url.dispatchEvent(new Event('input', {bubbles: true}));
          await delay(650);
          const next = form.querySelector('[data-wizard-next]');
          const previous = form.querySelector('[data-wizard-prev]');
          next.click(); await delay(120);
          const selectKind = (choiceValue, feedKind = null) => {
            const choice = form.querySelector('[data-api-kind-choice][value="' + choiceValue + '"]');
            choice.checked = true; choice.dispatchEvent(new Event('change', {bubbles: true}));
            if (feedKind) {
              const feed = form.querySelector('[data-feed-kind]');
              feed.value = feedKind; feed.dispatchEvent(new Event('change', {bubbles: true}));
            }
          };
          const advanceAndCapture = async () => {
            next.click(); await delay(120);
            const review = Object.fromEntries([...form.querySelectorAll('[data-final-method-config] [data-review-field]')]
              .map((item) => [item.dataset.reviewField, item.textContent]));
            const active = form.querySelector('[data-wizard-indicator].is-active')?.dataset.wizardIndicator;
            previous.click(); await delay(20);
            return {active, review};
          };
          selectKind('FEED', 'RSS');
          const rss = await advanceAndCapture();
          selectKind('FEED', 'ATOM');
          const atom = await advanceAndCapture();
          selectKind('OPEN_API');
          const openApi = await advanceAndCapture();
          document.body.dataset.rssNextContract = JSON.stringify({
            rss, atom, openApi, discoveryCount, configPreviewCount, interactiveBodies,
            noObjectText: !form.innerText.includes('[object Object]'),
          });
        } catch (error) {
          document.body.dataset.rssNextError = error.stack || error.message || String(error);
        }
      }, 50);
    """)
    assert contract["configPreviewCount"] == 0
    assert contract["discoveryCount"] == 1
    assert [item["method_config"]["kind"] for item in contract["interactiveBodies"][-3:]] == [
        "RSS", "ATOM", "OPEN_API",
    ]
    assert contract["rss"]["active"] == "3"
    assert contract["rss"]["review"] == {
        "method": "공개 API / RSS", "api_kind": "RSS", "endpoint": RSS_URL,
        "scheduled_refresh_enabled": "포함", "description": "-",
    }
    assert contract["atom"]["active"] == "3"
    assert contract["atom"]["review"]["api_kind"] == "Atom"
    assert contract["openApi"]["active"] == "3"
    assert contract["openApi"]["review"]["api_kind"] == "OpenAPI"
    assert "response_format" in contract["openApi"]["review"]
    assert contract["noObjectText"] is True


def test_failed_step_two_preview_restores_next_and_shows_fixed_error(next_db):
    database_url, _factory, tmp_path = next_db
    soup = _production_page(database_url, tmp_path)
    contract = _run_browser(soup, tmp_path, r"""
      setTimeout(async () => {
        const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const response = (payload, ok = true, status = 200) => ({
          ok, status, json: async () => payload, text: async () => JSON.stringify(payload),
        });
        try {
          let interactiveCount = 0;
          window.fetch = async (target, options = {}) => {
            const path = String(target);
            if (path.includes('/api/source-agency-discovery')) return response({
              existing_agency: {id: 'agency-seoul', name: '서울특별시'}, existing_org_unit: null,
              candidate_name: '서울특별시', org_unit_candidate: null,
            });
            if (path.includes('/api/source-bindings/interactive/preview')) {
              interactiveCount += 1;
              const body = JSON.parse(options.body); const row = body.rows[0];
              if (interactiveCount === 2) {
                await delay(160);
                return response({detail: [{loc: ['body', 'rows'], msg: '등록 검증 실패'}]}, false, 422);
              }
              return response({
                summary: {input: 1, resolved: 1, planned_new: 0, unresolved: 0, duplicates: 0,
                  errors: 0, importable: 1, agency_count: 1, org_connections: 0,
                  agency_wide_connections: 1, new_sources: 1, existing_sources: 0,
                  new_bindings: 1, existing_bindings: 0, new_agencies_planned: 0, new_org_units_planned: 0},
                rows: [{row_id: row.row_id, url: row.url, agency_id: 'agency-seoul', agency_name: '서울특별시',
                  org_unit_id: null, org_unit_name: null, status: 'RESOLVED', importable: true, message: '연결 확인됨'}],
                agency_resolutions: [], org_resolutions: [],
              });
            }
            return response({job: null, jobs: []});
          };
          const form = document.querySelector('[data-modal="source-create"] [data-source-wizard]');
          document.querySelector('[data-modal-open="source-create"]').click();
          const api = form.querySelector('[data-method-choice][value="API"]');
          api.checked = true; api.dispatchEvent(new Event('change', {bubbles: true}));
          const url = form.querySelector('[data-row-url]');
          url.value = 'https://data.seoul.go.kr/rss/rssView.do?searchType=0000';
          url.dispatchEvent(new Event('input', {bubbles: true}));
          await delay(650);
          const next = form.querySelector('[data-wizard-next]');
          next.click(); await delay(120);
          const feed = form.querySelector('[data-api-kind-choice][value="FEED"]');
          feed.checked = true; feed.dispatchEvent(new Event('change', {bubbles: true}));
          let clickEvents = 0;
          next.addEventListener('click', () => { clickEvents += 1; }, {capture: true});
          next.click();
          const pending = {disabled: next.disabled, label: next.textContent};
          next.click(); await delay(220);
          const error = form.querySelector('[data-form-error]');
          const footer = form.querySelector('.source-wizard-actions');
          document.body.dataset.rssNextContract = JSON.stringify({
            interactiveCount, clickEvents, pending,
            activeStep: form.querySelector('[data-wizard-indicator].is-active')?.dataset.wizardIndicator,
            nextDisabled: next.disabled, nextLabel: next.textContent,
            errorHidden: error.hidden, errorText: error.textContent,
            errorVisible: getComputedStyle(error).display !== 'none',
            errorOutsideScroll: !form.querySelector('[data-source-wizard-scroll]').contains(error),
            errorImmediatelyAboveFooter: error.nextElementSibling === footer,
            noObjectText: !form.innerText.includes('[object Object]'),
          });
        } catch (error) {
          document.body.dataset.rssNextError = error.stack || error.message || String(error);
        }
      }, 50);
    """)
    assert contract["interactiveCount"] == 2
    assert contract["clickEvents"] == 1
    assert contract["pending"] == {"disabled": True, "label": "확인 중..."}
    assert contract["activeStep"] == "2"
    assert contract["nextDisabled"] is False and contract["nextLabel"] == "다음"
    assert contract["errorHidden"] is False and contract["errorVisible"] is True
    assert contract["errorText"] == "rows: 등록 검증 실패"
    assert contract["errorOutsideScroll"] is True
    assert contract["errorImmediatelyAboveFooter"] is True
    assert contract["noObjectText"] is True


def test_interactive_preview_validates_rss_config_without_domain_writes(next_db):
    database_url, factory, tmp_path = next_db
    with factory() as session:
        agency = session.scalar(select(Agency).where(Agency.official_name == "서울특별시"))
        agency_id = str(agency.id)
    before = _domain_counts(factory)
    payload = {
        "collection_method": "API",
        "rows": [{
            "row_id": "row-1", "url": RSS_URL,
            "agency_name": "서울특별시", "agency_id": agency_id,
            "org_unit_name": "", "org_unit_id": None,
        }],
        "method_config": {"kind": "RSS"},
        "scheduled_refresh_enabled": True,
    }
    with TestClient(regression_app(database_url, project_root=tmp_path)) as client:
        valid = client.post(
            "/api/source-bindings/interactive/preview",
            headers={"X-CSRF-Token": "test-csrf"}, json=payload,
        )
        invalid = client.post(
            "/api/source-bindings/interactive/preview",
            headers={"X-CSRF-Token": "test-csrf"},
            json={**payload, "method_config": {
                "kind": "OPEN_API", "response_format": "JSON",
                "discovery_only": False, "field_mapping": {},
            }},
        )
    assert valid.status_code == 200
    assert valid.json()["summary"] == {
        "input": 1, "resolved": 1, "planned_new": 0, "unresolved": 0,
        "duplicates": 0, "errors": 0, "importable": 1, "agency_count": 1,
        "org_connections": 0, "new_agencies_planned": 0,
        "new_org_units_planned": 0, "agency_wide_connections": 1,
        "new_sources": 1, "existing_sources": 0,
        "new_bindings": 1, "existing_bindings": 0,
    }
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "필드 매핑 또는 명시적인 discovery-only 설정이 필요합니다."
    assert _domain_counts(factory) == before


def test_edit_wizard_navigation_and_save_payload_are_unchanged(next_db):
    database_url, factory, tmp_path = next_db
    with factory() as session:
        agency = session.scalar(select(Agency).where(Agency.official_name == "서울특별시"))
        source = Source(
            url="https://example.org/existing-api?mode=current",
            normalized_url="https://example.org/existing-api?mode=current",
            title="기존 API", collection_method=CollectionMethod.API,
            scheduled_refresh_enabled=True, active=True,
        )
        session.add(source)
        session.flush()
        session.add(SourceBinding(
            source_id=source.id, agency_id=agency.id, org_unit_id=None,
            scope_key=f"agency:{agency.id}", description="기관 공통", active=True,
        ))
        session.commit()
    soup = _production_page(database_url, tmp_path)
    contract = _run_browser(soup, tmp_path, r"""
      setTimeout(async () => {
        const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        try {
          const saves = [];
          window.fetch = async (target, options = {}) => {
            const path = String(target);
            if (options.method === 'PATCH') {
              saves.push({path, method: options.method, body: JSON.parse(options.body)});
              return new Promise(() => {});
            }
            return {ok: true, status: 200, json: async () => ({job: null, jobs: []}), text: async () => '{}'};
          };
          const form = document.querySelector('[data-modal^="edit-"] [data-source-wizard]');
          const modal = form.closest('[data-modal]');
          document.querySelector('[data-modal-open="' + modal.dataset.modal + '"]').click();
          const next = form.querySelector('[data-wizard-next]');
          next.click();
          const step2 = form.querySelector('[data-wizard-indicator].is-active')?.dataset.wizardIndicator;
          next.click();
          const step3 = form.querySelector('[data-wizard-indicator].is-active')?.dataset.wizardIndicator;
          const feed = form.querySelector('[data-api-kind-choice][value="FEED"]');
          feed.checked = true; feed.dispatchEvent(new Event('change', {bubbles: true}));
          const feedKind = form.querySelector('[data-feed-kind]');
          feedKind.value = 'RSS'; feedKind.dispatchEvent(new Event('change', {bubbles: true}));
          form.querySelector('[data-wizard-submit]').click();
          await delay(80);
          document.body.dataset.rssNextContract = JSON.stringify({
            step2, step3, saves,
            errorOutsideScroll: !form.querySelector('[data-source-wizard-scroll]').contains(form.querySelector('[data-form-error]')),
            noObjectText: !form.innerText.includes('[object Object]'),
          });
        } catch (error) {
          document.body.dataset.rssNextError = error.stack || error.message || String(error);
        }
      }, 50);
    """)
    assert contract["step2"] == "2" and contract["step3"] == "3"
    assert len(contract["saves"]) == 1
    assert contract["saves"][0]["method"] == "PATCH"
    assert contract["saves"][0]["body"]["collection_method"] == "API"
    assert contract["saves"][0]["body"]["method_config"]["kind"] == "RSS"
    assert contract["errorOutsideScroll"] is True
    assert contract["noObjectText"] is True
