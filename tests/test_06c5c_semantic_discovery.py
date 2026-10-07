from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    AgencyType, CandidateType, ContactPoint, CrawlRun, DetectedChangeCandidate,
    DetectionMethod, DirectoryRecordType, ExtractedContactCandidate,
    ExtractedDirectoryRecord, ExtractionRun, ExtractionStatus, Observation,
    RunStatus, SourceOccurrence, StageStatus,
)
from app.models.common import utc_now
from app.services.agency_service import AgencyService
from app.services.master_promotion_apply_service import MasterPromotionApplyService
from app.services.semantic_discovery_service import (
    DEFAULT_DISCOVERY_PAGE_SIZE, MAX_DISCOVERY_PAGE_SIZE, SemanticDiscoveryProjector,
)
from app.services.source_change_detection_service import SourceChangeDetectionService
from app.services.source_service import SourceService
from tests.support import regression_app


@pytest.fixture()
def semantic_db(tmp_path, monkeypatch):
    path = tmp_path / "semantic.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield url, factory, tmp_path, engine
    engine.dispose()


def _source(session, suffix: str = "staff"):
    agency, _ = AgencyService(session).create_agency(
        official_name=f"의미 검증 기관 {suffix}", agency_type=AgencyType.OTHER,
    )
    agency_id = uuid.UUID(agency["id"])
    binding, _ = SourceService(session).register_binding(
        url=f"https://example.org/{suffix}", agency_id=agency_id,
    )
    return agency_id, uuid.UUID(binding["source_id"])


def _evidence_context(session, source_id, *, stored_count=0):
    now = utc_now()
    run = CrawlRun(
        source_id=source_id, status=RunStatus.SUCCESS,
        connection_status=StageStatus.SUCCESS, raw_status=StageStatus.SUCCESS,
        extraction_status=StageStatus.SUCCESS, started_at=now, finished_at=now,
        records_observed=stored_count, http_status=200,
    )
    session.add(run); session.flush()
    observation = Observation(
        crawl_run_id=run.id, source_id=source_id, observed_at=now,
        page_url="https://example.org/staff",
    )
    session.add(observation); session.flush()
    contacts = ExtractionRun(
        observation_id=observation.id, extractor_name="html_contact",
        extractor_version="3", status=ExtractionStatus.SUCCESS,
        started_at=now, finished_at=now, candidates_found=0,
    )
    directory = ExtractionRun(
        observation_id=observation.id, extractor_name="staff_directory",
        extractor_version="2", status=ExtractionStatus.SUCCESS,
        started_at=now, finished_at=now, candidates_found=0,
    )
    session.add_all([contacts, directory]); session.flush()
    return run, observation, contacts, directory


def _contact(session, extraction, observation, index, *, locator="main > p"):
    raw = f"02-2600-{index:04d}"
    session.add(ExtractedContactCandidate(
        extraction_run_id=extraction.id, observation_id=observation.id,
        candidate_type=CandidateType.PHONE, raw_value=raw,
        normalized_value=f"022600{index:04d}", context_text=f"연락처 {raw}",
        source_locator=locator, detection_method=DetectionMethod.TEXT_PATTERN,
    ))


def _directory(session, extraction, observation, index, *, placeholder=False):
    value = "등록된 데이터가 없습니다." if placeholder else f"02-2600-{index:04d}"
    session.add(ExtractedDirectoryRecord(
        extraction_run_id=extraction.id, observation_id=observation.id,
        record_type=DirectoryRecordType.STAFF_DIRECTORY_ROW,
        org_unit_text=value if placeholder else "위생관리과",
        duty_text=value if placeholder else f"업무 {index}",
        position_text=None if placeholder else "주무관", phone_text=value,
        row_text=value if placeholder else f"위생관리과 업무 {index} {value}",
        source_locator=f"table > tr:nth-child({index + 1})",
    ))


def test_real_gangseo_51_raw_projects_to_29_semantic_discoveries(semantic_db):
    _url, factory, _root, _engine = semantic_db
    with factory() as session:
        _agency, source_id = _source(session, "gangseo")
        run, observation, contacts, directory = _evidence_context(session, source_id, stored_count=51)
        for index in range(29):
            _contact(session, contacts, observation, index)
        for index in range(22):
            _directory(session, directory, observation, index)
        session.commit()

        projector = SemanticDiscoveryProjector(session)
        projection = projector.project(run.id)
        summary = projector.summary(run.id)
        page = projector.page(run.id)
        assert projection.raw_evidence_count == 51
        assert len(projection.generic_contacts) == 29
        assert len(projection.directories) == 22
        assert len(projection.shadowed_contacts) == 22
        assert len(projection.standalone_contacts) == 7
        assert projection.semantic_count == 29
        assert summary["semantic_discovery_count"] == 29
        assert summary["directory_records"] == 22
        assert summary["standalone_contacts"] == 7
        assert page["pagination"]["page_size"] == DEFAULT_DISCOVERY_PAGE_SIZE == 30
        assert len(page["items"]) == 29
        assert all(item["kind"] != "CONTACT" or int(item["normalized_value"][-4:]) >= 22 for item in page["items"])
        assert session.get(CrawlRun, run.id).records_observed == 51


