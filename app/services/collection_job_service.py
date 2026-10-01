"""Persistent collection-job creation, scope resolution, and projections."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Agency, CollectionJob, CollectionJobItem, CollectionJobItemStatus,
    CollectionJobStatus, CollectionTriggerType, OrgUnit, Source, SourceBinding,
)


MANUAL_PRIORITY = 100
SCHEDULED_RETRY_PRIORITY = 50
SCHEDULED_FULL_PRIORITY = 10
MANUAL_TRIGGERS = {
    CollectionTriggerType.MANUAL_SOURCE,
    CollectionTriggerType.MANUAL_SELECTION,
    CollectionTriggerType.MANUAL_ORG_UNIT,
    CollectionTriggerType.MANUAL_AGENCY,
    CollectionTriggerType.MANUAL_REGION,
    CollectionTriggerType.MANUAL_ALL,
}


class CollectionJobError(ValueError):
    pass


class CollectionJobService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_manual(
        self,
        trigger_type: CollectionTriggerType,
        *,
        requested_by_user_id: uuid.UUID | None,
        source_id: uuid.UUID | None = None,
        source_ids: list[uuid.UUID] | None = None,
        agency_id: uuid.UUID | None = None,
        org_unit_id: uuid.UUID | None = None,
        region_code: str | None = None,
    ) -> CollectionJob:
        if trigger_type not in MANUAL_TRIGGERS:
            raise CollectionJobError("Manual API cannot create scheduled jobs.")
        context: dict[str, object] = {"scope": trigger_type.value}
        statement = self._eligible_source_statement(scheduled=False)
        if trigger_type is CollectionTriggerType.MANUAL_SOURCE:
            if source_id is None:
                raise CollectionJobError("source_id is required.")
            statement = statement.where(Source.id == source_id)
            context["source_id"] = str(source_id)
        elif trigger_type is CollectionTriggerType.MANUAL_SELECTION:
            selected = sorted(set(source_ids or []), key=str)
            if not selected:
                raise CollectionJobError("source_ids is required.")
            statement = statement.where(Source.id.in_(selected))
            context["source_ids"] = [str(value) for value in selected]
        elif trigger_type is CollectionTriggerType.MANUAL_ORG_UNIT:
            if org_unit_id is None:
                raise CollectionJobError("org_unit_id is required.")
            unit = self.session.get(OrgUnit, org_unit_id)
            if unit is None or not unit.active:
                raise CollectionJobError("Organization unit was not found.")
            statement = statement.where(SourceBinding.org_unit_id == org_unit_id)
            context.update({"org_unit_id": str(org_unit_id), "org_unit_name": unit.name})
        elif trigger_type is CollectionTriggerType.MANUAL_AGENCY:
            if agency_id is None:
                raise CollectionJobError("agency_id is required.")
            agency = self.session.get(Agency, agency_id)
            if agency is None or not agency.active:
                raise CollectionJobError("Agency was not found.")
            statement = statement.where(SourceBinding.agency_id == agency_id)
            context.update({"agency_id": str(agency_id), "agency_name": agency.official_name})
        elif trigger_type is CollectionTriggerType.MANUAL_REGION:
            region = (region_code or "").strip()
            if not region:
                raise CollectionJobError("region_code is required.")
            statement = statement.join(Agency, Agency.id == SourceBinding.agency_id).where(Agency.region_code == region)
            context["region_code"] = region
        source_values = list(self.session.scalars(statement.order_by(Source.normalized_url)).unique())
        return self.create_from_sources(
            trigger_type, source_values, priority=MANUAL_PRIORITY,
            requested_by_user_id=requested_by_user_id, trigger_context=context,
        )

    def create_from_sources(
        self,
        trigger_type: CollectionTriggerType,
        sources: list[Source] | tuple[Source, ...],
        *,
        priority: int,
        requested_by_user_id: uuid.UUID | None = None,
        trigger_context: dict | None = None,
        schedule_slot_key: str | None = None,
        parent_job_id: uuid.UUID | None = None,
    ) -> CollectionJob:
        distinct_sources = {source.id: source for source in sources}
        ordered = sorted(distinct_sources.values(), key=lambda value: (value.normalized_url, str(value.id)))
        if not ordered:
            raise CollectionJobError("No active collection sources matched this scope.")
        job = CollectionJob(
            trigger_type=trigger_type,
            status=CollectionJobStatus.PENDING,
            priority=priority,
            trigger_context=trigger_context or {},
            requested_by_user_id=requested_by_user_id,
            parent_job_id=parent_job_id,
            schedule_slot_key=schedule_slot_key,
            total_items=len(ordered),
        )
        job.items = [
            CollectionJobItem(source_id=source.id, sequence=index, status=CollectionJobItemStatus.PENDING)
            for index, source in enumerate(ordered, start=1)
        ]
        self.session.add(job)
        self.session.commit()
        return job

    def scheduled_sources(self, source_ids: set[uuid.UUID] | None = None) -> list[Source]:
        statement = self._eligible_source_statement(scheduled=True)
        if source_ids is not None:
            statement = statement.where(Source.id.in_(source_ids))
        return list(self.session.scalars(statement.order_by(Source.normalized_url)).unique())

    @staticmethod
    def _eligible_source_statement(*, scheduled: bool):
        statement = (
            select(Source)
            .join(SourceBinding, SourceBinding.source_id == Source.id)
            .where(Source.active.is_(True), SourceBinding.active.is_(True))
            .distinct()
        )
        if scheduled:
            statement = statement.where(Source.scheduled_refresh_enabled.is_(True))
        return statement

    def get(self, job_id: uuid.UUID) -> CollectionJob:
        job = self.session.scalar(
            select(CollectionJob)
            .options(selectinload(CollectionJob.items).selectinload(CollectionJobItem.source))
            .where(CollectionJob.id == job_id)
        )
        if job is None:
            raise CollectionJobError("Collection job was not found.")
        return job

    def list_recent(
        self, *, failures_only: bool = False, limit: int = 100,
        include_items: bool = False,
    ) -> list[dict]:
        statement = (
            select(CollectionJob)
            .options(selectinload(CollectionJob.items).selectinload(CollectionJobItem.source))
            .order_by(CollectionJob.created_at.desc(), CollectionJob.id.desc())
            .limit(max(1, min(limit, 500)))
        )
        if failures_only:
            statement = statement.where(CollectionJob.failed_items > 0)
        return [self.project(job, include_items=include_items) for job in self.session.scalars(statement).unique()]

    @staticmethod
    def project(job: CollectionJob, *, include_items: bool = True) -> dict:
        completed = job.succeeded_items + job.failed_items + job.skipped_items
        value = {
            "id": str(job.id),
            "trigger_type": job.trigger_type.value,
            "status": job.status.value,
            "priority": job.priority,
            "trigger_context": job.trigger_context,
            "total_items": job.total_items,
            "completed_items": completed,
            "succeeded_items": job.succeeded_items,
            "failed_items": job.failed_items,
            "skipped_items": job.skipped_items,
            "progress_percent": round(100 * completed / job.total_items) if job.total_items else 100,
            "error_summary": job.error_summary,
            "created_at": job.created_at.isoformat(),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "parent_job_id": str(job.parent_job_id) if job.parent_job_id else None,
        }
        if include_items:
            value["items"] = [
                {
                    "id": str(item.id), "source_id": str(item.source_id),
                    "source": item.source.title or item.source.url,
                    "sequence": item.sequence, "status": item.status.value,
                    "attempt_count": item.attempt_count,
                    "crawl_run_id": str(item.crawl_run_id) if item.crawl_run_id else None,
                    "error_code": item.error_code, "error_summary": item.error_summary,
                    "started_at": item.started_at.isoformat() if item.started_at else None,
                    "finished_at": item.finished_at.isoformat() if item.finished_at else None,
                }
                for item in job.items
            ]
        return value
