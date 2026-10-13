from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse

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
    SourceBinding,
)
from tests.support import regression_app


PUBLIC_DNS = lambda _host: {"93.184.216.34"}
RSS_URL = "https://data.seoul.go.kr/rss/rssView.do?searchType=0000"


@pytest.fixture()
def preview_db(tmp_path, monkeypatch):
    database = tmp_path / "preview-06c8a.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(database_url)
    factory = create_session_factory(engine)
    yield database_url, factory, tmp_path
    engine.dispose()


def seed_api_source(factory) -> None:
    with factory() as session:
        agency = Agency(
            official_name="서울특별시", normalized_name="서울특별시",
            agency_type=AgencyType.METROPOLITAN_GOVERNMENT,
            region_code="SEOUL", active=True,
        )
        source = Source(
            url="https://example.org/existing-feed.xml?scope=all",
            normalized_url="https://example.org/existing-feed.xml?scope=all",
            title="기존 API Source", collection_method=CollectionMethod.API,
            scheduled_refresh_enabled=True, active=True,
        )
        session.add_all([agency, source])
        session.flush()
        session.add(SourceBinding(
            source_id=source.id, agency_id=agency.id, org_unit_id=None,
            scope_key=f"agency:{agency.id}", description="기관 공통", active=True,
        ))
        session.commit()


def _domain_counts(factory) -> dict[str, int]:
    models = (
        Agency, OrgUnit, Source, SourceBinding, CrawlRun, Observation,
        CollectionJob, ExtractionRun, ExtractedFeedItem,
        Person, Duty, ContactPoint,
    )
    with factory() as session:
        return {
            model.__name__: session.scalar(select(func.count()).select_from(model))
            for model in models
        }


def test_rss_atom_and_openapi_preview_are_read_only_and_keep_query_string(preview_db):
    database_url, factory, root = preview_db
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/rssView.do"):
            content = b"<rss><channel><item/><item/><item/></channel></rss>"
            return httpx.Response(200, headers={"Content-Type": "application/rss+xml"}, content=content)
        if request.url.path.endswith("/atom.xml"):
            content = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry/><entry/></feed>'
            return httpx.Response(200, headers={"Content-Type": "application/atom+xml"}, content=content)
        return httpx.Response(
            200, headers={"Content-Type": "application/json"},
            content=b'{"items":[{"name":"one"},{"name":"two"}]}',
        )

    app = regression_app(database_url, project_root=root)
    app.state.preview_fetcher_factory = lambda: HTTPFetcher(
        transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS,
    )
    before = _domain_counts(factory)
    with TestClient(app) as client:
        rss = client.post("/api/source-config/preview", json={
            "url": RSS_URL, "method_config": {"kind": "RSS"},
        })
        atom = client.post("/api/source-config/preview", json={
            "url": "https://example.org/atom.xml?format=full",
            "method_config": {"kind": "ATOM"},
        })
        openapi = client.post("/api/source-config/preview", json={
            "url": "https://example.org/open-api?service=list",
            "method_config": {
                "kind": "OPEN_API", "response_format": "JSON",
                "record_path": "items", "auth_mode": "NONE", "discovery_only": True,
            },
        })

    assert rss.status_code == 200 and rss.json()["raw_count"] == 3
    assert rss.json()["mapped_sample"] == []
    assert atom.status_code == 200 and atom.json()["raw_count"] == 2
    assert atom.json()["mapped_sample"] == []
    assert openapi.status_code == 200 and openapi.json()["raw_count"] == 2
    assert len(openapi.json()["mapped_sample"]) == 2
    assert calls == [
        RSS_URL,
        "https://example.org/atom.xml?format=full",
        "https://example.org/open-api?service=list",
    ]
    assert _domain_counts(factory) == before


