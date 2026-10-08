from __future__ import annotations

import json
from pathlib import Path
import subprocess
from urllib.parse import urlparse

from alembic import command
from alembic.config import Config
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import pytest

from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    ChangeDetection, ChangeEventType, DetectedChangeCandidate, EntityType, Observation,
    OrgUnit, ReviewStatus, RunStatus, Source, SourceCoverageMode,
)
from app.models.common import utc_now
from app.services.master_promotion_apply_service import MasterPromotionApplyService
from tests.support import regression_app
from tests.test_04b_master_review import add_context, add_extraction


LONG_AGENCY = "매우 긴 공공기관 이름을 사용하는 시민건강안전정책지원본부"
LONG_DEPARTMENT = "시민건강안전정책기획조정과 공중위생데이터통합관리팀"
LONG_DUTY = "식품제조가공업 인허가와 위생점검 및 시민 문의 대응 자료를 통합 관리하는 매우 긴 담당 업무 설명"
LONG_URL = "https://example.go.kr/very/long/path/with/many/segments?parameter=long-value&category=public-health-directory"
LONG_CURRENT = "기존 확정 값과 관련 맥락을 함께 보여 주기 위한 매우 긴 현재 연락처 값 02-1111-2222 내선 12345"
LONG_DISCOVERED = "새로 발견된 값과 관련 맥락을 함께 보여 주기 위한 매우 긴 발견 연락처 값 02-9999-8888 내선 98765"


@pytest.fixture()
def long_cell_db(tmp_path, monkeypatch):
    database_path = tmp_path / "workspace-long-cells.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(database_url)
    factory = create_session_factory(engine)
    with factory() as session:
        agency_id, source_id, org_id = add_context(
            session, url=LONG_URL, agency_name=LONG_AGENCY,
        )
        session.get(OrgUnit, org_id).name = LONG_DEPARTMENT
        source = session.get(Source, source_id)
        source.title = LONG_URL
        session.commit()

        extraction_id, observation_id, _record_id = add_extraction(
            session, source_id, org=LONG_DEPARTMENT, duty=LONG_DUTY,
            phone="02-1111-2222", email="long-contact-address@example.go.kr",
        )
        MasterPromotionApplyService(session).apply(extraction_id)
        observation = session.get(Observation, observation_id)
        observation.page_url = LONG_URL
        observation.final_url = LONG_URL
        detection = ChangeDetection(
            source_id=source_id, observation_id=observation_id,
            extraction_run_id=extraction_id, agency_id=agency_id,
            status=RunStatus.SUCCESS, coverage_mode=SourceCoverageMode.UNKNOWN,
            detector_name="long-cell-fixture", detector_version="1",
            candidates_found=1, started_at=utc_now(), finished_at=utc_now(),
        )
        session.add(detection)
        session.flush()
        session.add(DetectedChangeCandidate(
            detection_run_id=detection.id, source_id=source_id,
            observation_id=observation_id, agency_id=agency_id,
            entity_type=EntityType.CONTACT_POINT,
            proposed_event_type=ChangeEventType.OTHER,
            old_value={"value": LONG_CURRENT, "duty_title": LONG_DUTY},
            new_value={"value": LONG_DISCOVERED, "duty_title": LONG_DUTY},
            reason="긴 값 표시와 근거 context 줄바꿈을 검증하는 fixture",
            review_status=ReviewStatus.PENDING_REVIEW,
            candidate_key="workspace-long-cell-review", actionable=True,
        ))
        session.commit()
    yield database_url, tmp_path
    engine.dispose()


def _inline_assets(page_html: str) -> BeautifulSoup:
    soup = BeautifulSoup(page_html, "html.parser")
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


def test_workspace_long_cells_use_shared_server_rendered_contract(long_cell_db):
    database_url, root = long_cell_db
    with TestClient(regression_app(database_url, project_root=root)) as client:
        pages = {path: BeautifulSoup(client.get(path).text, "html.parser") for path in (
            "/sources", "/contacts", "/review", "/runs",
        )}
    markers = {
        "/sources": ("source-url", LONG_URL),
        "/contacts": ("contact-duty", LONG_DUTY),
        "/review": ("review-value", LONG_CURRENT),
        "/runs": ("run-source", LONG_URL),
    }
    for path, (marker, expected) in markers.items():
        cell = pages[path].select_one(f'[data-long-cell="{marker}"]')
        assert cell is not None
        text = cell.select_one(".workspace-cell__text")
        assert text is not None and text.get_text(strip=True) == expected
        assert text.get("title") == expected
        row = cell.find_parent("tr")
        assert row.get("tabindex") == "0" and row.get("aria-selected") == "true"
    assert LONG_URL in pages["/sources"].select_one(".detail-view:not([hidden]) .url-cell").get_text()
    assert LONG_DUTY in pages["/contacts"].select_one(".detail-view:not([hidden])").get_text()
    assert LONG_CURRENT in pages["/review"].select_one(".detail-view:not([hidden])").get_text()
    assert LONG_URL in pages["/runs"].select_one(".detail-view:not([hidden])").get_text()


