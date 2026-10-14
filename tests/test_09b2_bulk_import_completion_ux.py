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


PARTIAL_QUEUED = {
    "summary": {
        "input_rows": 8, "created_agencies": 1, "created_org_units": 0,
        "created_sources": 2, "created_bindings": 2,
        "existing_source_new_bindings": 0, "duplicates_skipped": 4,
        "conflicts": 1, "invalid_rows": 1, "unexpected_failures": 0,
    },
    "collection_requested": True, "collection_source_count": 2,
    "collection_queue_status": "QUEUED", "collection_job_id": "job-09b2",
}


def _sources_page(tmp_path: Path) -> str:
    database = tmp_path / "completion-ux.sqlite3"
    url = f"sqlite:///{database.as_posix()}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(regression_app(url, project_root=tmp_path)) as client:
        return client.get("/sources").text


def _completion_harness(page: str, result: dict, collect: bool) -> str:
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

    encoded = base64.b64encode(b"09B-2 completion fixture").decode()
    result_json = json.dumps(result, ensure_ascii=False)
    collect_json = json.dumps(collect)
    harness = soup.new_tag("script")
    harness.string = f"""
      setTimeout(async () => {{
        const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const response = (payload) => ({{ok: true, status: 200, json: async () => payload}});
        const confirmResult = {result_json};
        const previewSummary = {{total: 8, importable: 2, new_agencies: 1, new_org_units: 0,
          new_sources: 2, existing_source_new_bindings: 0, duplicates: 4, conflicts: 1, invalid: 1}};
        const previewRows = [{{row_number: 2, raw_agency: '완료 기관', resolved_agency: '완료 기관',
          agency_type: 'PUBLIC_INSTITUTION', region_code: null, resolved_org_unit: null,
          raw_url: 'https://completion.example/source', normalized_url: 'https://completion.example/source',
          collection_method: 'WEB_PAGE', api_kind: null, scheduled_refresh_enabled: true,
          description: '', classification: 'READY', message: '등록 가능합니다.', importable: true}}];
        let confirms = 0;
        let deletes = 0;
        window.fetch = async (target, options = {{}}) => {{
          const value = String(target);
          if (options.method === 'DELETE') {{ deletes += 1; return response({{discarded: true}}); }}
          if (value.endsWith('/imports/preview')) return response({{token: 'completion-token', filename: 'completion.xlsx',
            rows: previewRows, summary: previewSummary,
            preview: {{filter: 'all', page: 1, page_size: 50, total: 1, pages: 1, has_previous: false, has_next: false}}}});
          if (value.includes('/confirm?')) {{ confirms += 1; await delay(20); return response(confirmResult); }}
          return response({{}});
        }};
        try {{
          document.querySelector('[data-modal-open="excel-import"]').click();
          const transfer = new DataTransfer();
          const bytes = Uint8Array.from(atob('{encoded}'), (char) => char.charCodeAt(0));
          transfer.items.add(new File([bytes], 'completion.xlsx', {{type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}}));
          document.querySelector('[data-import-preview] input[type="file"]').files = transfer.files;
          document.querySelector('[data-import-preview] button[type="submit"]').click();
          await delay(45);
          if ({collect_json}) document.querySelector('[data-import-collect-after-confirm]').click();
          const confirmButton = document.querySelector('[data-import-confirm]');
          confirmButton.click();
          confirmButton.click();
          await delay(90);
          const shell = document.querySelector('.modal-shell--import');
          const complete = document.querySelector('[data-import-complete]');
          const actions = document.querySelector('.import-complete-actions');
          const collection = document.querySelector('[data-import-collection-state]');
          const shellRect = shell.getBoundingClientRect();
          const actionsRect = actions.getBoundingClientRect();
          const collectionRect = collection.getBoundingClientRect();
          const paired = Array.from(document.querySelectorAll('.import-complete-primary > div, .import-complete-secondary > div'))
            .every((item) => item.querySelectorAll(':scope > dt').length === 1 && item.querySelectorAll(':scope > dd').length === 1);
          const primary = Array.from(document.querySelectorAll('.import-complete-primary > div')).map((item) => item.textContent.trim());
          const secondary = Array.from(document.querySelectorAll('.import-complete-secondary > div')).map((item) => item.textContent.trim());
          const completion = {{
            title: document.querySelector('[data-import-modal-title]').textContent,
            subtitle: document.querySelector('[data-import-complete-subtitle]').textContent,
            outcome: document.querySelector('[data-import-complete-outcome]').textContent,
            primary, secondary, paired,
            shellHeight: shellRect.height,
            modalContained: shellRect.top >= 0 && shellRect.bottom <= window.innerHeight,
            footerVisible: actionsRect.top >= shellRect.top && actionsRect.bottom <= shellRect.bottom + 1,
            excessBlank: actionsRect.top - collectionRect.bottom > 40,
            previewHidden: document.querySelector('[data-import-preview-result]').hidden,
            uploadHidden: document.querySelector('[data-import-preview]').hidden,
            stepsHidden: getComputedStyle(document.querySelector('.import-steps')).display === 'none',
            completeClass: shell.classList.contains('is-complete'),
            collectionState: collection.dataset.state,
            collectionBadge: document.querySelector('[data-import-collection-badge]').textContent,
            collectionTitle: document.querySelector('[data-import-collection-title]').textContent,
            collectionMessage: document.querySelector('[data-import-collection-result]').textContent,
            collectionSuccessClaimed: complete.innerText.includes('수집 성공'),
            historyVisible: !document.querySelector('[data-import-history]').hidden,
            historyHref: document.querySelector('[data-import-history]').getAttribute('href'),
            sourceHref: document.querySelector('[data-import-refresh]').getAttribute('href'),
          }};
          document.querySelector('.modal-shell--import > .modal-heading [data-modal-close]').click();
          document.querySelector('[data-modal-open="excel-import"]').click();
          await delay(15);
          completion.reopenReset = !shell.classList.contains('is-complete')
            && !document.querySelector('[data-import-preview]').hidden
            && document.querySelector('[data-import-complete]').hidden
            && document.querySelector('[data-import-modal-title]').textContent === '수집 소스 대량 등록';
          completion.confirms = confirms;
          completion.deletes = deletes;
          document.body.dataset.completionContract = JSON.stringify(completion);
        }} catch (error) {{ document.body.dataset.completionError = error.stack || String(error); }}
      }}, 50);
    """
    soup.body.append(harness)
    return str(soup)


