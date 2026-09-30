"""Explicit three-way collection dispatcher with immutable run evidence."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.collectors.http_fetcher import HTTPFetchError, HTTPFetcher, UnsafeRequestTarget
from app.models import (
    ApiAuthMode, ApiPaginationMode, ApiSourceKind, CollectionMethod, CrawlRun,
    ExtractionRun, ExtractionStatus, Observation, RunStatus, Source, StageStatus,
)
from app.models.common import utc_now
from app.services.api_credential_store import ApiCredentialStore, CredentialStoreError
from app.services.collection_recovery_service import reconcile_stale_collections, source_claim_key
from app.services.contact_extraction_service import ContactExtractionService
from app.services.directory_extraction_service import DirectoryExtractionService
from app.services.raw_artifact_store import RawArtifactStore, StoredArtifact
from app.services.operation_claim_service import OperationClaimService
from app.services.source_change_detection_service import SourceChangeDetectionService
from app.services.source_method_service import MethodConfigError, SourceMethodService
from app.services.structured_extraction_service import StructuredExtractionError, StructuredExtractionService


HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
COLLECTOR_VERSION = "05B-1"


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
            "run_id": str(run.id), "source_id": str(run.source_id),
            "status": run.status.value, "connection_status": run.connection_status.value,
            "raw_status": run.raw_status.value, "extraction_status": run.extraction_status.value,
            "records_observed": run.records_observed,
            "contact_candidates": self.contact_candidates,
            "directory_records": self.directory_records,
            "http_status": run.http_status, "error_summary": run.error_summary,
            "collection_method": (run.collection_method_snapshot or CollectionMethod.WEB_PAGE).value,
            "collection_kind": run.collection_kind_snapshot,
            "statistics": run.collection_statistics or {},
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }


class CollectionCoordinator:
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


def _canonical_page_url(url: str) -> str:
    value, _fragment = urldefrag(url)
    parts = urlsplit(value)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _without_secret(url: str, secret_name: str | None) -> str:
    if not secret_name:
        return url
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != secret_name]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class CollectionService:
    def __init__(
        self, session: Session, *, project_root: Path, raw_root: Path,
        coordinator: CollectionCoordinator, fetcher: HTTPFetcher | None = None,
        artifact_store: RawArtifactStore | None = None,
        contact_extractor_service: type[ContactExtractionService] = ContactExtractionService,
        directory_extractor_service: type[DirectoryExtractionService] = DirectoryExtractionService,
        sleeper=time.sleep, credential_store: ApiCredentialStore | None = None,
    ) -> None:
        self.session = session
        self.project_root = project_root.resolve()
        self.raw_root = raw_root.resolve()
        self.coordinator = coordinator
        self.fetcher = fetcher or HTTPFetcher()
        self.artifact_store = artifact_store or RawArtifactStore(project_root=self.project_root, raw_root=self.raw_root)
        self.contact_extractor_service = contact_extractor_service
        self.directory_extractor_service = directory_extractor_service
        self.sleeper = sleeper
        self.credential_store = credential_store or ApiCredentialStore(self.project_root / "config")
        self._claim_id: uuid.UUID | None = None
        self._claim_owner: str | None = None

    def collect(self, source_id: uuid.UUID) -> CollectionResult:
        reconcile_stale_collections(self.session)
        source = self.session.get(Source, source_id)
        if source is None:
            raise SourceNotFoundError("등록된 수집 소스를 찾을 수 없습니다.")
        if not source.active or not any(binding.active for binding in source.bindings):
            raise UnsupportedCollectionMethod("활성 수집 소스 연결이 없습니다.")
        if source.collection_method not in {CollectionMethod.WEB_PAGE, CollectionMethod.WEB_CRAWL, CollectionMethod.API}:
            raise UnsupportedCollectionMethod("지원하지 않는 수집 방식입니다.")
        if not self.coordinator.acquire(source_id):
            raise CollectionBusyError("이미 이 소스를 수집 중입니다.")
        try:
            self.session.rollback()
            with Session(bind=self.session.get_bind()) as claim_session:
                claimed = OperationClaimService(claim_session).acquire(
                    source_claim_key(source_id), 'COLLECT', commit=True
                )
                if not claimed.acquired:
                    raise CollectionBusyError("이미 이 소스를 수집 중입니다.")
                self._claim_id = claimed.claim.id
                self._claim_owner = claimed.claim.owner_token
            self.session.refresh(source)
            try:
                kind, snapshot = SourceMethodService.snapshot(source)
            except MethodConfigError as error:
                raise UnsupportedCollectionMethod(str(error)) from error
            if self.session.scalar(select(CrawlRun.id).where(
                CrawlRun.source_id == source_id, CrawlRun.status == RunStatus.RUNNING
            ).limit(1)) is not None:
                raise CollectionBusyError("이미 이 소스를 수집 중입니다.")
            started = utc_now()
            run = CrawlRun(
                source_id=source_id, status=RunStatus.RUNNING,
                connection_status=StageStatus.PENDING, raw_status=StageStatus.PENDING,
                extraction_status=StageStatus.PENDING, started_at=started, heartbeat_at=started,
                records_observed=0, collection_method_snapshot=source.collection_method,
                collection_kind_snapshot=kind, collection_config_snapshot=snapshot,
                collection_statistics={}, collector_version=COLLECTOR_VERSION,
            )
            self.session.add(run)
            try:
                self.session.commit()
            except IntegrityError as error:
                self.session.rollback()
                raise CollectionBusyError("이미 이 소스를 수집 중입니다.") from error
            if source.collection_method is CollectionMethod.WEB_PAGE:
                return self._collect_scrape(source, run)
            if source.collection_method is CollectionMethod.WEB_CRAWL:
                return self._collect_crawl(source, run)
            return self._collect_api(source, run)
        finally:
            if self._claim_id is not None and self._claim_owner is not None:
                with Session(bind=self.session.get_bind()) as claim_session:
                    OperationClaimService(claim_session).release(self._claim_id, self._claim_owner)
                self._claim_id = None
                self._claim_owner = None
            self.coordinator.release(source_id)

    def _collect_scrape(self, source: Source, run: CrawlRun) -> CollectionResult:
        stats = {"pages_attempted": 1, "pages_succeeded": 0}
        try:
            fetched = self.fetcher.fetch(source.url)
        except HTTPFetchError as error:
            return CollectionResult(self._finish_failure(run.id, error, StageStatus.FAILED, StageStatus.SKIPPED, StageStatus.SKIPPED, error.status_code, stats), None, None)
        self._connection_success(run.id, fetched.status_code)
        try:
            observation, artifact = self._store_observation(run.id, source.id, source.url, fetched, None)
        except OSError as error:
            return CollectionResult(self._finish_failure(run.id, error, StageStatus.SUCCESS, StageStatus.FAILED, StageStatus.SKIPPED, fetched.status_code, stats), None, None)
        stats["pages_succeeded"] = 1
        if fetched.content_type not in HTML_CONTENT_TYPES:
            return CollectionResult(self._finalize(run.id, RunStatus.PARTIAL, StageStatus.SKIPPED, 0, "추출 미지원 형식", stats), observation, artifact)
        config = source.scrape_config
        contacts, directories, success, failures, directory_runs = self._extract_html(
            observation, config.extract_contacts if config else True,
            config.extract_directory if config else True,
        )
        if success and not failures:
            final = self._finalize(run.id, RunStatus.SUCCESS, StageStatus.SUCCESS, contacts + directories, None, stats)
            for extraction_id in directory_runs:
                self._detect_changes_best_effort(extraction_id)
        elif success:
            final = self._finalize(run.id, RunStatus.PARTIAL, StageStatus.FAILED, contacts + directories, "추출 일부 실패: " + ", ".join(failures), stats)
        else:
            final = self._finalize(run.id, RunStatus.FAILED, StageStatus.FAILED, 0, "추출 실패", stats)
        return CollectionResult(final, observation, artifact, contacts, directories)

    def _collect_crawl(self, source: Source, run: CrawlRun) -> CollectionResult:
        config = source.crawl_config
        if config is None:
            return CollectionResult(self._finish_failure(run.id, MethodConfigError("크롤링 설정이 없습니다."), StageStatus.SKIPPED, StageStatus.SKIPPED, StageStatus.SKIPPED, None, {}), None, None)
        seed = _canonical_page_url(source.url)
        queue = deque([(seed, 0)])
        seen = {seed}
        robots: dict[str, RobotFileParser | None] = {}
        attempts = fetches = successes = failures = contact_count = directory_count = 0
        max_depth_reached = 0
        last_observation = None
        last_artifact = None
        extraction_failed = False
        directory_runs: list[uuid.UUID] = []
        while queue and attempts < config.max_pages:
            target, depth = queue.popleft()
            max_depth_reached = max(max_depth_reached, depth)
            if not self._robots_allowed(target, robots):
                failures += 1
                continue
            if attempts:
                self.sleeper(config.request_delay_ms / 1000)
            attempts += 1
            try:
                fetched = self.fetcher.fetch(target, target_validator=lambda value: self._validate_scope(source.url, value, config.scope.value, config.allowed_path))
                self._validate_scope(source.url, fetched.final_url, config.scope.value, config.allowed_path)
            except HTTPFetchError:
                failures += 1
                continue
            fetches += 1
            if successes == 0:
                self._connection_success(run.id, fetched.status_code)
            try:
                observation, artifact = self._store_observation(run.id, source.id, target, fetched, successes + 1)
            except OSError:
                failures += 1
                continue
            successes += 1
            last_observation, last_artifact = observation, artifact
            if fetched.content_type in HTML_CONTENT_TYPES:
                contacts, directories, extracted, extract_failures, runs = self._extract_html(
                    observation, config.extract_contacts, config.extract_directory
                )
                contact_count += contacts
                directory_count += directories
                directory_runs.extend(runs)
                extraction_failed = extraction_failed or bool(extract_failures) or not extracted
                if depth < config.max_depth:
                    for link in self._links(fetched.content, fetched.final_url, fetched.declared_charset):
                        canonical = _canonical_page_url(link)
                        if canonical in seen:
                            continue
                        try:
                            self._validate_scope(source.url, canonical, config.scope.value, config.allowed_path)
                        except UnsafeRequestTarget:
                            continue
                        seen.add(canonical)
                        queue.append((canonical, depth + 1))
        stats = {
            "pages_discovered": len(seen), "pages_attempted": attempts,
            "pages_succeeded": successes, "pages_failed": failures,
            "max_depth_reached": max_depth_reached,
        }
        total = contact_count + directory_count
        if successes == 0:
            final = self._finish_failure(
                run.id, RuntimeError("수집에 성공한 eligible 페이지가 없습니다."),
                StageStatus.SUCCESS if fetches else StageStatus.FAILED,
                StageStatus.FAILED if fetches else StageStatus.SKIPPED,
                StageStatus.SKIPPED, None, stats,
            )
        elif failures or extraction_failed:
            final = self._finalize(run.id, RunStatus.PARTIAL, StageStatus.FAILED, total, "일부 페이지의 접근 또는 추출에 실패했습니다.", stats)
        else:
            final = self._finalize(run.id, RunStatus.SUCCESS, StageStatus.SUCCESS, total, None, stats)
            for extraction_id in directory_runs:
                self._detect_changes_best_effort(extraction_id)
        return CollectionResult(final, last_observation, last_artifact, contact_count, directory_count)

    def _collect_api(self, source: Source, run: CrawlRun) -> CollectionResult:
        config = source.api_config
        if config is None:
            return CollectionResult(self._finish_failure(run.id, MethodConfigError("API/RSS 설정이 없습니다."), StageStatus.SKIPPED, StageStatus.SKIPPED, StageStatus.SKIPPED, None, {}), None, None)
        params = dict(config.static_params or {})
        headers: dict[str, str] = {}
        secret_name = None
        if config.credential_expires_on and config.credential_expires_on < date.today():
            return CollectionResult(self._finish_failure(run.id, RuntimeError("API 자격증명이 만료되었습니다."), StageStatus.SKIPPED, StageStatus.SKIPPED, StageStatus.SKIPPED, None, {}), None, None)
        if config.auth_mode is not ApiAuthMode.NONE:
            try:
                secret = self.credential_store.resolve(config.credential_ref)
            except CredentialStoreError as error:
                return CollectionResult(self._finish_failure(run.id, error, StageStatus.SKIPPED, StageStatus.SKIPPED, StageStatus.SKIPPED, None, {}), None, None)
            secret_name = config.credential_name
            if config.auth_mode is ApiAuthMode.QUERY_API_KEY:
                params[config.credential_name] = secret
            else:
                headers[config.credential_name] = secret
        requests_attempted = requests_succeeded = raw_records = mapped_records = 0
        contact_count = directory_count = 0
        last_observation = None
        last_artifact = None
        errors: list[str] = []
        pages = config.max_pages if config.pagination_mode is ApiPaginationMode.PAGE_NUMBER else 1
        for offset in range(pages):
            request_params = dict(params)
            if config.pagination_mode is ApiPaginationMode.PAGE_NUMBER:
                request_params[config.page_parameter] = config.start_page + offset
                if config.page_size_parameter:
                    request_params[config.page_size_parameter] = config.page_size
            if requests_attempted:
                self.sleeper(config.request_delay_ms / 1000)
            requests_attempted += 1
            try:
                fetched = self.fetcher.fetch(source.url, params=request_params, headers=headers)
            except HTTPFetchError as error:
                errors.append(_summary(error))
                continue
            if requests_succeeded == 0:
                self._connection_success(run.id, fetched.status_code)
            requests_succeeded += 1
            actual_url = _without_secret(fetched.final_url, secret_name if config.auth_mode is ApiAuthMode.QUERY_API_KEY else None)
            try:
                observation, artifact = self._store_observation(
                    run.id, source.id, source.url, fetched, requests_succeeded,
                    final_url=actual_url,
                )
            except OSError as error:
                errors.append(_summary(error))
                continue
            last_observation, last_artifact = observation, artifact
            try:
                if config.kind in {ApiSourceKind.RSS, ApiSourceKind.ATOM}:
                    result = StructuredExtractionService(self.session).extract_feed(observation.id, fetched.content, config.kind)
                else:
                    result = StructuredExtractionService(self.session).extract_open_api(
                        observation.id, fetched.content, config.response_format,
                        config.record_path, config.field_mapping or {}, config.discovery_only,
                        fetched.declared_charset,
                    )
                    if result.mapped_records:
                        directory_count += result.mapped_records
                        self._detect_changes_best_effort(result.extraction_run.id)
                raw_records += result.raw_records
                mapped_records += result.mapped_records
                if result.raw_records == 0 and config.pagination_mode is ApiPaginationMode.PAGE_NUMBER:
                    break
            except StructuredExtractionError as error:
                errors.append(_summary(error))
        stats = {
            "requests_attempted": requests_attempted, "requests_succeeded": requests_succeeded,
            "raw_records": raw_records, "mapped_records": mapped_records,
        }
        if config.kind in {ApiSourceKind.RSS, ApiSourceKind.ATOM}:
            stats["feed_items"] = raw_records
        meaningful = raw_records if config.discovery_only or config.kind in {ApiSourceKind.RSS, ApiSourceKind.ATOM} else mapped_records
        if requests_succeeded == 0 or (errors and mapped_records == 0 and raw_records == 0):
            final = self._finish_failure(
                run.id, RuntimeError(errors[0] if errors else "API 요청 실패"),
                StageStatus.FAILED if requests_succeeded == 0 else StageStatus.SUCCESS,
                StageStatus.SKIPPED if requests_succeeded == 0 else StageStatus.SUCCESS,
                StageStatus.SKIPPED if requests_succeeded == 0 else StageStatus.FAILED,
                None, stats,
            )
        elif errors:
            final = self._finalize(run.id, RunStatus.PARTIAL, StageStatus.FAILED, meaningful, "일부 API 요청 또는 추출에 실패했습니다.", stats)
        else:
            final = self._finalize(run.id, RunStatus.SUCCESS, StageStatus.SUCCESS, meaningful, None, stats)
        return CollectionResult(final, last_observation, last_artifact, contact_count, directory_count)

    def _connection_success(self, run_id: uuid.UUID, status_code: int) -> None:
        run = self._get_run(run_id)
        run.connection_status = StageStatus.SUCCESS
        run.http_status = status_code
        run.heartbeat_at = utc_now()
        self.session.commit()
        self._heartbeat()

    def _store_observation(
        self, run_id: uuid.UUID, source_id: uuid.UUID, page_url: str, fetched,
        sequence: int | None, *, final_url: str | None = None,
    ) -> tuple[Observation, StoredArtifact]:
        observed_at = utc_now()
        store_values = {
            "source_id": source_id, "crawl_run_id": run_id, "observed_at": observed_at,
            "content": fetched.content, "content_type": fetched.content_type,
        }
        if sequence is not None:
            store_values["sequence"] = sequence
        artifact = self.artifact_store.store(**store_values)
        observation = Observation(
            crawl_run_id=run_id, source_id=source_id, observed_at=observed_at,
            page_url=page_url, final_url=final_url or fetched.final_url,
            artifact_path=artifact.relative_path, artifact_sha256=artifact.sha256,
            content_type=fetched.content_type or None, declared_charset=fetched.declared_charset,
            response_bytes=fetched.response_bytes, structured_payload=None,
        )
        try:
            run = self._get_run(run_id)
            run.raw_status = StageStatus.SUCCESS
            run.heartbeat_at = utc_now()
            self.session.add(observation)
            self.session.commit()
            self._heartbeat()
        except SQLAlchemyError as error:
            self.session.rollback()
            self.artifact_store.remove(artifact)
            self._best_effort_failure(run_id, error, connection=StageStatus.SUCCESS, raw=StageStatus.FAILED, extraction=StageStatus.SKIPPED, http_status=fetched.status_code, statistics={})
            raise CollectionFinalizationError("RAW evidence를 DB에 확정하지 못했습니다.") from error
        return observation, artifact

    def _extract_html(self, observation: Observation, contacts_enabled: bool, directory_enabled: bool) -> tuple[int, int, bool, list[str], list[uuid.UUID]]:
        contacts = directories = 0
        successes = 0
        failures: list[str] = []
        directory_runs: list[uuid.UUID] = []
        if contacts_enabled:
            result = self.contact_extractor_service(self.session, project_root=self.project_root, raw_root=self.raw_root).extract(observation.id)
            if result.extraction_run.status is ExtractionStatus.SUCCESS:
                contacts = len(result.candidates)
                successes += 1
            else:
                failures.append(result.extraction_run.extractor_name)
        if directory_enabled:
            result = self.directory_extractor_service(self.session, project_root=self.project_root, raw_root=self.raw_root).extract(observation.id)
            if result.extraction_run.status is ExtractionStatus.SUCCESS:
                directories = len(result.records)
                successes += 1
                directory_runs.append(result.extraction_run.id)
            else:
                failures.append(result.extraction_run.extractor_name)
        return contacts, directories, successes > 0, failures, directory_runs

    @staticmethod
    def _validate_scope(seed_url: str, target_url: str, scope: str, allowed_path: str) -> None:
        seed = urlsplit(seed_url)
        target = urlsplit(target_url)
        if target.scheme not in {"http", "https"} or target.hostname != seed.hostname or target.port != seed.port:
            raise UnsafeRequestTarget("크롤링 범위를 벗어난 호스트입니다.", final_url=target_url)
        if scope == "PATH_PREFIX":
            prefix = allowed_path.rstrip("/") or "/"
            path = target.path or "/"
            if path != prefix and not path.startswith(prefix.rstrip("/") + "/"):
                raise UnsafeRequestTarget("허용 경로를 벗어난 URL입니다.", final_url=target_url)

    def _robots_allowed(self, target: str, cache: dict[str, RobotFileParser | None]) -> bool:
        parts = urlsplit(target)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in cache:
            parser = RobotFileParser()
            parser.set_url(origin + "/robots.txt")
            try:
                fetched = self.fetcher.fetch(
                    parser.url,
                    target_validator=lambda value: self._validate_scope(
                        origin + "/", value, "SAME_DOMAIN", "/"
                    ),
                )
                parser.parse(fetched.content.decode(fetched.declared_charset or "utf-8", errors="replace").splitlines())
                cache[origin] = parser
            except HTTPFetchError as error:
                if error.status_code in {404, 410}:
                    cache[origin] = None
                else:
                    parser.parse(["User-agent: *", "Disallow: /"])
                    cache[origin] = parser
        parser = cache[origin]
        return True if parser is None else parser.can_fetch(self.fetcher.user_agent, target)

    @staticmethod
    def _links(content: bytes, base_url: str, charset: str | None) -> tuple[str, ...]:
        try:
            document = content.decode(charset or "utf-8", errors="replace")
        except LookupError:
            document = content.decode("utf-8", errors="replace")
        links: list[str] = []
        for anchor in BeautifulSoup(document, "html.parser").find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            if not href or href.split(":", 1)[0].lower() in {"mailto", "tel", "javascript", "data", "file"}:
                continue
            absolute = urljoin(base_url, href)
            if urlsplit(absolute).scheme in {"http", "https"}:
                links.append(absolute)
        return tuple(links)

    def _detect_changes_best_effort(self, extraction_run_id: uuid.UUID) -> None:
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

    def _finalize(self, run_id: uuid.UUID, status: RunStatus, extraction: StageStatus, records: int, error_summary: str | None, statistics: dict) -> CrawlRun:
        run = self._get_run(run_id)
        source = self.session.get(Source, run.source_id)
        finished = utc_now()
        run.status = status
        run.extraction_status = extraction
        run.records_observed = records
        run.error_summary = error_summary
        run.collection_statistics = statistics
        run.finished_at = finished
        run.heartbeat_at = finished
        if source is not None:
            source.last_checked_at = finished
            if status is RunStatus.SUCCESS:
                source.last_success_at = finished
        self.session.commit()
        return run

    def _finish_failure(self, run_id: uuid.UUID, error: Exception, connection: StageStatus, raw: StageStatus, extraction: StageStatus, http_status: int | None, statistics: dict) -> CrawlRun:
        self.session.rollback()
        run = self._get_run(run_id)
        source = self.session.get(Source, run.source_id)
        finished = utc_now()
        run.status = RunStatus.FAILED
        run.connection_status = connection
        run.raw_status = raw
        run.extraction_status = extraction
        if http_status is not None or run.http_status is None:
            run.http_status = http_status
        run.records_observed = 0
        run.error_summary = _summary(error)
        run.collection_statistics = statistics
        run.finished_at = finished
        run.heartbeat_at = finished
        if source is not None:
            source.last_checked_at = finished
        self.session.commit()
        return run

    def _best_effort_failure(self, run_id: uuid.UUID, error: Exception, **stages: object) -> None:
        try:
            self._finish_failure(run_id, error, stages["connection"], stages["raw"], stages["extraction"], stages.get("http_status"), stages.get("statistics") or {})
        except (SQLAlchemyError, CollectionFinalizationError):
            self.session.rollback()

    def _heartbeat(self) -> None:
        if self._claim_id is not None and self._claim_owner is not None:
            with Session(bind=self.session.get_bind()) as claim_session:
                OperationClaimService(claim_session).heartbeat(self._claim_id, self._claim_owner)
