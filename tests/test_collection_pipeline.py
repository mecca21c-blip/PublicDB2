from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.collectors.html_contact_extractor import HTMLContactExtractor
from app.collectors.http_fetcher import HTTPFetcher, UnsafeRequestTarget
from app.collectors.staff_directory_extractor import StaffDirectoryExtractor
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    AgencyType, ChangeDetection, ChangeEvent, CollectionMethod, ContactHistory, ContactPoint, CrawlRun,
    ExtractedContactCandidate, ExtractedDirectoryRecord, ExtractionRun,
    Observation, OrgUnitType, Person, PersonAssignment, RunStatus,
    Source, SourceBinding, SourceOccurrence, StageStatus,
)
from app.services.agency_service import AgencyService
from app.services.artifact_validation import HTMLArtifactValidationError, load_verified_html_artifact
from app.services.collection_service import (
    CollectionBusyError, CollectionCoordinator, CollectionFinalizationError, CollectionService,
    SourceNotFoundError, UnsupportedCollectionMethod,
)
from app.services.contact_extraction_service import ContactExtractionService
from app.services.raw_artifact_store import RawArtifactStore
from app.services.semantic_discovery_service import SemanticDiscoveryProjector
from app.services.source_service import SourceService


HTML = """<html><body>
<p>대표전화 02-1234-5678 / 팩스 02-9876-5432 / team@example.go.kr</p>
<table><thead><tr><th>부서</th><th>업무</th><th>직위</th><th>담당자</th><th>전화</th><th>이메일</th></tr></thead>
<tbody><tr><td>정보과</td><td>공공데이터</td><td>주무관</td><td>홍길동</td><td>02-1111-2222</td><td>hong@example.go.kr</td></tr></tbody></table>
</body></html>""".encode()
EMPTY_HTML = b"<html><body><p>official page without target data</p></body></html>"
PUBLIC_DNS = lambda _host: {"93.184.216.34"}


@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    db_path = tmp_path / "db" / "test.sqlite3"
    db_path.parent.mkdir()
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    with factory() as session:
        yield session, tmp_path
    engine.dispose()


def source_with_bindings(session, *, url="https://example.org/staff", bindings=1):
    agencies = AgencyService(session)
    source_id = None
    binding_ids = []
    for index in range(bindings):
        agency, _ = agencies.create_agency(official_name=f"기관 {index}", agency_type=AgencyType.OTHER)
        agency_id = uuid.UUID(agency["id"])
        unit = agencies.create_org_unit(agency_id=agency_id, name=f"부서 {index}", unit_type=OrgUnitType.DEPARTMENT)
        projected, _ = SourceService(session).register_binding(
            url=url, agency_id=agency_id, org_unit_id=uuid.UUID(unit["id"])
        )
        source_id = uuid.UUID(projected["source_id"])
        binding_ids.append(uuid.UUID(projected["binding_id"]))
    return source_id, binding_ids


def service(session, root: Path, handler, **kwargs):
    fetcher = HTTPFetcher(
        transport=httpx.MockTransport(handler),
        resolver=PUBLIC_DNS,
        max_response_bytes=kwargs.pop("max_response_bytes", 10 * 1024 * 1024),
    )
    return CollectionService(
        session,
        project_root=root,
        raw_root=root / "data" / "raw",
        coordinator=kwargs.pop("coordinator", CollectionCoordinator()),
        fetcher=fetcher,
        artifact_store=kwargs.pop("artifact_store", None),
        **kwargs,
    )


def html_response(body=HTML):
    return lambda _request: httpx.Response(
        200, headers={"Content-Type": "text/html; charset=utf-8"}, content=body
    )


