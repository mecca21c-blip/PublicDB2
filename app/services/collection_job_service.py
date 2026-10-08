"""Persistent collection-job creation, scope resolution, and projections."""

from __future__ import annotations

import uuid
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Agency, CollectionJob, CollectionJobItem, CollectionJobItemStatus,
    CollectionJobStatus, CollectionTriggerType, OrgUnit, Source, SourceBinding,
)
from app.models.common import utc_now
from app.core.time_presentation import format_kst_datetime, serialize_utc_datetime
from app.services.source_query_service import SourceFilterError, SourceFilterSpec, SourceQueryService


MANUAL_PRIORITY = 100
SCHEDULED_RETRY_PRIORITY = 50
SCHEDULED_FULL_PRIORITY = 10
MANUAL_TRIGGERS = {
    CollectionTriggerType.MANUAL_SOURCE,
    CollectionTriggerType.MANUAL_SELECTION,
    CollectionTriggerType.MANUAL_FILTER,
    CollectionTriggerType.MANUAL_ORG_UNIT,
    CollectionTriggerType.MANUAL_AGENCY,
    CollectionTriggerType.MANUAL_REGION,
    CollectionTriggerType.MANUAL_ALL,
}
ACTIVE_JOB_STATUSES = (CollectionJobStatus.PENDING, CollectionJobStatus.RUNNING)
TERMINAL_FEEDBACK_SECONDS = 30
TRIGGER_LABELS = {
    CollectionTriggerType.MANUAL_SOURCE: "개별 소스 즉시 수집",
    CollectionTriggerType.MANUAL_SELECTION: "선택 소스 즉시 수집",
    CollectionTriggerType.MANUAL_FILTER: "검색 결과 즉시 수집",
    CollectionTriggerType.MANUAL_ORG_UNIT: "부서 범위 즉시 수집",
    CollectionTriggerType.MANUAL_AGENCY: "기관 범위 즉시 수집",
    CollectionTriggerType.MANUAL_REGION: "지역 범위 즉시 수집",
    CollectionTriggerType.MANUAL_ALL: "전체 소스 즉시 수집",
    CollectionTriggerType.SCHEDULED_FULL: "자동 전체 수집",
    CollectionTriggerType.SCHEDULED_RETRY: "자동 실패 재시도",
}
JOB_STATUS_LABELS = {
    CollectionJobStatus.PENDING: "수집 대기",
    CollectionJobStatus.RUNNING: "수집 중",
    CollectionJobStatus.COMPLETED: "완료",
    CollectionJobStatus.COMPLETED_WITH_ERRORS: "완료 · 오류 있음",
    CollectionJobStatus.FAILED: "실패",
    CollectionJobStatus.CANCELLED: "취소됨",
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
        filter_spec: SourceFilterSpec | None = None,
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
        elif trigger_type is CollectionTriggerType.MANUAL_FILTER:
            if filter_spec is None:
                raise CollectionJobError("filter is required.")
            try:
                query = SourceQueryService(self.session)
                source_values = query.resolve_sources(filter_spec, eligible_only=True)
                context["filter"] = query.snapshot(filter_spec, resolved_count=len(source_values))
            except SourceFilterError as error:
                raise CollectionJobError(str(error)) from error
            return self.create_from_sources(
                trigger_type, source_values, priority=MANUAL_PRIORITY,
                requested_by_user_id=requested_by_user_id, trigger_context=context,
            )
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

    def scheduled_source_count(self) -> int:
        statement = (
            select(func.count(func.distinct(Source.id)))
            .select_from(Source)
            .join(SourceBinding, SourceBinding.source_id == Source.id)
            .where(
                Source.active.is_(True),
                SourceBinding.active.is_(True),
                Source.scheduled_refresh_enabled.is_(True),
            )
        )
        return int(self.session.scalar(statement) or 0)

    def latest_scheduled_full(self) -> dict | None:
        job = self.session.scalar(
            select(CollectionJob)
            .where(CollectionJob.trigger_type == CollectionTriggerType.SCHEDULED_FULL)
            .order_by(CollectionJob.created_at.desc(), CollectionJob.id.desc())
            .limit(1)
        )
        if job is None:
            return None
        projection = self.project(job, include_items=False)
        projection["status_label"] = JOB_STATUS_LABELS[job.status]
        return projection

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

    def get_summary(self, job_id: uuid.UUID) -> CollectionJob:
        job = self.session.get(CollectionJob, job_id)
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

    def active_snapshot(self) -> dict:
        """Return one bounded DB-owned status projection for shared-shell polling."""
        active_count = self.session.scalar(
            select(func.count()).select_from(CollectionJob).where(CollectionJob.status.in_(ACTIVE_JOB_STATUSES))
        ) or 0
        current_item = self.session.scalar(
            select(CollectionJobItem)
            .join(CollectionJob, CollectionJob.id == CollectionJobItem.job_id)
            .where(
                CollectionJob.status.in_(ACTIVE_JOB_STATUSES),
                CollectionJobItem.status == CollectionJobItemStatus.RUNNING,
            )
            .order_by(CollectionJob.priority.desc(), CollectionJob.created_at, CollectionJobItem.sequence)
            .limit(1)
        )
        job = current_item.job if current_item is not None else self.session.scalar(
            select(CollectionJob)
            .where(CollectionJob.status.in_(ACTIVE_JOB_STATUSES))
            .order_by(CollectionJob.priority.desc(), CollectionJob.created_at, CollectionJob.id)
            .limit(1)
        )
        recent_terminal = False
        if job is None:
            candidate = self.session.scalar(
                select(CollectionJob)
                .where(CollectionJob.finished_at.is_not(None))
                .order_by(CollectionJob.finished_at.desc(), CollectionJob.id.desc())
                .limit(1)
            )
            if candidate and candidate.finished_at >= utc_now() - timedelta(seconds=TERMINAL_FEEDBACK_SECONDS):
                job = candidate
                recent_terminal = True
        if job is None:
            return {"active_job_count": 0, "recent_terminal": False, "job": None}
        if current_item is None and not recent_terminal:
            current_item = self.session.scalar(
                select(CollectionJobItem)
                .where(
                    CollectionJobItem.job_id == job.id,
                    CollectionJobItem.status.in_((CollectionJobItemStatus.RUNNING, CollectionJobItemStatus.PENDING)),
                )
                .order_by(CollectionJobItem.sequence)
                .limit(1)
            )
        projection = self.summary_projection(job, current_item=current_item)
        return {
            "active_job_count": active_count,
            "recent_terminal": recent_terminal,
            "job": projection,
        }

    def summary_projection(
        self, job: CollectionJob, *, current_item: CollectionJobItem | None = None,
    ) -> dict:
        if current_item is None and job.status in ACTIVE_JOB_STATUSES:
            current_item = self.session.scalar(
                select(CollectionJobItem)
                .where(
                    CollectionJobItem.job_id == job.id,
                    CollectionJobItem.status.in_((CollectionJobItemStatus.RUNNING, CollectionJobItemStatus.PENDING)),
                )
                .order_by(
                    (CollectionJobItem.status == CollectionJobItemStatus.RUNNING).desc(),
                    CollectionJobItem.sequence,
                )
                .limit(1)
            )
        projection = self.project(job, include_items=False)
        projection["trigger_label"] = TRIGGER_LABELS.get(job.trigger_type, "수집 작업")
        projection["current_item"] = self._current_item_projection(current_item)
        return projection

    def _current_item_projection(self, item: CollectionJobItem | None) -> dict | None:
        if item is None:
            return None
        source = item.source
        binding = self.session.scalar(
            select(SourceBinding)
            .options(selectinload(SourceBinding.agency), selectinload(SourceBinding.org_unit))
            .where(SourceBinding.source_id == source.id, SourceBinding.active.is_(True))
            .order_by(SourceBinding.created_at, SourceBinding.id)
            .limit(1)
        )
        parsed = urlsplit(source.url)
        display_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:160]
        method_labels = {
            "WEB_PAGE": "개별 URL · 스크래핑",
            "WEB_CRAWL": "Index URL · 크롤링",
            "API": "공개 API / RSS",
        }
        return {
            "source_id": str(source.id),
            "status": item.status.value,
            "agency": binding.agency.official_name if binding else None,
            "org_unit": binding.org_unit.name if binding and binding.org_unit else None,
            "method": method_labels.get(source.collection_method.value, source.collection_method.value),
            "url": display_url,
        }

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
            "created_at": serialize_utc_datetime(job.created_at),
            "created_at_display": format_kst_datetime(job.created_at),
            "started_at": serialize_utc_datetime(job.started_at),
            "finished_at": serialize_utc_datetime(job.finished_at),
            "finished_at_display": format_kst_datetime(job.finished_at),
            "parent_job_id": str(job.parent_job_id) if job.parent_job_id else None,
            "scope_summary": CollectionJobService._scope_summary(job),
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
                    "started_at": serialize_utc_datetime(item.started_at),
                    "finished_at": serialize_utc_datetime(item.finished_at),
                }
                for item in job.items
            ]
        return value

    @staticmethod
    def _scope_summary(job: CollectionJob) -> str:
        if job.trigger_type is not CollectionTriggerType.MANUAL_FILTER:
            return job.trigger_type.value
        snapshot = job.trigger_context.get("filter", {})
        parts = []
        if snapshot.get("region_codes"):
            parts.append("지역 " + ", ".join(snapshot["region_codes"]))
        if snapshot.get("agency_name"):
            parts.append("기관 " + snapshot["agency_name"])
        if snapshot.get("org_unit_name"):
            parts.append("부서 " + snapshot["org_unit_name"])
        if snapshot.get("methods"):
            labels = {"WEB_PAGE": "스크래핑", "WEB_CRAWL": "크롤링", "API": "API/RSS"}
            parts.append("방식 " + ", ".join(labels.get(value, value) for value in snapshot["methods"]))
        if snapshot.get("status"):
            parts.append("상태 " + snapshot["status"])
        if snapshot.get("scheduled") and snapshot["scheduled"] != "all":
            parts.append("자동 전체 수집 " + ("포함" if snapshot["scheduled"] == "included" else "제외"))
        if snapshot.get("search"):
            parts.append("검색 “" + snapshot["search"] + "”")
        parts.append(f"{snapshot.get('resolved_count', job.total_items)}개")
        return " · ".join(parts)
