from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urlparse

from alembic import command
from alembic.config import Config
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import pytest

from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    CollectionJob, CollectionJobItem, CollectionJobItemStatus, CollectionJobStatus,
    CollectionMethod, CollectionTriggerType, CrawlRun, ExtractedFeedItem,
    ExtractionRun, ExtractionStatus, Observation, RunStatus, Source, SourceBinding,
    StageStatus,
)
from app.services.collection_job_service import CollectionJobService
from app.services.run_history_filter import RunHistoryFilter
from app.services.run_service import RunService
from app.services.source_service import SourceService
from tests.support import regression_app
from tests.test_collection_ui import add_binding


SEOUL_DAY = date(2026, 10, 12)
GANGSEO_URL = "https://www.gangseo.seoul.kr/health/ht060301"
EXCLUDED_URL = "https://www.gangseo.seoul.kr/health/ht060304"
SEOUL_URL = "https://www.seoul.go.kr/public/seoul.do"


def _run(source_id, started_at, *, status=RunStatus.SUCCESS, records=0, config=None, stats=None):
    return CrawlRun(
        source_id=source_id, status=status,
        connection_status=StageStatus.SUCCESS, raw_status=StageStatus.SUCCESS,
        extraction_status=StageStatus.SUCCESS, started_at=started_at,
        finished_at=started_at + timedelta(seconds=3), records_observed=records,
        collection_method_snapshot=CollectionMethod.WEB_CRAWL,
        collection_kind_snapshot="HTML",
        collection_config_snapshot=config,
        collection_statistics=stats,
    )


def _job(session, source_ids, created_at, *, status=CollectionJobStatus.COMPLETED):
    failed = 1 if status in (CollectionJobStatus.COMPLETED_WITH_ERRORS, CollectionJobStatus.FAILED) else 0
    succeeded = max(0, len(source_ids) - failed)
    job = CollectionJob(
        trigger_type=CollectionTriggerType.MANUAL_SELECTION,
        status=status, priority=100, trigger_context={"scope": "fixture"},
        total_items=len(source_ids), succeeded_items=succeeded, failed_items=failed,
        skipped_items=0, created_at=created_at, started_at=created_at,
        finished_at=created_at + timedelta(seconds=2),
    )
    session.add(job)
    session.flush()
    for sequence, source_id in enumerate(source_ids, 1):
        item_status = CollectionJobItemStatus.FAILED if sequence == len(source_ids) and failed else CollectionJobItemStatus.SUCCESS
        session.add(CollectionJobItem(
            job_id=job.id, source_id=source_id, sequence=sequence,
            status=item_status, attempt_count=1,
            started_at=created_at, finished_at=created_at + timedelta(seconds=1),
            error_code="HTTP_TIMEOUT" if item_status is CollectionJobItemStatus.FAILED else None,
            error_summary="연결 시간 초과" if item_status is CollectionJobItemStatus.FAILED else None,
        ))
    return job