def _run_chromium(tmp_path: Path, result: dict, collect: bool, viewport: tuple[int, int]) -> dict:
    browser = next((candidate for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ) if candidate.exists()), None)
    if browser is None:
        pytest.skip("Installed Chromium is required for the 09B-2 completion contract")
    html_path = tmp_path / f"completion-{viewport[0]}x{viewport[1]}.html"
    html_path.write_text(_completion_harness(_sources_page(tmp_path), result, collect), encoding="utf-8")
    completed = subprocess.run([
        str(browser), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={tmp_path / ('chrome-' + str(viewport[0]) + '-' + str(viewport[1]))}",
        f"--window-size={viewport[0]},{viewport[1]}", "--virtual-time-budget=4500", "--dump-dom", html_path.as_uri(),
    ], check=True, capture_output=True, text=True, encoding="utf-8", timeout=35)
    rendered = BeautifulSoup(completed.stdout, "html.parser")
    assert "data-completion-error" not in rendered.body.attrs, rendered.body.get("data-completion-error")
    return json.loads(rendered.body["data-completion-contract"])


def test_completion_template_owns_paired_stats_and_real_actions(tmp_path):
    soup = BeautifulSoup(_sources_page(tmp_path), "html.parser")
    complete = soup.select_one('[data-import-complete]')
    items = complete.select('.import-complete-primary > div, .import-complete-secondary > div')
    assert len(items) == 9
    assert all(len(item.select(':scope > dt')) == 1 and len(item.select(':scope > dd')) == 1 for item in items)
    assert complete.select_one('[data-import-history][href="/runs"]') is not None
    assert complete.select_one('[data-import-refresh][href="/sources"]') is not None


