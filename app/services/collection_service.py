"""One explicit action: safe fetch, RAW evidence, deterministic extraction, final run."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.collectors.http_fetcher import HTTPFetchError, HTTPFetcher
from app.models import (
    CollectionMethod,
    CrawlRun,
    ExtractionRun,
    ExtractionStatus,
    Observation,
    RunStatus,
    Source,
    StageStatus,
)
from app.models.common import utc_now
from app.services.contact_extraction_service import ContactExtractionService
from app.services.directory_extraction_service import DirectoryExtractionService
from app.services.raw_artifact_store import RawArtifactStore, StoredArtifact
from app.services.source_change_detection_service import SourceChangeDetectionService


HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}


class SourceNotFoundError(LookupError):
    pass


class UnsupportedCollectionMethod(ValueError):
    pass


class CollectionBusyError(RuntimeError):
    pass


class CollectionFinalizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CollectionResult:
    crawl_run: CrawlRun
    observation: Observation | None
    artifact: StoredArtifact | None
    contact_candidates: int = 0
    directory_records: int = 0

    def projection(self) -> dict[str, object]:
        run = self.crawl_run
        return {
            "run_id": str(run.id),
            "source_id": str(run.source_id),
            "status": run.status.value,
            "connection_status": run.connection_status.value,
            "raw_status": run.raw_status.value,
            "extraction_status": run.extraction_status.value,
            "records_observed": run.records_observed,
            "contact_candidates": self.contact_candidates,
            "directory_records": self.directory_records,
            "http_status": run.http_status,
            "error_summary": run.error_summary,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }


class CollectionCoordinator:
    """Process-local guard for obvious duplicate submissions."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._active: set[uuid.UUID] = set()

    def acquire(self, source_id: uuid.UUID) -> bool:
        with self._mutex:
            if source_id in self._active:
                return False
            self._active.add(source_id)
            return True

    def release(self, source_id: uuid.UUID) -> None:
        with self._mutex:
            self._active.discard(source_id)


def _summary(error: Exception) -> str:
    return (str(error).strip() or error.__class__.__name__)[:1000]