@pytest.fixture()
def run_history_db(tmp_path, monkeypatch):
    database_path = tmp_path / "run-history.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(database_url)
    factory = create_session_factory(engine)
    with factory() as session:
        gangseo_id, gangseo_source_id, gangseo_binding_id = add_binding(
            session, agency_name="강서구청", url=GANGSEO_URL,
        )
        gangseo_binding = session.get(SourceBinding, gangseo_binding_id)
        session.get(Source, gangseo_source_id).title = "강서구 보건소 직원안내"
        extra_source_ids = []
        for url in (EXCLUDED_URL, "https://www.gangseo.seoul.kr/health/a", "https://www.gangseo.seoul.kr/health/b"):
            binding, _ = SourceService(session).register_binding(
                url=url, agency_id=gangseo_id, org_unit_id=gangseo_binding.org_unit_id,
            )
            extra_source_ids.append(uuid.UUID(binding["source_id"]))
        seoul_id, seoul_source_id, _ = add_binding(
            session, agency_name="서울특별시", url=SEOUL_URL,
        )
        source_ids = [gangseo_source_id, *extra_source_ids, seoul_source_id]

        acceptance_time = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
        acceptance = _run(
            gangseo_source_id, acceptance_time, records=203,
            config={
                "target_url": GANGSEO_URL, "crawl_scope": "PATH_PREFIX",
                "allowed_paths": ["/health/ht060301", "/health/ht060302", "/health/ht060303"],
                "excluded_paths": ["/health/ht060304"], "max_depth": 1,
                "max_pages": 3, "request_delay_ms": 1000,
            },
            stats={
                "pages_discovered": 4, "pages_attempted": 3, "pages_succeeded": 3,
                "pages_failed": 0, "max_depth_reached": 1,
                "robots_crawl_delay_ms": 10000, "effective_request_delay_ms": 10000,
            },
        )
        session.add(acceptance)
        session.flush()
        acceptance_observations = []
        for offset, suffix in enumerate(("ht060301", "ht060302", "ht060303")):
            observation = Observation(
                crawl_run_id=acceptance.id, source_id=gangseo_source_id,
                observed_at=acceptance_time + timedelta(seconds=offset), page_url=f"https://www.gangseo.seoul.kr/health/{suffix}",
                final_url=f"https://www.gangseo.seoul.kr/health/{suffix}",
                content_type="text/html", response_bytes=1024,
            )
            session.add(observation)
            acceptance_observations.append(observation)
        session.flush()
        extraction = ExtractionRun(
            observation_id=acceptance_observations[0].id,
            extractor_name="feed", extractor_version="fixture",
            status=ExtractionStatus.SUCCESS, started_at=acceptance_time,
            finished_at=acceptance_time, candidates_found=203,
        )
        session.add(extraction)
        session.flush()
        session.add_all([
            ExtractedFeedItem(
                extraction_run_id=extraction.id, observation_id=acceptance_observations[0].id,
                item_identity=f"item-{index}", title=f"발견 {index}",
                link=f"https://example.test/items/{index}", source_locator=f"item:{index}",
            )
            for index in range(203)
        ])

        large_observation_run = _run(
            extra_source_ids[1], acceptance_time - timedelta(hours=1), records=25,
        )
        session.add(large_observation_run)
        session.flush()
        session.add_all([
            Observation(
                crawl_run_id=large_observation_run.id, source_id=extra_source_ids[1],
                observed_at=acceptance_time + timedelta(seconds=index),
                page_url=f"https://www.gangseo.seoul.kr/health/a/{index}",
                final_url=f"https://www.gangseo.seoul.kr/health/a/{index}",
            )
            for index in range(25)
        ])

        before_run = _run(
            gangseo_source_id, datetime(2026, 10, 11, 15, 30, tzinfo=timezone.utc),
        )
        after_run = _run(
            seoul_source_id, datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc),
        )
        session.add_all([before_run, after_run])
        bulk_base = datetime(2026, 10, 19, 23, 0, tzinfo=timezone.utc)
        for index in range(99):
            session.add(_run(source_ids[index % len(source_ids)], bulk_base - timedelta(minutes=index)))

        before_job = _job(
            session, [gangseo_source_id], datetime(2026, 10, 11, 15, 30, tzinfo=timezone.utc),
        )
        after_job = _job(
            session, [seoul_source_id], datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc),
        )
        five_source_job = _job(session, source_ids, acceptance_time + timedelta(minutes=1))
        for index in range(49):
            status = CollectionJobStatus.COMPLETED_WITH_ERRORS if index == 1 else CollectionJobStatus.COMPLETED
            _job(
                session, [source_ids[index % len(source_ids)]],
                bulk_base - timedelta(minutes=index), status=status,
            )
        session.commit()
        ids = {
            "gangseo": gangseo_id, "seoul": seoul_id,
            "acceptance": acceptance.id, "before_run": before_run.id, "after_run": after_run.id,
            "large_observation_run": large_observation_run.id,
            "before_job": before_job.id, "after_job": after_job.id,
            "five_source_job": five_source_job.id,
        }
    yield database_url, factory, tmp_path, ids
    engine.dispose()


