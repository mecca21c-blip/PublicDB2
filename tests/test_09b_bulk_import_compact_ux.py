from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.db.base import Base
from app.db.engine import create_db_engine
from tests.support import regression_app


def _sources_page(tmp_path: Path) -> str:
    database = tmp_path / "compact-ux.sqlite3"
    url = f"sqlite:///{database.as_posix()}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(regression_app(url, project_root=tmp_path)) as client:
        return client.get("/sources").text


def _production_harness(page: str, payload: bytes) -> str:
    soup = BeautifulSoup(page, "html.parser")
    for link in list(soup.select('link[rel="stylesheet"]')):
        static_path = urlparse(link["href"]).path
        if static_path.startswith("/static/"):
            style = soup.new_tag("style")
            style.string = (Path("app/web/static") / static_path.removeprefix("/static/")).read_text(encoding="utf-8")
            link.replace_with(style)
    for script in list(soup.select('script[src$="/static/js/app.js"]')):
        inline = soup.new_tag("script")
        inline.string = Path("app/web/static/js/app.js").read_text(encoding="utf-8")
        script.replace_with(inline)

    encoded = base64.b64encode(payload).decode()
    harness = soup.new_tag("script")
    harness.string = f"""
      setTimeout(async () => {{
        const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const response = (payload) => ({{ok: true, status: 200, json: async () => payload}});
        const categories = ['NEW_AGENCY', 'READY', 'EXACT_DUPLICATE', 'EXACT_DUPLICATE',
          'EXACT_DUPLICATE', 'EXACT_DUPLICATE', 'CONFLICT', 'INVALID'];
        const messages = {{NEW_AGENCY: '기관을 새로 만듭니다.', READY: '등록 가능합니다.',
          EXACT_DUPLICATE: '같은 URL과 기관 연결이 이미 등록되어 있습니다.',
          CONFLICT: '기존 canonical URL의 수집 방식이 충돌합니다.', INVALID: 'URL 형식이 올바르지 않습니다.'}};
        const makeRow = (index, classification) => ({{
          row_number: index + 2, raw_agency: index === 0 ? '신규 기관' : '기존 기관',
          resolved_agency: index === 0 ? '신규 기관' : '기존 기관', agency_type: 'PUBLIC_INSTITUTION',
          region_code: null, resolved_org_unit: index % 2 ? '데이터부' : null,
          raw_url: 'https://compact.example/very/long/source/path/' + index + '?searchType=0000&detail=full',
          normalized_url: 'https://compact.example/very/long/source/path/' + index + '?searchType=0000&detail=full',
          collection_method: 'WEB_PAGE', api_kind: null, scheduled_refresh_enabled: index % 2 === 0,
          description: '전체 내용을 확인할 수 있는 소스 설명 ' + index,
          classification, message: messages[classification] || '등록 가능합니다.',
          importable: classification === 'NEW_AGENCY' || classification === 'READY',
        }});
        const fixtureRows = categories.map((category, index) => makeRow(index, category));
        const summary = {{total: 8, importable: 2, new_agencies: 1, new_org_units: 0, new_sources: 2,
          existing_source_new_bindings: 0, duplicates: 4, conflicts: 1, invalid: 1}};
        const filters = {{
          all: fixtureRows,
          importable: fixtureRows.filter((row) => row.importable),
          duplicate: fixtureRows.filter((row) => row.classification === 'EXACT_DUPLICATE'),
          conflict: fixtureRows.filter((row) => row.classification === 'CONFLICT'),
          invalid: fixtureRows.filter((row) => row.classification === 'INVALID'),
        }};
        const largeRows = Array.from({{length: 58}}, (_, index) => makeRow(index, 'READY'));
        let submissions = 0;
        let deletes = 0;
        let confirms = 0;
        let requestedPageSize = null;
        window.fetch = async (target, options = {{}}) => {{
          const value = String(target);
          if (options.method === 'DELETE') {{ deletes += 1; return response({{discarded: true}}); }}
          if (value.endsWith('/imports/preview')) {{
            submissions += 1;
            if (submissions === 1) return response({{token: 'fixture-8', filename: 'acceptance-8.xlsx', rows: fixtureRows, summary,
              preview: {{filter: 'all', page: 1, page_size: 50, total: 8, pages: 1, has_previous: false, has_next: false}}}});
            const largeSummary = {{...summary, total: 58, importable: 58, new_agencies: 0, new_sources: 58,
              duplicates: 0, conflicts: 0, invalid: 0}};
            return response({{token: 'fixture-58', filename: 'pagination-58.xlsx', rows: largeRows.slice(0, 50), summary: largeSummary,
              preview: {{filter: 'all', page: 1, page_size: 50, total: 58, pages: 2, has_previous: false, has_next: true}}}});
          }}
          if (value.includes('/preview?')) {{
            const query = new URL(value, 'https://local.test').searchParams;
            requestedPageSize = query.get('page_size');
            if (submissions === 1) {{
              const selected = filters[query.get('filter')] || fixtureRows;
              return response({{rows: selected, summary, preview: {{filter: query.get('filter'), page: 1, page_size: 50,
                total: selected.length, pages: 1, has_previous: false, has_next: false}}}});
            }}
            const largeSummary = {{...summary, total: 58, importable: 58, new_agencies: 0, new_sources: 58,
              duplicates: 0, conflicts: 0, invalid: 0}};
            return response({{rows: largeRows.slice(50), summary: largeSummary,
              preview: {{filter: 'all', page: 2, page_size: 50, total: 58, pages: 2, has_previous: true, has_next: false}}}});
          }}
          if (value.includes('/confirm?')) {{ confirms += 1; return response({{}}); }}
          return response({{job: null, jobs: []}});
        }};
        const assignFile = (name) => {{
          const bytes = Uint8Array.from(atob('{encoded}'), (char) => char.charCodeAt(0));
          const transfer = new DataTransfer();
          transfer.items.add(new File([bytes], name, {{type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}}));
          document.querySelector('[data-import-preview] input[type="file"]').files = transfer.files;
        }};
        const selectFilter = async (value) => {{
          const control = document.querySelector('[data-import-filter]');
          control.value = value;
          control.dispatchEvent(new Event('change', {{bubbles: true}}));
          await delay(35);
          return document.querySelectorAll('[data-import-rows] tr').length;
        }};
        try {{
          const documentHeightBeforeModal = document.documentElement.scrollHeight;
          document.querySelector('[data-modal-open="excel-import"]').click();
          assignFile('acceptance-8.xlsx');
          document.querySelector('[data-import-preview] button[type="submit"]').click();
          await delay(70);
          const modal = document.querySelector('.modal-shell--import');
          const footer = document.querySelector('[data-import-footer]');
          const scroll = document.querySelector('[data-import-table-scroll]');
          const footerBefore = footer.getBoundingClientRect();
          const modalRect = modal.getBoundingClientRect();
          const summaryLines = new Set(Array.from(document.querySelectorAll('.import-preview-summary .import-summary-item'))
            .map((item) => Math.round(item.getBoundingClientRect().top))).size;
          const summaryText = Array.from(document.querySelectorAll('.import-preview-summary .import-summary-item'))
            .map((item) => item.textContent.trim());
          const initialRows = document.querySelectorAll('[data-import-rows] tr').length;
          scroll.scrollTop = scroll.scrollHeight;
          const lastRow = document.querySelector('[data-import-rows] tr:last-child').getBoundingClientRect();
          const scrollRect = scroll.getBoundingClientRect();
          const reachable = lastRow.bottom <= scrollRect.bottom + 2;
          const filterCounts = {{all: await selectFilter('all'), importable: await selectFilter('importable'),
            duplicate: await selectFilter('duplicate'), conflict: await selectFilter('conflict'), invalid: await selectFilter('invalid')}};
          const deletesBeforeChange = deletes;
          document.querySelector('[data-import-collect-after-confirm]').click();
          const checked = document.querySelector('[data-import-collect-after-confirm]').checked;
          document.querySelector('[data-import-file-change]').click();
          await delay(25);
          const changeWorks = !document.querySelector('[data-import-preview]').hidden
            && document.querySelector('[data-import-preview-result]').hidden && deletes === 1;
          assignFile('pagination-58.xlsx');
          document.querySelector('[data-import-preview] button[type="submit"]').click();
          await delay(70);
          const maxRendered = document.querySelectorAll('[data-import-rows] tr').length;
          scroll.scrollTop = scroll.scrollHeight;
          document.querySelector('[data-import-next]').click();
          await delay(40);
          const pageTwoRows = document.querySelectorAll('[data-import-rows] tr').length;
          const pageTwoStatus = document.querySelector('[data-import-page-status]').textContent;
          const scrollReset = scroll.scrollTop === 0;
          scroll.scrollTop = scroll.scrollHeight;
          const footerAfter = footer.getBoundingClientRect();
          const actionsRect = footer.querySelector('div').getBoundingClientRect();
          const checkRect = footer.querySelector('.check-row').getBoundingClientRect();
          const overlap = !(checkRect.right <= actionsRect.left || actionsRect.right <= checkRect.left
            || checkRect.bottom <= actionsRect.top || actionsRect.bottom <= checkRect.top);
          const fullUrlAccessible = Boolean(document.querySelector('[data-import-rows] td:nth-child(5)')?.title);
          const fullMessageAccessible = Boolean(document.querySelector('[data-import-rows] td:nth-child(8)')?.title);
          document.querySelector('[data-import-footer] [data-modal-close]').click();
          await delay(20);
          document.body.dataset.compactImportContract = JSON.stringify({{
            viewport: [window.innerWidth, window.innerHeight], initialRows, summaryLines, summaryText, filterCounts,
            compactFilename: document.querySelector('[data-import-filename]').textContent,
            formCompact: document.querySelector('[data-import-preview]').hidden,
            tableOverflow: getComputedStyle(scroll).overflowY,
            stickyHeader: getComputedStyle(document.querySelector('.import-preview-scroll thead th')).position,
            reachable, footerVisible: footerBefore.top >= modalRect.top && footerBefore.bottom <= modalRect.bottom,
            footerStable: Math.abs(footerBefore.bottom - footerAfter.bottom) <= 1,
            modalContained: modalRect.top >= 0 && modalRect.bottom <= window.innerHeight,
            documentOverflow: document.documentElement.scrollHeight > documentHeightBeforeModal + 1,
            overlap, checked, deletesBeforeChange, changeWorks, maxRendered, pageTwoRows, pageTwoStatus,
            requestedPageSize, scrollReset, fullUrlAccessible, fullMessageAccessible,
            confirms, cancelled: document.querySelector('[data-modal="excel-import"]').hidden, deletes,
          }});
        }} catch (error) {{ document.body.dataset.compactImportError = error.stack || String(error); }}
      }}, 50);
    """
    soup.body.append(harness)
    return str(soup)


