from __future__ import annotations

import uuid

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.collectors.html_contact_extractor import HTMLContactExtractor
from app.collectors.http_fetcher import HTTPFetcher
from app.collectors.staff_directory_extractor import StaffDirectoryExtractor
from app.core.discovery_quality import ContactScope, classify_contact_scope
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    AgencyType,
    CandidateType,
    CrawlRun,
    DetectedChangeCandidate,
    DetectionMethod,
    DirectoryRecordType,
    ExtractedContactCandidate,
    ExtractedDirectoryRecord,
    ExtractionRun,
    ExtractionStatus,
    Observation,
    RunStatus,
    SourceOccurrence,
    StageStatus,
)
from app.models.common import utc_now
from app.services.agency_service import AgencyService
from app.services.collection_service import CollectionCoordinator, CollectionService
from app.services.discovery_read_service import DiscoveryReadService
from app.services.master_promotion_planner import MasterPromotionPlanner
from app.services.source_change_detection_service import SourceChangeDetectionService
from app.services.source_service import SourceService
from tests.support import regression_app


SEOUL_NESTED = """
<html><body><footer id="footer"><div class="contact">
  <div>대표전화 02-731-2120</div>
  <div><a href="tel:02-731-2120">02-731-2120</a></div>
  <p>대표전화: 02-731-2120</p>
</div></footer></body></html>
"""
BUSAN_EMPTY = """
<table class="boardList"><thead><tr>
  <th>No.</th><th>부서</th><th>부서</th><th>직위</th>
  <th>담당업무</th><th>전화번호</th><th>상세</th><th>전화</th>
</tr></thead><tbody><tr><td colspan="8">등록된 데이터가 없습니다.</td></tr></tbody></table>
"""
GANGSEO_STAFF = """
<main><table><thead><tr><th>직급</th><th>담당업무</th><th>전화번호</th></tr></thead>
<tbody>
  <tr><td>팀장</td><td>위생감시팀 업무 총괄</td><td>02-2600-5833</td></tr>
  <tr><td>주무관</td><td>농수산물 원산지 관리</td><td>02-2600-5843</td></tr>
</tbody></table></main>
"""
PUBLIC_DNS = lambda _host: {"93.184.216.34"}


@pytest.fixture()
def quality_db(tmp_path, monkeypatch):
    path = tmp_path / "db" / "quality.sqlite3"
    path.parent.mkdir()
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield url, factory, tmp_path
    engine.dispose()


def add_source(session, url: str = "https://example.org/staff"):
    agency, _ = AgencyService(session).create_agency(
        official_name="품질 검증 기관",
        agency_type=AgencyType.OTHER,
    )
    agency_id = uuid.UUID(agency["id"])
    binding, _ = SourceService(session).register_binding(
        url=url,
        agency_id=agency_id,
    )
    return agency_id, uuid.UUID(binding["source_id"])


def add_historical_evidence(session):
    agency_id, source_id = add_source(session)
    now = utc_now()
    run = CrawlRun(
        source_id=source_id,
        status=RunStatus.SUCCESS,
        connection_status=StageStatus.SUCCESS,
        raw_status=StageStatus.SUCCESS,
        extraction_status=StageStatus.SUCCESS,
        started_at=now,
        finished_at=now,
        records_observed=4,
        http_status=200,
    )
    session.add(run)
    session.flush()
    observation = Observation(
        crawl_run_id=run.id,
        source_id=source_id,
        observed_at=now,
        page_url="https://example.org/staff",
    )
    session.add(observation)
    session.flush()
    contacts = ExtractionRun(
        observation_id=observation.id,
        extractor_name="html_contact",
        extractor_version="2",
        status=ExtractionStatus.SUCCESS,
        started_at=now,
        finished_at=now,
        candidates_found=3,
    )
    directory = ExtractionRun(
        observation_id=observation.id,
        extractor_name="staff_directory",
        extractor_version="1",
        status=ExtractionStatus.SUCCESS,
        started_at=now,
        finished_at=now,
        candidates_found=1,
    )
    session.add_all([contacts, directory])
    session.flush()
    for index, locator in enumerate(("footer > div", "footer > div > p", "footer > div > p > a")):
        session.add(ExtractedContactCandidate(
            extraction_run_id=contacts.id,
            observation_id=observation.id,
            candidate_type=CandidateType.PHONE,
            raw_value="02-731-2120",
            normalized_value="027312120",
            context_text="대표전화 02-731-2120" if index == 2 else "대표전화 안내 02-731-2120",
            source_locator=locator,
            detection_method=DetectionMethod.TEL_LINK if index == 2 else DetectionMethod.TEXT_PATTERN,
        ))
    session.add(ExtractedDirectoryRecord(
        extraction_run_id=directory.id,
        observation_id=observation.id,
        record_type=DirectoryRecordType.STAFF_DIRECTORY_ROW,
        org_unit_text="등록된 데이터가 없습니다.",
        duty_text="등록된 데이터가 없습니다.",
        position_text="등록된 데이터가 없습니다.",
        phone_text="등록된 데이터가 없습니다.",
        row_text="등록된 데이터가 없습니다.",
        source_locator="table > tbody > tr",
    ))
    session.commit()
    return agency_id, source_id, run.id, observation.id, contacts.id, directory.id