class CollectionService:
    def __init__(
        self,
        session: Session,
        *,
        project_root: Path,
        raw_root: Path,
        coordinator: CollectionCoordinator,
        fetcher: HTTPFetcher | None = None,
        artifact_store: RawArtifactStore | None = None,
        contact_extractor_service: type[ContactExtractionService] = ContactExtractionService,
        directory_extractor_service: type[DirectoryExtractionService] = DirectoryExtractionService,
    ) -> None:
        self.session = session
        self.project_root = project_root.resolve()
        self.raw_root = raw_root.resolve()
        self.coordinator = coordinator
        self.fetcher = fetcher or HTTPFetcher()
        self.artifact_store = artifact_store or RawArtifactStore(project_root=self.project_root, raw_root=self.raw_root)
        self.contact_extractor_service = contact_extractor_service
        self.directory_extractor_service = directory_extractor_service

    def collect(self, source_id: uuid.UUID) -> CollectionResult:
        source = self.session.get(Source, source_id)
        if source is None:
            raise SourceNotFoundError("등록된 수집 소스를 찾을 수 없습니다.")
        if not source.active or not any(binding.active for binding in source.bindings):
            raise UnsupportedCollectionMethod("활성 수집 소스 연결이 없습니다.")
        if source.collection_method is not CollectionMethod.WEB_PAGE:
            raise UnsupportedCollectionMethod("이 수집 방식은 04A에서 지원하지 않습니다.")
        if not self.coordinator.acquire(source_id):
            raise CollectionBusyError("이미 이 소스를 수집 중입니다.")
        try:
            active = self.session.scalar(
                select(CrawlRun.id).where(CrawlRun.source_id == source_id, CrawlRun.status == RunStatus.RUNNING).limit(1)
            )
            if active is not None:
                raise CollectionBusyError("이미 이 소스를 수집 중입니다.")
            run = CrawlRun(
                source_id=source_id,
                status=RunStatus.RUNNING,
                connection_status=StageStatus.PENDING,
                raw_status=StageStatus.PENDING,
                extraction_status=StageStatus.PENDING,
                started_at=utc_now(),
                records_observed=0,
            )
            self.session.add(run)
            self.session.commit()
            run_id = run.id

            try:
                fetched = self.fetcher.fetch(source.url)
            except HTTPFetchError as error:
                run = self._finish_failure(
                    run_id, error, connection=StageStatus.FAILED,
                    raw=StageStatus.SKIPPED, extraction=StageStatus.SKIPPED,
                    http_status=error.status_code,
                )
                return CollectionResult(run, None, None)

            run = self._get_run(run_id)
            run.connection_status = StageStatus.SUCCESS
            run.http_status = fetched.status_code
            self.session.commit()

            observed_at = utc_now()
            try:
                artifact = self.artifact_store.store(
                    source_id=source_id,
                    crawl_run_id=run_id,
                    observed_at=observed_at,
                    content=fetched.content,
                    content_type=fetched.content_type,
                )
            except OSError as error:
                run = self._finish_failure(
                    run_id, error, connection=StageStatus.SUCCESS,
                    raw=StageStatus.FAILED, extraction=StageStatus.SKIPPED,
                    http_status=fetched.status_code,
                )
                return CollectionResult(run, None, None)

            observation = Observation(
                crawl_run_id=run_id,
                source_id=source_id,
                observed_at=observed_at,
                page_url=fetched.final_url,
                final_url=fetched.final_url,
                artifact_path=artifact.relative_path,
                artifact_sha256=artifact.sha256,
                content_type=fetched.content_type or None,
                declared_charset=fetched.declared_charset,
                response_bytes=fetched.response_bytes,
                structured_payload=None,
            )
            try:
                run = self._get_run(run_id)
                run.raw_status = StageStatus.SUCCESS
                self.session.add(observation)
                self.session.commit()
            except SQLAlchemyError as error:
                self.session.rollback()
                self.artifact_store.remove(artifact)
                self._best_effort_failure(
                    run_id, error, connection=StageStatus.SUCCESS,
                    raw=StageStatus.FAILED, extraction=StageStatus.SKIPPED,
                    http_status=fetched.status_code,
                )
                raise CollectionFinalizationError("RAW evidence를 DB에 확정하지 못했습니다.") from error

            if fetched.content_type not in HTML_CONTENT_TYPES:
                run = self._finalize(
                    run_id, RunStatus.PARTIAL, StageStatus.SKIPPED, 0,
                    "추출 미지원 형식",
                )
                return CollectionResult(run, observation, artifact)

            contact_result = self.contact_extractor_service(
                self.session, project_root=self.project_root, raw_root=self.raw_root
            ).extract(observation.id)
            directory_result = self.directory_extractor_service(
                self.session, project_root=self.project_root, raw_root=self.raw_root
            ).extract(observation.id)
            contact_count = len(contact_result.candidates)
            directory_count = len(directory_result.records)
            total = contact_count + directory_count
            statuses = (contact_result.extraction_run.status, directory_result.extraction_run.status)
            if all(status is ExtractionStatus.SUCCESS for status in statuses):
                run = self._finalize(run_id, RunStatus.SUCCESS, StageStatus.SUCCESS, total, None)
                self._detect_changes_best_effort(directory_result.extraction_run.id)
            elif any(status is ExtractionStatus.SUCCESS for status in statuses):
                failed = [
                    result.extraction_run.extractor_name
                    for result in (contact_result, directory_result)
                    if result.extraction_run.status is ExtractionStatus.FAILED
                ]
                run = self._finalize(
                    run_id, RunStatus.PARTIAL, StageStatus.FAILED, total,
                    "추출 일부 실패: " + ", ".join(failed),
                )
            else:
                run = self._finalize(run_id, RunStatus.FAILED, StageStatus.FAILED, 0, "추출 실패")
            return CollectionResult(run, observation, artifact, contact_count, directory_count)
        finally:
            self.coordinator.release(source_id)

    def _detect_changes_best_effort(self, extraction_run_id: uuid.UUID) -> None:
        """Review generation is independent from an already successful CrawlRun."""
        try:
            extraction = self.session.get(ExtractionRun, extraction_run_id)
            if extraction is None:
                return
            detector = SourceChangeDetectionService(self.session)
            agency_id = detector.planner.resolve_agency(extraction.observation.source_id)
            if detector.baseline_exists(extraction.observation.source_id, agency_id):
                detector.generate(extraction_run_id, agency_id)
        except Exception:
            self.session.rollback()

    def _get_run(self, run_id: uuid.UUID) -> CrawlRun:
        run = self.session.get(CrawlRun, run_id)
        if run is None:
            raise CollectionFinalizationError("수집 실행이 사라졌습니다.")
        return run

    def _finalize(
        self, run_id: uuid.UUID, status: RunStatus, extraction: StageStatus,
        records: int, error_summary: str | None,
    ) -> CrawlRun:
        run = self._get_run(run_id)
        source = self.session.get(Source, run.source_id)
        finished = utc_now()
        run.status = status
        run.extraction_status = extraction
        run.records_observed = records
        run.error_summary = error_summary
        run.finished_at = finished
        if source is not None:
            source.last_checked_at = finished
            if status is RunStatus.SUCCESS:
                source.last_success_at = finished
        self.session.commit()
        return run

    def _finish_failure(
        self, run_id: uuid.UUID, error: Exception, *, connection: StageStatus,
        raw: StageStatus, extraction: StageStatus, http_status: int | None,
    ) -> CrawlRun:
        self.session.rollback()
        run = self._get_run(run_id)
        source = self.session.get(Source, run.source_id)
        finished = utc_now()
        run.status = RunStatus.FAILED
        run.connection_status = connection
        run.raw_status = raw
        run.extraction_status = extraction
        run.http_status = http_status
        run.records_observed = 0
        run.error_summary = _summary(error)
        run.finished_at = finished
        if source is not None:
            source.last_checked_at = finished
        self.session.commit()
        return run

    def _best_effort_failure(self, run_id: uuid.UUID, error: Exception, **stages: object) -> None:
        try:
            self._finish_failure(run_id, error, **stages)
        except (SQLAlchemyError, CollectionFinalizationError):
            self.session.rollback()