def test_shared_filter_contract_search_combinations_and_kst(run_history_db):
    _url, factory, _root, ids = run_history_db
    with factory() as session:
        runs = RunService(session)
        jobs = CollectionJobService(session)

        by_url = RunHistoryFilter.normalize(search="  ht060301  ")
        run_page = runs.list_page(filters=by_url, page_size=100)
        job_page = jobs.list_page(by_url, page_size=15)
        assert run_page["pagination"]["total"] > 0
        assert all("ht060301" in item["source"] or "직원안내" in item["source"] for item in run_page["items"])
        assert job_page["pagination"]["total"] > 0
        assert sum(item["id"] == str(ids["five_source_job"]) for item in job_page["items"]) == 1

        for term, agency_id in (("강서구청", ids["gangseo"]), ("서울특별시", ids["seoul"])):
            text_run = runs.list_page(filters=RunHistoryFilter.normalize(search=term), page_size=100)
            text_job = jobs.list_page(RunHistoryFilter.normalize(search=term), page_size=15)
            agency_run = runs.list_page(filters=RunHistoryFilter.normalize(agency_id=agency_id), page_size=100)
            agency_job = jobs.list_page(RunHistoryFilter.normalize(agency_id=agency_id), page_size=15)
            assert text_run["pagination"]["total"] > 0 and text_job["pagination"]["total"] > 0
            assert agency_run["pagination"]["total"] > 0 and agency_job["pagination"]["total"] > 0

        health = RunHistoryFilter.normalize(search="health", agency_id=ids["gangseo"], status=RunStatus.SUCCESS)
        assert runs.list_page(filters=health, page_size=100)["pagination"]["total"] > 0
        assert jobs.list_page(health, page_size=15)["pagination"]["total"] > 0
        date_search = RunHistoryFilter.normalize(
            date_from=date(2026, 10, 20), date_to=date(2026, 10, 20), search="health",
        )
        combined = RunHistoryFilter.normalize(
            date_from=date(2026, 10, 20), date_to=date(2026, 10, 20),
            agency_id=ids["gangseo"], status=RunStatus.SUCCESS, search="health",
        )
        assert runs.list_page(filters=date_search, page_size=100)["pagination"]["total"] > 0
        assert jobs.list_page(date_search, page_size=15)["pagination"]["total"] > 0
        assert runs.list_page(filters=combined, page_size=100)["pagination"]["total"] > 0
        assert jobs.list_page(combined, page_size=15)["pagination"]["total"] > 0
        partial_jobs = jobs.list_page(
            RunHistoryFilter.normalize(search="health", status=RunStatus.PARTIAL), page_size=15,
        )
        assert partial_jobs["pagination"]["total"] == 1
        empty = RunHistoryFilter.normalize(search="does-not-exist")
        assert runs.list_page(filters=empty)["pagination"]["total"] == 0
        assert jobs.list_page(empty)["pagination"]["total"] == 0

        bounded = runs.list_page(filters=RunHistoryFilter.normalize(search="/health/a"), page_size=25)
        large_index = next(
            index for index, item in enumerate(bounded["items"])
            if item["id"] == str(ids["large_observation_run"])
        )
        assert len(bounded["details"][large_index]["observation_urls"]) == 20
        assert bounded["details"][large_index]["observation_urls_remaining"] == 5

        kst_day = RunHistoryFilter.normalize(date_from=SEOUL_DAY, date_to=SEOUL_DAY)
        day_runs = runs.list_page(filters=kst_day, page_size=100)["items"]
        day_jobs = jobs.list_page(kst_day, page_size=15)["items"]
        assert str(ids["before_run"]) in {item["id"] for item in day_runs}
        assert str(ids["after_run"]) not in {item["id"] for item in day_runs}
        assert str(ids["before_job"]) in {item["id"] for item in day_jobs}
        assert str(ids["after_job"]) not in {item["id"] for item in day_jobs}


def _inline_assets(page_html: str) -> BeautifulSoup:
    soup = BeautifulSoup(page_html, "html.parser")
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
    return soup


def test_runs_route_search_feedback_pagination_detail_and_reset(run_history_db):
    database_url, _factory, root, _ids = run_history_db
    query = "/runs?search=ht060301"
    with TestClient(regression_app(database_url, project_root=root)) as client:
        response = client.get(query)
        empty = client.get("/runs?search=does-not-exist")
        combined = client.get(
            "/runs?date_from=2026-10-20&date_to=2026-10-20&status=SUCCESS"
            f"&agency_id={_ids['gangseo']}&search=health"
        )
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one('#run-search')["value"] == "ht060301"
    assert soup.select_one('.run-filter-feedback strong').get_text(strip=True) == "ht060301"
    assert soup.select_one('.collection-job-history .table-count').get_text(strip=True) != "0건"
    assert soup.select_one('.run-list-panel .table-count').get_text(strip=True) != "0건"
    first_row = soup.select_one('.run-list-panel .selectable-row.is-selected')
    assert "ht060301" in first_row.get_text() or "직원안내" in first_row.get_text()
    detail = soup.select_one('.run-detail-panel .detail-view:not([hidden])')
    detail_text = detail.get_text(" ", strip=True)
    assert "Index URL · 크롤링" in detail_text
    for label in ("시작 URL", "탐색 범위", "포함 경로", "제외 경로", "시도 페이지", "성공 페이지", "실패 페이지", "실제 요청 간격"):
        assert label in detail_text
    urls = [item.get_text(strip=True) for item in detail.select('.run-observation-list li')]
    assert urls == [
        "https://www.gangseo.seoul.kr/health/ht060301",
        "https://www.gangseo.seoul.kr/health/ht060302",
        "https://www.gangseo.seoul.kr/health/ht060303",
    ]
    assert all("ht060304" not in value for value in urls)
    assert soup.select_one('a.button--ghost[href="/runs"]').get_text(strip=True) == "초기화"
    next_links = soup.select('.workspace-pager a')
    assert next_links
    for link in next_links:
        assert parse_qs(urlparse(link["href"]).query)["search"] == ["ht060301"]

    combined_soup = BeautifulSoup(combined.text, "html.parser")
    combined_links = combined_soup.select('.workspace-pager a')
    assert combined_links
    for link in combined_links:
        params = parse_qs(urlparse(link["href"]).query)
        assert params["date_from"] == params["date_to"] == ["2026-10-20"]
        assert params["agency_id"] == [str(_ids["gangseo"])]
        assert params["status"] == ["SUCCESS"]
        assert params["search"] == ["health"]

    empty_soup = BeautifulSoup(empty.text, "html.parser")
    assert "조건에 맞는 수집 작업이 없습니다." in empty_soup.get_text()
    assert "조건에 맞는 수집 이력이 없습니다." in empty_soup.get_text()