def test_registered_web_source_runs_complete_pipeline_and_preserves_raw(live_db):
    session, root = live_db
    source_id, _ = source_with_bindings(session)
    result = service(session, root, html_response()).collect(source_id)
    assert result.crawl_run.status is RunStatus.SUCCESS
    assert (result.crawl_run.connection_status, result.crawl_run.raw_status, result.crawl_run.extraction_status) == (
        StageStatus.SUCCESS, StageStatus.SUCCESS, StageStatus.SUCCESS
    )
    assert result.contact_candidates >= 3
    assert result.directory_records == 1
    summary = SemanticDiscoveryProjector(session).summary(result.crawl_run.id)
    assert result.crawl_run.records_observed == summary["semantic_discovery_count"]
    assert summary["raw_evidence_count"] == result.contact_candidates + result.directory_records
    assert result.observation and result.artifact
    assert not Path(result.artifact.relative_path).is_absolute()
    artifact = root / result.artifact.relative_path
    assert artifact.read_bytes() == HTML
    assert result.observation.artifact_sha256 == hashlib.sha256(HTML).hexdigest()
    assert result.observation.response_bytes == len(HTML)
    assert result.observation.final_url == "https://example.org/staff"
    assert session.scalar(select(func.count()).select_from(ExtractedContactCandidate)) == result.contact_candidates
    assert session.scalar(select(func.count()).select_from(ExtractedDirectoryRecord)) == 1


def test_unknown_and_unsupported_sources_do_not_fetch_or_create_run(live_db):
    session, root = live_db
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"unexpected")
    runner = service(session, root, handler)
    with pytest.raises(SourceNotFoundError):
        runner.collect(uuid.uuid4())
    source_id, _ = source_with_bindings(session)
    source = session.get(Source, source_id)
    source.collection_method = CollectionMethod.API
    session.commit()
    with pytest.raises(UnsupportedCollectionMethod):
        runner.collect(source_id)
    assert calls == 0
    assert session.scalar(select(func.count()).select_from(CrawlRun)) == 0


def test_non_2xx_and_oversize_fail_without_observation_or_final_raw(live_db):
    session, root = live_db
    source_id, _ = source_with_bindings(session)
    failed = service(session, root, lambda _r: httpx.Response(404, content=b"no")).collect(source_id)
    assert failed.crawl_run.status is RunStatus.FAILED
    assert failed.crawl_run.connection_status is StageStatus.FAILED
    assert failed.crawl_run.raw_status is StageStatus.SKIPPED
    assert failed.observation is None
    second_id, _ = source_with_bindings(session, url="https://example.org/large")
    large = service(
        session, root, lambda _r: httpx.Response(200, content=b"x" * 65),
        max_response_bytes=64,
    ).collect(second_id)
    assert large.crawl_run.status is RunStatus.FAILED
    assert session.scalar(select(func.count()).select_from(Observation)) == 0
    raw_root = root / "data" / "raw"
    assert not raw_root.exists() or not any(path.is_file() for path in raw_root.rglob("*"))


def test_artifact_validation_rejects_escape_and_hash_mismatch(live_db):
    session, root = live_db
    source_id, _ = source_with_bindings(session)
    result = service(session, root, html_response()).collect(source_id)
    observation = result.observation
    observation.artifact_path = "../escaped.html"
    session.commit()
    with pytest.raises(HTMLArtifactValidationError, match="escapes"):
        load_verified_html_artifact(session, observation.id, project_root=root, raw_root=root / "data" / "raw")
    observation.artifact_path = result.artifact.relative_path
    observation.artifact_sha256 = "0" * 64
    session.commit()
    with pytest.raises(HTMLArtifactValidationError, match="SHA-256"):
        load_verified_html_artifact(session, observation.id, project_root=root, raw_root=root / "data" / "raw")


def test_private_literal_and_private_redirect_are_blocked():
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)
    fetcher = HTTPFetcher(transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS)
    with pytest.raises(UnsafeRequestTarget):
        fetcher.fetch("http://127.0.0.1/private")
    assert calls == 0

    def redirect(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "http://10.0.0.1/internal"})
    with pytest.raises(UnsafeRequestTarget):
        HTTPFetcher(transport=httpx.MockTransport(redirect), resolver=PUBLIC_DNS).fetch("https://example.org/start")
    assert calls == 1