def test_seoul_duplicate_and_busan_placeholder_semantics(semantic_db):
    _url, factory, _root, _engine = semantic_db
    with factory() as session:
        _agency, seoul_source = _source(session, "seoul")
        seoul, observation, contacts, _directory_run = _evidence_context(session, seoul_source, stored_count=3)
        for locator in ("footer", "footer > p", "footer > p > a"):
            _contact(session, contacts, observation, 6114, locator=locator)

        _agency, busan_source = _source(session, "busan")
        busan, busan_observation, _contacts, busan_directory = _evidence_context(session, busan_source, stored_count=1)
        _directory(session, busan_directory, busan_observation, 0, placeholder=True)
        session.commit()

        projector = SemanticDiscoveryProjector(session)
        assert projector.summary(seoul.id)["semantic_discovery_count"] == 1
        assert projector.summary(busan.id)["semantic_discovery_count"] == 0
        assert session.get(CrawlRun, seoul.id).records_observed == 3
        assert session.get(CrawlRun, busan.id).records_observed == 1


def test_scale_projection_paginates_after_semantic_consolidation_without_n_plus_one(semantic_db):
    _url, factory, _root, engine = semantic_db
    with factory() as session:
        _agency, source_id = _source(session, "scale")
        run, observation, contacts, directory = _evidence_context(session, source_id, stored_count=2500)
        for index in range(1500):
            _contact(session, contacts, observation, index)
        for index in range(1000):
            _directory(session, directory, observation, index)
        session.commit()

        statements = []
        def record_query(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", record_query)
        try:
            result = SemanticDiscoveryProjector(session).page(run.id, page=2, page_size=30)
        finally:
            event.remove(engine, "before_cursor_execute", record_query)
        assert result["summary"]["raw_evidence_count"] == 2500
        assert result["summary"]["semantic_discovery_count"] == 1500
        assert result["summary"]["directory_records"] == 1000
        assert result["summary"]["standalone_contacts"] == 500
        assert len(result["items"]) == 30
        assert len(statements) <= 6
        assert MAX_DISCOVERY_PAGE_SIZE == 100


def test_shadowed_generic_does_not_duplicate_contactpoint_review_or_occurrence(semantic_db):
    _url, factory, _root, _engine = semantic_db
    with factory() as session:
        agency_id, source_id = _source(session, "downstream")
        baseline, observation, contacts, directory = _evidence_context(session, source_id, stored_count=2)
        _contact(session, contacts, observation, 5833)
        _directory(session, directory, observation, 5833)
        session.commit()

        projection = SemanticDiscoveryProjector(session).project(baseline.id)
        assert projection.semantic_count == 1
        assert len(projection.shadowed_contacts) == 1
        applied = MasterPromotionApplyService(session).apply(directory.id, agency_id)
        assert applied["contacts_created"] == 1
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 1
        occurrence_count = session.scalar(select(func.count()).select_from(SourceOccurrence))

        current, current_observation, current_contacts, current_directory = _evidence_context(session, source_id, stored_count=2)
        _contact(session, current_contacts, current_observation, 5833)
        _directory(session, current_directory, current_observation, 5833)
        session.commit()
        result = SourceChangeDetectionService(session).generate(current_directory.id, agency_id)
        duplicate_generic = session.scalar(
            select(func.count()).select_from(DetectedChangeCandidate)
            .where(DetectedChangeCandidate.contact_candidate_id.is_not(None))
        )
        assert result["candidates"] == 0
        assert duplicate_generic == 0
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 1
        assert session.scalar(select(func.count()).select_from(SourceOccurrence)) == occurrence_count


def test_semantic_api_categories_and_compact_ui_contract(semantic_db):
    url, factory, root, _engine = semantic_db
    with factory() as session:
        _agency, source_id = _source(session, "ui")
        run, observation, contacts, directory = _evidence_context(session, source_id, stored_count=3)
        _contact(session, contacts, observation, 1, locator="footer > p")
        _contact(session, contacts, observation, 2)
        _directory(session, directory, observation, 2)
        session.commit()
        run_id = run.id

    app = regression_app(url, project_root=root)
    with TestClient(app) as client:
        all_items = client.get(f"/api/runs/{run_id}/discoveries")
        directory_items = client.get(f"/api/runs/{run_id}/discoveries?category=DIRECTORY")
        site_items = client.get(f"/api/runs/{run_id}/discoveries?category=SITE_WIDE")
        too_large = client.get(f"/api/runs/{run_id}/discoveries?page_size=101")
        page = client.get("/runs")
    assert all_items.status_code == 200
    assert all_items.json()["summary"]["semantic_discovery_count"] == 2
    assert all_items.json()["page_size"] == 30
    assert directory_items.json()["total"] == 1
    assert site_items.json()["total"] == 1
    assert too_large.status_code == 422
    assert "유효 발견" in page.text
    assert 'data-discovery-category="DIRECTORY"' in page.text
    assert "실행 당시 기록" in page.text