def test_actual_workspace_pages_clamp_then_expand_selected_long_value(long_cell_db):
    database_url, root = long_cell_db
    browser = next((candidate for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ) if candidate.exists()), None)
    if browser is None:
        pytest.skip("Chrome or Edge is required for workspace long-cell browser contracts")

    cases = {
        "/sources": ("source-url", LONG_URL),
        "/contacts": ("contact-duty", LONG_DUTY),
        "/review": ("review-value", LONG_CURRENT),
        "/runs": ("run-source", LONG_URL),
    }
    contracts = {}
    with TestClient(regression_app(database_url, project_root=root)) as client:
        for index, (path, (marker, expected)) in enumerate(cases.items()):
            response = client.get(path)
            assert response.status_code == 200
            soup = _inline_assets(response.text)
            harness = soup.new_tag("script")
            harness.string = f"""
              setTimeout(() => {{
                try {{
                  const cell = document.querySelector('[data-long-cell="{marker}"]');
                  const text = cell.querySelector('.workspace-cell__text');
                  const row = cell.closest('.selectable-row');
                  const tableScroll = cell.closest('.table-scroll');
                  row.classList.remove('is-selected');
                  row.setAttribute('aria-selected', 'false');
                  const previewStyle = getComputedStyle(text);
                  const previewHeight = text.getBoundingClientRect().height;
                  const previewLines = previewHeight / parseFloat(previewStyle.lineHeight);
                  const previewClipped = text.scrollHeight > text.clientHeight + 1;
                  row.click();
                  const selectedStyle = getComputedStyle(text);
                  const selectedHeight = text.getBoundingClientRect().height;
                  document.body.dataset.longCellContract = JSON.stringify({{
                    fullText: text.textContent,
                    title: text.title,
                    previewLines,
                    previewClipped,
                    selected: row.classList.contains('is-selected') && row.getAttribute('aria-selected') === 'true',
                    selectedExpanded: selectedHeight >= previewHeight,
                    selectedFullVisible: text.scrollHeight <= text.clientHeight + 1,
                    selectedWhiteSpace: selectedStyle.whiteSpace,
                    selectedWrap: selectedStyle.overflowWrap,
                    detailWrap: [...document.querySelectorAll('.detail-view:not([hidden]) .url-cell, .detail-view:not([hidden]) .detail-grid dd, .detail-view:not([hidden]) .comparison-card strong')]
                      .every((node) => getComputedStyle(node).overflowWrap === 'anywhere'),
                    scrollContained: tableScroll.getBoundingClientRect().right <= document.documentElement.clientWidth + 1,
                    documentOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
                  }});
                }} catch (error) {{ document.body.dataset.longCellError = error.stack || String(error); }}
              }}, 50);
            """
            soup.body.append(harness)
            html_path = root / f"long-cell-{index}.html"
            html_path.write_text(str(soup), encoding="utf-8")
            completed = subprocess.run(
                [
                    str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
                    "--no-default-browser-check", "--window-size=1280,1000",
                    f"--user-data-dir={root / f'chrome-profile-{index}'}",
                    "--virtual-time-budget=1500", "--dump-dom", html_path.as_uri(),
                ],
                check=True, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            rendered = BeautifulSoup(completed.stdout, "html.parser")
            assert "data-long-cell-error" not in rendered.body.attrs, rendered.body.get("data-long-cell-error")
            contracts[path] = json.loads(rendered.body["data-long-cell-contract"])

    for path, contract in contracts.items():
        assert contract["fullText"] == cases[path][1]
        assert contract["title"] == cases[path][1]
        assert contract["previewLines"] <= 2.1
        assert contract["previewClipped"] is True
        assert contract["selected"] is True
        assert contract["selectedExpanded"] is True
        assert contract["selectedFullVisible"] is True
        assert contract["selectedWhiteSpace"] == "normal"
        assert contract["selectedWrap"] == "anywhere"
        assert contract["detailWrap"] is True
        assert contract["scrollContained"] is True
        assert contract["documentOverflow"] is False