@pytest.mark.parametrize("viewport", [(1440, 900), (1280, 720), (1024, 768), (900, 600)])
def test_partial_queued_completion_is_compact_and_truthful(tmp_path, viewport):
    result = _run_chromium(tmp_path, PARTIAL_QUEUED, True, viewport)
    assert result["title"] == "수집 소스 등록 완료"
    assert result["subtitle"] == "8개 입력 행 처리 결과"
    assert result["primary"] == ["신규 기관1개", "신규 Source2개", "등록 연결2개"]
    assert result["secondary"] == ["신규 부서0개", "기존 Source 새 연결0개", "중복 건너뜀4개", "충돌1개", "오류1개", "등록 실패0개"]
    assert result["paired"] and result["shellHeight"] <= 550
    assert result["modalContained"] and result["footerVisible"] and not result["excessBlank"]
    assert result["previewHidden"] and result["uploadHidden"] and result["stepsHidden"] and result["completeClass"]
    assert result["collectionState"] == "queued" and result["collectionBadge"] == "접수됨 · 대기 중"
    assert result["collectionTitle"] == "즉시 수집 작업 접수"
    assert result["collectionMessage"] == "신규 Source 2개가 수집 대기열에 등록되었습니다."
    assert not result["collectionSuccessClaimed"]
    assert result["historyVisible"] and result["historyHref"] == "/runs" and result["sourceHref"] == "/sources"
    assert result["confirms"] == 1 and result["deletes"] == 0 and result["reopenReset"]


SEMANTIC_CASES = [
    ("queue_failed", {
        **PARTIAL_QUEUED, "collection_queue_status": "FAILED_TO_QUEUE", "collection_job_id": None,
    }, True, "failed", "즉시 수집 작업 등록 실패", "소스 등록은 완료되었습니다. 수집 소스 목록에서 다시 수집할 수 있습니다.", False),
    ("not_requested", {
        **PARTIAL_QUEUED, "collection_requested": False, "collection_queue_status": "NOT_REQUESTED",
        "collection_source_count": 2, "collection_job_id": None,
    }, False, "idle", "즉시 수집", "즉시 수집은 요청하지 않았습니다.", False),
    ("zero_new_source", {
        "summary": {"input_rows": 8, "created_agencies": 0, "created_org_units": 0, "created_sources": 0,
            "created_bindings": 0, "existing_source_new_bindings": 0, "duplicates_skipped": 8,
            "conflicts": 0, "invalid_rows": 0, "unexpected_failures": 0},
        "collection_requested": True, "collection_source_count": 0, "collection_queue_status": "NO_NEW_SOURCES",
        "collection_job_id": None,
    }, True, "idle", "즉시 수집 작업 없음", "신규 Source가 없어 즉시 수집 작업을 만들지 않았습니다.", False),
    ("all_valid", {
        "summary": {"input_rows": 8, "created_agencies": 0, "created_org_units": 0, "created_sources": 8,
            "created_bindings": 8, "existing_source_new_bindings": 0, "duplicates_skipped": 0,
            "conflicts": 0, "invalid_rows": 0, "unexpected_failures": 0},
        "collection_requested": False, "collection_source_count": 8, "collection_queue_status": "NOT_REQUESTED",
        "collection_job_id": None,
    }, False, "idle", "즉시 수집", "즉시 수집은 요청하지 않았습니다.", False),
]


@pytest.mark.parametrize("_name,result,collect,state,title,message,history", SEMANTIC_CASES)
def test_completion_collection_and_zero_source_semantics(tmp_path, _name, result, collect, state, title, message, history):
    rendered = _run_chromium(tmp_path, result, collect, (1280, 720))
    assert rendered["title"] == "수집 소스 등록 완료"
    assert rendered["collectionState"] == state
    assert rendered["collectionTitle"] == title and rendered["collectionMessage"] == message
    assert rendered["historyVisible"] is history
    assert not rendered["collectionSuccessClaimed"]
    assert rendered["confirms"] == 1 and rendered["reopenReset"]
    if _name == "queue_failed":
        assert "완료" in rendered["outcome"] and rendered["collectionBadge"] == "등록 실패"
    if _name == "zero_new_source":
        assert rendered["primary"][1] == "신규 Source0개"
    if _name == "all_valid":
        assert rendered["outcome"] == "모든 입력 행의 등록 처리가 완료되었습니다."