def test_public_redirect_is_bounded_and_final_metadata_is_captured():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=euc-kr"},
            content=b"<html></html>",
        )

    result = HTTPFetcher(
        transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS, max_redirects=2
    ).fetch("https://example.org/start")
    assert calls == ["https://example.org/start", "https://example.org/final"]
    assert result.final_url == "https://example.org/final"
    assert result.content_type == "text/html"
    assert result.declared_charset == "euc-kr"


def test_deterministic_extractors_and_placeholder_rejection():
    contacts = HTMLContactExtractor().extract(
        "<p>전화 02-1234-5678 팩스 02-9876-5432 a@Example.GO.KR</p><p>예시 02-0000-0000</p>"
    )
    kinds = {item.candidate_type.value for item in contacts}
    assert {"PHONE", "FAX", "EMAIL"} <= kinds
    assert all(item.normalized_value != "0200000000" for item in contacts)
    rows = StaffDirectoryExtractor().extract(HTML)
    assert len(rows) == 1
    assert rows[0].org_unit_text == "정보과"
    assert rows[0].person_name_text == "홍길동"


def test_successful_extractor_version_is_idempotent(live_db):
    session, root = live_db
    source_id, _ = source_with_bindings(session)
    result = service(session, root, html_response()).collect(source_id)
    before_runs = session.scalar(select(func.count()).select_from(ExtractionRun))
    before_candidates = session.scalar(select(func.count()).select_from(ExtractedContactCandidate))
    again = ContactExtractionService(
        session, project_root=root, raw_root=root / "data" / "raw"
    ).extract(result.observation.id)
    assert again.reused
    assert session.scalar(select(func.count()).select_from(ExtractionRun)) == before_runs
    assert session.scalar(select(func.count()).select_from(ExtractedContactCandidate)) == before_candidates


def test_zero_result_is_success_and_only_then_projects_empty(live_db):
    session, root = live_db
    source_id, bindings = source_with_bindings(session)
    result = service(session, root, html_response(EMPTY_HTML)).collect(source_id)
    assert result.crawl_run.status is RunStatus.SUCCESS
    assert result.crawl_run.extraction_status is StageStatus.SUCCESS
    assert result.crawl_run.records_observed == 0
    assert SourceService(session).get_binding(bindings[0])["status"] == "자료없음"


class BrokenArtifactStore:
    def store(self, **_kwargs):
        raise OSError("disk unavailable")


class FailingExtractor:
    name = "html_contact"
    version = "failure-test"
    def extract(self, *_args, **_kwargs):
        raise RuntimeError("contact extractor failed")


class FailingContactService(ContactExtractionService):
    def __init__(self, session, *, project_root, raw_root):
        super().__init__(
            session, extractor=FailingExtractor(),
            project_root=project_root, raw_root=raw_root,
        )