def test_import_modal_has_dedicated_scroll_and_footer_owners(tmp_path):
    soup = BeautifulSoup(_sources_page(tmp_path), "html.parser")
    shell = soup.select_one('[data-modal="excel-import"] .modal-shell--import')
    workflow = shell.select_one(':scope > [data-import-workflow]')
    preview = workflow.select_one('[data-import-preview-result]')
    assert shell.select_one(':scope > .modal-heading') is not None
    assert preview.select_one('[data-import-table-scroll]') is not None
    assert workflow.select_one(':scope > [data-import-footer]') is not None
    assert preview.select_one('[data-import-confirm]') is None
    assert workflow.select_one('[data-import-file-compact]') is not None


@pytest.mark.parametrize("viewport", [(1440, 900), (1280, 720), (1024, 768), (900, 600)])
def test_compact_import_production_dom_headless_acceptance(tmp_path, viewport):
    browser = next((candidate for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ) if candidate.exists()), None)
    if browser is None:
        pytest.skip("Installed Chromium is required for the 09B import UX contract")
    html_path = tmp_path / f"compact-import-{viewport[0]}x{viewport[1]}.html"
    html_path.write_text(_production_harness(_sources_page(tmp_path), b"09B fixture"), encoding="utf-8")
    completed = subprocess.run([
        str(browser), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={tmp_path / ('chrome-' + str(viewport[0]) + '-' + str(viewport[1]))}",
        f"--window-size={viewport[0]},{viewport[1]}", "--virtual-time-budget=5000", "--dump-dom", html_path.as_uri(),
    ], check=True, capture_output=True, text=True, encoding="utf-8", timeout=35)
    rendered = BeautifulSoup(completed.stdout, "html.parser")
    assert "data-compact-import-error" not in rendered.body.attrs, rendered.body.get("data-compact-import-error")
    result = json.loads(rendered.body["data-compact-import-contract"])
    assert result["initialRows"] == 8
    assert result["summaryLines"] <= 2
    assert result["summaryText"] == ["입력 행8", "등록 가능2", "신규 기관1", "신규 부서0", "신규 Source2",
        "기존 Source 새 연결0", "중복4", "충돌1", "오류1"]
    assert result["filterCounts"] == {"all": 8, "importable": 2, "duplicate": 4, "conflict": 1, "invalid": 1}
    assert result["tableOverflow"] == "auto" and result["stickyHeader"] == "sticky"
    assert result["reachable"] and result["footerVisible"] and result["footerStable"]
    assert result["modalContained"] and not result["documentOverflow"] and not result["overlap"]
    assert result["checked"] and result["deletesBeforeChange"] == 0 and result["changeWorks"]
    assert result["maxRendered"] == 50 and result["pageTwoRows"] == 8
    assert result["pageTwoStatus"] == "58건 중 2 / 2 페이지"
    assert result["requestedPageSize"] == "50" and result["scrollReset"]
    assert result["fullUrlAccessible"] and result["fullMessageAccessible"]
    assert result["confirms"] == 0 and result["cancelled"] and result["deletes"] == 2
