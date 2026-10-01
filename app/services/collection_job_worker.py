"""Sequential, item-isolated execution for persistent collection jobs."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    CollectionJob, CollectionJobItem, CollectionJobItemStatus, CollectionJobStatus,
    RunStatus,
)
from app.models.common import utc_now
from app.services.collection_service import CollectionBusyError, CollectionFinalizationError, CollectionService


logger = logging.getLogger("publicdb2")
_PROCESS_WORKER_LOCK = threading.Lock()


class WorkerResult(str, Enum):
    IDLE = "IDLE"
    ITEM_COMPLETE = "ITEM_COMPLETE"
    RETRY_LATER = "RETRY_LATER"
    SYSTEM_FAILURE = "SYSTEM_FAILURE"


class WorkerOwnershipError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimedItem:
    item_id: uuid.UUID
    source_id: uuid.UUID


def _error_summary(error: Exception) -> str:
    return (str(error).strip() or error.__class__.__name__)[:1000]


def _refresh_job(job: CollectionJob) -> None:
    statuses = [item.status for item in job.items]
    job.succeeded_items = statuses.count(CollectionJobItemStatus.SUCCESS)
    job.failed_items = statuses.count(CollectionJobItemStatus.FAILED)
    job.skipped_items = statuses.count(CollectionJobItemStatus.SKIPPED)
    if any(value in (CollectionJobItemStatus.PENDING, CollectionJobItemStatus.RUNNING) for value in statuses):
        return
    job.finished_at = utc_now()
    job.status = (
        CollectionJobStatus.COMPLETED_WITH_ERRORS
        if job.failed_items or job.skipped_items
        else CollectionJobStatus.COMPLETED
    )


def recover_interrupted_jobs(session: Session) -> int:
    """Fail only interrupted items; already-terminal and pending items are preserved."""
    now = utc_now()
    items = list(session.scalars(
        select(CollectionJobItem).where(CollectionJobItem.status == CollectionJobItemStatus.RUNNING)
    ))
    touched_jobs: set[uuid.UUID] = set()
    for item in items:
        item.status = CollectionJobItemStatus.FAILED
        item.finished_at = now
        item.error_code = "INTERRUPTED_BY_RESTART"
        item.error_summary = "Collection item was interrupted by application restart."
        touched_jobs.add(item.job_id)
    for job_id in touched_jobs:
        job = session.get(CollectionJob, job_id)
        if job is not None:
            job.error_summary = "One or more items were interrupted by application restart."
            _refresh_job(job)
    if items:
        session.commit()
    return len(items)


class CollectionJobWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        collection_service_factory: Callable[[Session], CollectionService],
    ) -> None:
        self.session_factory = session_factory
        self.collection_service_factory = collection_service_factory

    def run_one(self) -> WorkerResult:
        if not _PROCESS_WORKER_LOCK.acquire(blocking=False):
            return WorkerResult.IDLE
        try:
            claimed = self._claim_next()
            if claimed is None:
                return WorkerResult.IDLE
            try:
                with self.session_factory() as collection_session:
                    result = self.collection_service_factory(collection_session).collect(claimed.source_id)
                    crawl_run_id = result.crawl_run.id
                    run_status = result.crawl_run.status
                    run_error = result.crawl_run.error_summary
                if run_status is RunStatus.SUCCESS:
                    self._finish_item(
                        claimed.item_id, CollectionJobItemStatus.SUCCESS,
                        crawl_run_id=crawl_run_id,
                    )
                else:
                    self._finish_item(
                        claimed.item_id, CollectionJobItemStatus.FAILED,
                        crawl_run_id=crawl_run_id,
                        error_code=f"COLLECTION_{run_status.value}",
                        error_summary=run_error or "Collection did not complete successfully.",
                    )
                return WorkerResult.ITEM_COMPLETE
            except CollectionBusyError as error:
                self._release_busy_item(claimed.item_id, error)
                return WorkerResult.RETRY_LATER
            except (SQLAlchemyError, CollectionFinalizationError, WorkerOwnershipError) as error:
                self._note_system_failure(claimed.item_id, error)
                logger.exception("collection job worker stopped after systemic failure")
                return WorkerResult.SYSTEM_FAILURE
            except Exception as error:
                try:
                    self._finish_item(
                        claimed.item_id, CollectionJobItemStatus.FAILED,
                        error_code=error.__class__.__name__, error_summary=_error_summary(error),
                    )
                except SQLAlchemyError:
                    logger.exception("collection job failure could not be recorded")
                    return WorkerResult.SYSTEM_FAILURE
                logger.warning("collection source failed and queue will continue: %s", error.__class__.__name__)
                return WorkerResult.ITEM_COMPLETE
        except SQLAlchemyError:
            logger.exception("collection job worker ownership or queue operation failed")
            return WorkerResult.SYSTEM_FAILURE
        finally:
            _PROCESS_WORKER_LOCK.release()

    def drain(self, *, max_items: int | None = None) -> list[WorkerResult]:
        results: list[WorkerResult] = []
        while max_items is None or len(results) < max_items:
            result = self.run_one()
            if result is WorkerResult.IDLE:
                break
            results.append(result)
            if result in (WorkerResult.SYSTEM_FAILURE, WorkerResult.RETRY_LATER):
                break
        return results

    def _claim_next(self) -> ClaimedItem | None:
        with self.session_factory() as session:
            item = session.scalar(
                select(CollectionJobItem)
                .join(CollectionJob, CollectionJob.id == CollectionJobItem.job_id)
                .where(
                    CollectionJobItem.status == CollectionJobItemStatus.PENDING,
                    CollectionJob.status.in_((CollectionJobStatus.PENDING, CollectionJobStatus.RUNNING)),
                )
                .order_by(
                    CollectionJob.priority.desc(), CollectionJobItem.attempt_count,
                    CollectionJob.created_at, CollectionJobItem.sequence,
                )
                .limit(1)
            )
            if item is None:
                return None
            now = utc_now()
            job = item.job
            item.status = CollectionJobItemStatus.RUNNING
            item.started_at = now
            item.attempt_count += 1
            if job.status is CollectionJobStatus.PENDING:
                job.status = CollectionJobStatus.RUNNING
                job.started_at = now
            session.commit()
            return ClaimedItem(item.id, item.source_id)

    def _finish_item(
        self,
        item_id: uuid.UUID,
        status: CollectionJobItemStatus,
        *,
        crawl_run_id: uuid.UUID | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        with self.session_factory() as session:
            item = session.get(CollectionJobItem, item_id)
            if item is None or item.status is not CollectionJobItemStatus.RUNNING:
                raise WorkerOwnershipError("Collection job item ownership was lost.")
            item.status = status
            item.finished_at = utc_now()
            item.crawl_run_id = crawl_run_id
            item.error_code = error_code
            item.error_summary = (error_summary or "")[:1000] or None
            _refresh_job(item.job)
            session.commit()

    def _note_system_failure(self, item_id: uuid.UUID, error: Exception) -> None:
        try:
            with self.session_factory() as session:
                item = session.get(CollectionJobItem, item_id)
                if item is not None:
                    item.job.error_summary = f"WORKER_SYSTEM_FAILURE: {_error_summary(error)}"
                    session.commit()
        except SQLAlchemyError:
            logger.exception("system failure marker could not be persisted")

    def _release_busy_item(self, item_id: uuid.UUID, error: Exception) -> None:
        """Keep manual intent pending when another owner still holds the Source claim."""
        with self.session_factory() as session:
            item = session.get(CollectionJobItem, item_id)
            if item is None or item.status is not CollectionJobItemStatus.RUNNING:
                raise WorkerOwnershipError("Collection job item ownership was lost.")
            item.status = CollectionJobItemStatus.PENDING
            item.started_at = None
            item.finished_at = None
            item.error_code = "SOURCE_BUSY_RETRY"
            item.error_summary = _error_summary(error)
            session.commit()