def test_production_source_wizard_preview_owns_current_create_and_edit_url(preview_db):
    database_url, factory, tmp_path = preview_db
    seed_api_source(factory)
    page = TestClient(regression_app(database_url, project_root=tmp_path)).get("/sources")
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
      setTimeout(async () => {
        const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const response = (payload, ok = true) => ({
          ok, status: ok ? 200 : 422,
          json: async () => payload,
          text: async () => JSON.stringify(payload),
        });
        try {
          const configRequests = [];
          window.fetch = async (target, options = {}) => {
            const path = String(target);
            if (path.includes('/api/source-agency-discovery')) return response({
              existing_agency: {id: 'agency-1', name: '서울특별시'}, existing_org_unit: null,
              candidate_name: '서울특별시', org_unit_candidate: null, message: '기관 확인됨',
            });
            if (path.includes('/api/source-config/preview')) {
              const payload = JSON.parse(options.body);
              configRequests.push(payload);
              return response({
                http_status: 200, raw_count: 12,
                mapped_sample: payload.method_config.kind === 'OPEN_API' ? [{name: 'sample'}] : [],
                mapping_errors: [],
              });
            }
            return response({job: null, jobs: []});
          };

          const selectMethod = (form, method) => {
            const choice = form.querySelector('[data-method-choice][value="' + method + '"]');
            choice.checked = true;
            choice.dispatchEvent(new Event('change', {bubbles: true}));
          };
          const selectKind = (form, choiceValue, feedKind = null) => {
            const choice = form.querySelector('[data-api-kind-choice][value="' + choiceValue + '"]');
            choice.checked = true;
            choice.dispatchEvent(new Event('change', {bubbles: true}));
            if (feedKind) {
              const select = form.querySelector('[data-feed-kind]');
              select.value = feedKind;
              select.dispatchEvent(new Event('change', {bubbles: true}));
            }
          };
          const clickPreview = async (form, subtype) => {
            const panel = form.querySelector('[data-api-subtype="' + subtype + '"]');
            panel.querySelector('[data-config-preview]').click();
            await delay(120);
            return panel.querySelector('[data-preview-result]').textContent;
          };

          document.querySelector('[data-modal-open="source-create"]').click();
          const createForm = document.querySelector('[data-modal="source-create"] [data-source-wizard]');
          selectMethod(createForm, 'API');
          const createUrl = createForm.querySelector('[data-source-row] [data-row-url]');
          createUrl.value = 'https://data.seoul.go.kr/rss/rssView.do?searchType=0000';
          createUrl.dispatchEvent(new Event('input', {bubbles: true}));
          await delay(600);
          createForm.querySelector('[data-wizard-next]').click();
          await delay(120);

          selectKind(createForm, 'FEED', 'RSS');
          const rssText = await clickPreview(createForm, 'FEED');
          const rssPayload = configRequests.at(-1);
          selectKind(createForm, 'FEED', 'ATOM');
          const atomText = await clickPreview(createForm, 'FEED');
          const atomPayload = configRequests.at(-1);
          selectKind(createForm, 'OPEN_API');
          const openApiText = await clickPreview(createForm, 'OPEN_API');
          const openApiPayload = configRequests.at(-1);

          createUrl.value = 'https://example.go.kr/rss/feed?searchType=0000&category=health';
          createUrl.dispatchEvent(new Event('input', {bubbles: true}));
          selectKind(createForm, 'FEED', 'RSS');
          const changedText = await clickPreview(createForm, 'FEED');
          const changedPayload = configRequests.at(-1);

          createUrl.value = 'ftp://data.seoul.go.kr/feed.xml';
          const beforeInvalidScheme = configRequests.length;
          const invalidSchemeText = await clickPreview(createForm, 'FEED');
          const afterInvalidScheme = configRequests.length;
          createUrl.value = '';
          const beforeMissing = configRequests.length;
          const missingText = await clickPreview(createForm, 'FEED');
          const afterMissing = configRequests.length;

          const editForm = document.querySelector('[data-modal^="edit-"] [data-source-wizard]');
          selectMethod(editForm, 'API');
          selectKind(editForm, 'FEED', 'RSS');
          const editUrl = editForm.querySelector('[data-api-subtype="FEED"] [name="url"]');
          editUrl.value = 'https://example.org/edit-feed.xml?mode=current';
          const editText = await clickPreview(editForm, 'FEED');
          const editPayload = configRequests.at(-1);

          document.body.dataset.previewOwnershipContract = JSON.stringify({
            rss: {text: rssText, payload: rssPayload},
            atom: {text: atomText, payload: atomPayload},
            openApi: {text: openApiText, payload: openApiPayload},
            changed: {text: changedText, payload: changedPayload},
            invalid: {text: invalidSchemeText, before: beforeInvalidScheme, after: afterInvalidScheme},
            missing: {text: missingText, before: beforeMissing, after: afterMissing},
            edit: {text: editText, payload: editPayload},
            noObjectText: !createForm.innerText.includes('[object Object]') && !editForm.innerText.includes('[object Object]'),
          });
        } catch (error) {
          document.body.dataset.previewOwnershipError = error.stack || error.message || String(error);
        }
      }, 50);
    """
    soup.body.append(harness)
    html_path = tmp_path / "source-preview-ownership-contract.html"
    html_path.write_text(str(soup), encoding="utf-8")
    completed = subprocess.run(
        [
            str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={tmp_path / 'chrome-profile'}",
            "--virtual-time-budget=5000", "--dump-dom", html_path.as_uri(),
        ],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    rendered = BeautifulSoup(completed.stdout, "html.parser")
    assert "data-preview-ownership-error" not in rendered.body.attrs, rendered.body.get("data-preview-ownership-error")
    contract = json.loads(rendered.body["data-preview-ownership-contract"])

    assert contract["rss"] == {
        "text": "HTTP 200 · RSS 항목 12건",
        "payload": {"url": RSS_URL, "method_config": {"kind": "RSS"}},
    }
    assert contract["atom"] == {
        "text": "HTTP 200 · Atom 항목 12건",
        "payload": {"url": RSS_URL, "method_config": {"kind": "ATOM"}},
    }
    assert contract["openApi"]["payload"]["url"] == RSS_URL
    assert contract["openApi"]["payload"]["method_config"]["kind"] == "OPEN_API"
    assert contract["openApi"]["text"] == "HTTP 200 · 원시 12건 · 표본 1건"
    assert contract["changed"] == {
        "text": "HTTP 200 · RSS 항목 12건",
        "payload": {
            "url": "https://example.go.kr/rss/feed?searchType=0000&category=health",
            "method_config": {"kind": "RSS"},
        },
    }
    assert contract["invalid"] == {
        "text": "URL / Endpoint를 먼저 확인하세요.", "before": 4, "after": 4,
    }
    assert contract["missing"] == {
        "text": "URL / Endpoint를 먼저 확인하세요.", "before": 4, "after": 4,
    }
    assert contract["edit"] == {
        "text": "HTTP 200 · RSS 항목 12건",
        "payload": {
            "url": "https://example.org/edit-feed.xml?mode=current",
            "method_config": {"kind": "RSS"},
        },
    }
    assert contract["noObjectText"] is True