def test_contact_dedup_uses_type_and_normalized_value_with_specific_context():
    candidates = HTMLContactExtractor().extract(SEOUL_NESTED)
    assert len(candidates) == 1
    assert candidates[0].normalized_value == "027312120"
    assert candidates[0].source_locator.endswith("a:nth-of-type(1)")
    assert classify_contact_scope(
        candidates[0].source_locator,
        candidates[0].context_text,
    ) is ContactScope.SITE_WIDE


@pytest.mark.parametrize("placeholder", [
    "등록된 데이터가 없습니다.",
    "등록된 자료가 없습니다",
    "검색 결과가 없습니다!",
    "조회   결과가 없습니다.",
    "자료가 없습니다",
    "데이터가 없습니다.",
    "해당 정보가 없습니다",
])
def test_directory_placeholder_rows_are_rejected(placeholder):
    html = BUSAN_EMPTY.replace("등록된 데이터가 없습니다.", placeholder)
    assert StaffDirectoryExtractor().extract(html) == []


def test_position_duty_phone_table_is_a_directory_without_inventing_person():
    records = StaffDirectoryExtractor().extract(GANGSEO_STAFF)
    assert [(record.position_text, record.phone_text) for record in records] == [
        ("팀장", "02-2600-5833"),
        ("주무관", "02-2600-5843"),
    ]
    assert all(record.org_unit_text is None for record in records)
    assert all(record.person_name_text is None for record in records)


def test_scope_prefers_dom_structure_over_keywords():
    assert classify_contact_scope("html > body > footer#footer > p", "대표전화 02-2600-6114") is ContactScope.SITE_WIDE
    assert classify_contact_scope("html > body > main > table > tr > td", "대표전화 업무 담당 02-2600-5835") is ContactScope.BUSINESS
    assert classify_contact_scope("html > body > div.notice", "연락처 02-2600-5000") is ContactScope.UNKNOWN


def test_historical_read_dedups_without_rewriting_and_filters_placeholder(quality_db):
    url, factory, root = quality_db
    with factory() as session:
        agency_id, source_id, run_id, observation_id, contacts_id, directory_id = add_historical_evidence(session)
        reader = DiscoveryReadService(session)
        summary = reader.summary(run_id)
        page = reader.page(run_id, page_size=500)
        assert summary["valid_contacts"] == 1
        assert summary["site_wide_contacts"] == 1
        assert summary["directory_records"] == 0
        assert summary["meaningful_total"] == 1
        assert len(page["items"]) == 1
        assert page["pagination"]["page_size"] == 100
        assert session.scalar(select(func.count()).select_from(ExtractedContactCandidate)) == 3
        assert session.scalar(select(func.count()).select_from(ExtractedDirectoryRecord)) == 1

        specs = SourceChangeDetectionService(session)._generic_specs(observation_id, source_id, agency_id)
        assert len(specs) == 1
        assert session.scalar(select(func.count()).select_from(DetectedChangeCandidate)) == 0
        assert session.scalar(select(func.count()).select_from(SourceOccurrence)) == 0

        plan = MasterPromotionPlanner(session).plan(directory_id, agency_id)
        assert plan.rows == []
        assert plan.summary()["directory_records"] == 0

    app = regression_app(url, project_root=root)
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/discoveries?page=1&page_size=50")
        too_large = client.get(f"/api/runs/{run_id}/discoveries?page_size=101")
        page_html = client.get("/runs")
    assert response.status_code == 200
    assert response.json()["pagination"]["total"] == 1
    assert too_large.status_code == 422
    assert "발견 데이터 보기" in page_html.text
    assert "유효 발견" in page_html.text
    assert "02-731-2120" not in page_html.text


def test_future_collection_counts_semantic_discoveries_without_external_http(quality_db):
    _url, factory, root = quality_db
    with factory() as session:
        _, seoul_source = add_source(session, "https://example.org/seoul")
        transport = httpx.MockTransport(lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=SEOUL_NESTED.encode(),
        ))
        result = CollectionService(
            session,
            project_root=root,
            raw_root=root / "data" / "raw",
            coordinator=CollectionCoordinator(),
            fetcher=HTTPFetcher(transport=transport, resolver=PUBLIC_DNS),
        ).collect(seoul_source)
        assert result.crawl_run.records_observed == 1
        assert result.contact_candidates == 1

    with factory() as session:
        _, busan_source = add_source(session, "https://example.org/busan")
        transport = httpx.MockTransport(lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=BUSAN_EMPTY.encode(),
        ))
        result = CollectionService(
            session,
            project_root=root,
            raw_root=root / "data" / "raw",
            coordinator=CollectionCoordinator(),
            fetcher=HTTPFetcher(transport=transport, resolver=PUBLIC_DNS),
        ).collect(busan_source)
        assert result.crawl_run.records_observed == 0
        assert result.directory_records == 0