def test_fetch_raw_partial_and_unsupported_status_truth_table(live_db):
    session, root = live_db
    fetch_id, fetch_bindings = source_with_bindings(session)
    service(session, root, lambda _r: httpx.Response(500)).collect(fetch_id)
    assert SourceService(session).get_binding(fetch_bindings[0])["status"] == "오류"

    raw_id, raw_bindings = source_with_bindings(session, url="https://example.org/raw")
    raw_result = service(
        session, root, html_response(),
        artifact_store=BrokenArtifactStore(),
    ).collect(raw_id)
    assert raw_result.crawl_run.raw_status is StageStatus.FAILED
    assert raw_result.crawl_run.extraction_status is StageStatus.SKIPPED
    assert SourceService(session).get_binding(raw_bindings[0])["status"] == "오류"

    partial_id, partial_bindings = source_with_bindings(session, url="https://example.org/partial")
    partial = service(
        session, root, html_response(),
        contact_extractor_service=FailingContactService,
    ).collect(partial_id)
    assert partial.crawl_run.status is RunStatus.PARTIAL
    assert partial.crawl_run.extraction_status is StageStatus.FAILED
    assert partial.directory_records == 1
    assert session.scalar(
        select(func.count()).select_from(ExtractedDirectoryRecord)
        .where(ExtractedDirectoryRecord.observation_id == partial.observation.id)
    ) == 1
    projected = SourceService(session).get_binding(partial_bindings[0])
    assert projected["status"] == "오류"
    assert "추출 일부 실패" in projected["error"]

    other_id, other_bindings = source_with_bindings(session, url="https://example.org/file")
    unsupported = service(
        session, root,
        lambda _r: httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF"),
    ).collect(other_id)
    assert unsupported.crawl_run.status is RunStatus.PARTIAL
    assert unsupported.crawl_run.extraction_status is StageStatus.SKIPPED
    assert unsupported.observation is not None
    assert (root / unsupported.artifact.relative_path).exists()
    assert SourceService(session).get_binding(other_bindings[0])["status"] == "오류"


def test_canonical_source_shares_one_run_and_exclusion_overrides(live_db):
    session, root = live_db
    source_id, bindings = source_with_bindings(session, bindings=3)
    SourceService(session).exclude_binding(bindings[2], "불필요")
    service(session, root, html_response()).collect(source_id)
    assert session.scalar(select(func.count()).select_from(CrawlRun)) == 1
    assert SourceService(session).get_binding(bindings[0])["status"] == "정상"
    assert SourceService(session).get_binding(bindings[1])["status"] == "정상"
    assert SourceService(session).get_binding(bindings[2])["status"] == "제외"


def test_running_source_is_busy_and_no_duplicate_fetch(live_db):
    session, root = live_db
    source_id, bindings = source_with_bindings(session)
    session.add(CrawlRun(
        source_id=source_id, status=RunStatus.RUNNING,
        connection_status=StageStatus.PENDING, raw_status=StageStatus.PENDING,
        extraction_status=StageStatus.PENDING,
        started_at=__import__("app.models.common", fromlist=["utc_now"]).utc_now(),
    ))
    session.commit()
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)
    with pytest.raises(CollectionBusyError):
        service(session, root, handler).collect(source_id)
    assert calls == 0
    assert session.scalar(select(func.count()).select_from(CrawlRun)) == 1
    assert SourceService(session).get_binding(bindings[0])["status"] == "미확인"


def test_db_finalization_failure_removes_unreferenced_raw(live_db, monkeypatch):
    session, root = live_db
    source_id, _ = source_with_bindings(session)
    original_commit = session.commit
    commit_count = 0

    def fail_observation_commit():
        nonlocal commit_count
        commit_count += 1
        if commit_count == 3:
            from sqlalchemy.exc import SQLAlchemyError
            raise SQLAlchemyError("forced observation finalization failure")
        original_commit()

    monkeypatch.setattr(session, "commit", fail_observation_commit)
    with pytest.raises(CollectionFinalizationError):
        service(session, root, html_response()).collect(source_id)
    raw_root = root / "data" / "raw"
    assert not any(path.is_file() for path in raw_root.rglob("*"))
    run = session.scalar(select(CrawlRun))
    assert run.status is RunStatus.FAILED
    assert run.raw_status is StageStatus.FAILED


def test_discovery_does_not_write_confirmed_or_change_entities(live_db):
    session, root = live_db
    source_id, _ = source_with_bindings(session)
    service(session, root, html_response()).collect(source_id)
    for model in (
        ContactPoint, Person, PersonAssignment, SourceOccurrence,
        ChangeDetection, ChangeEvent, ContactHistory,
    ):
        assert session.scalar(select(func.count()).select_from(model)) == 0