def test_runs_workspace_is_bounded_in_headless_browser(run_history_db):
    database_url, _factory, root, _ids = run_history_db
    browser = next((candidate for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ) if candidate.exists()), None)
    if browser is None:
        pytest.skip("Chrome or Edge is required for Run History layout contracts")

    with TestClient(regression_app(database_url, project_root=root)) as client:
        response = client.get("/runs")
    soup = _inline_assets(response.text)
    harness = soup.new_tag("script")
    harness.string = """
      setTimeout(() => {
        try {
          const jobs = document.querySelector('[data-run-job-scroll]');
          const runs = document.querySelector('[data-run-list-scroll]');
          const detail = document.querySelector('.run-detail-panel .detail-view:not([hidden]) .detail-content');
          const secondRow = document.querySelectorAll('.run-list-panel .selectable-row')[1];
          const beforeJobScroll = jobs.scrollTop;
          const beforeRunScroll = runs.scrollTop;
          const detailInternalScroll = detail.scrollHeight > detail.clientHeight;
          secondRow.focus();
          secondRow.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
          document.body.dataset.runHistoryContract = JSON.stringify({
            jobRows: document.querySelectorAll('[data-job-summary]').length,
            runRows: document.querySelectorAll('.run-list-panel .selectable-row').length,
            jobInternalScroll: jobs.scrollHeight > jobs.clientHeight,
            runInternalScroll: runs.scrollHeight > runs.clientHeight,
            detailInternalScroll,
            jobOverflowY: getComputedStyle(jobs).overflowY,
            runOverflowY: getComputedStyle(runs).overflowY,
            detailOverflowY: getComputedStyle(detail).overflowY,
            selectedChanged: secondRow.classList.contains('is-selected') && secondRow.getAttribute('aria-selected') === 'true',
            jobScrollPreserved: jobs.scrollTop === beforeJobScroll,
            runScrollPreserved: runs.scrollTop === beforeRunScroll,
            documentHeight: document.documentElement.scrollHeight,
            viewportHeight: document.documentElement.clientHeight,
            observationListBounded: document.querySelector('.run-observation-list')?.scrollHeight <= 181,
            horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
          });
        } catch (error) { document.body.dataset.runHistoryError = error.stack || String(error); }
      }, 80);
    """
    soup.body.append(harness)
    html_path = root / "run-history-bounded.html"
    html_path.write_text(str(soup), encoding="utf-8")
    completed = subprocess.run(
        [
            str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--window-size=1440,1000",
            f"--user-data-dir={root / 'chrome-profile-run-history'}",
            "--virtual-time-budget=1600", "--dump-dom", html_path.as_uri(),
        ],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    rendered = BeautifulSoup(completed.stdout, "html.parser")
    assert "data-run-history-error" not in rendered.body.attrs, rendered.body.get("data-run-history-error")
    contract = json.loads(rendered.body["data-run-history-contract"])
    assert contract["jobRows"] == 10
    assert contract["runRows"] == 25
    assert contract["jobInternalScroll"] is True
    assert contract["runInternalScroll"] is True
    assert contract["detailInternalScroll"] is True
    assert contract["jobOverflowY"] == contract["runOverflowY"] == contract["detailOverflowY"] == "auto"
    assert contract["selectedChanged"] is True
    assert contract["jobScrollPreserved"] is True
    assert contract["runScrollPreserved"] is True
    assert contract["documentHeight"] <= contract["viewportHeight"] * 2
    assert contract["observationListBounded"] is True
    assert contract["horizontalOverflow"] is False
