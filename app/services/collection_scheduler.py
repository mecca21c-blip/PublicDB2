"""Database-authoritative global collection schedule in Asia/Seoul."""

from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    CollectionJob, CollectionJobItem, CollectionJobItemStatus, CollectionJobStatus,
    CollectionTriggerType, RefreshRecurrence,
)
from app.models.common import utc_now
from app.services.collection_job_service import (
    CollectionJobError, CollectionJobService, SCHEDULED_FULL_PRIORITY,
    SCHEDULED_RETRY_PRIORITY,
)
from app.services.settings_service import SettingsService, SettingsSnapshot


SEOUL = ZoneInfo("Asia/Seoul")


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute, tzinfo=SEOUL)


def _month_slot(year: int, month: int, day: int, clock: time) -> datetime:
    bounded = min(day, calendar.monthrange(year, month)[1])
    return datetime.combine(date(year, month, bounded), clock)


def latest_schedule_slot(settings: SettingsSnapshot, now: datetime) -> datetime:
    local_now = now.astimezone(SEOUL)
    clock = _clock(settings.refresh_time_of_day)
    if settings.refresh_recurrence is RefreshRecurrence.DAILY:
        candidate = datetime.combine(local_now.date(), clock)
        return candidate if candidate <= local_now else candidate - timedelta(days=1)
    if settings.refresh_recurrence is RefreshRecurrence.WEEKLY:
        weekday = settings.refresh_weekday if settings.refresh_weekday is not None else 5
        candidate_date = local_now.date() - timedelta(days=(local_now.weekday() - weekday) % 7)
        candidate = datetime.combine(candidate_date, clock)
        return candidate if candidate <= local_now else candidate - timedelta(days=7)
    day = settings.refresh_day_of_month or 1
    candidate = _month_slot(local_now.year, local_now.month, day, clock)
    if candidate <= local_now:
        return candidate
    year, month = (local_now.year - 1, 12) if local_now.month == 1 else (local_now.year, local_now.month - 1)
    return _month_slot(year, month, day, clock)


def next_schedule_slot(settings: SettingsSnapshot, now: datetime) -> datetime:
    latest = latest_schedule_slot(settings, now)
    if settings.refresh_recurrence is RefreshRecurrence.DAILY:
        return latest + timedelta(days=1)
    if settings.refresh_recurrence is RefreshRecurrence.WEEKLY:
        return latest + timedelta(days=7)
    year, month = (latest.year + 1, 1) if latest.month == 12 else (latest.year, latest.month + 1)
    return _month_slot(year, month, settings.refresh_day_of_month or 1, _clock(settings.refresh_time_of_day))


class CollectionScheduler:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def next_run(self, now: datetime | None = None) -> datetime | None:
        with self.session_factory() as session:
            settings = SettingsService(session).snapshot()
        if not settings.automatic_refresh_enabled:
            return None
        return next_schedule_slot(settings, now or utc_now())

    def tick(self, now: datetime | None = None) -> list[uuid.UUID]:
        current = (now or utc_now()).astimezone(SEOUL)
        created: list[uuid.UUID] = []
        with self.session_factory() as session:
            settings = SettingsService(session).snapshot()
            if not settings.automatic_refresh_enabled:
                return created
            retry = self._create_due_retry(session, settings, current)
            if retry is not None:
                created.append(retry.id)
            full = self._create_due_full(session, settings, current)
            if full is not None:
                created.append(full.id)
        return created

    def _create_due_full(
        self, session: Session, settings: SettingsSnapshot, now: datetime
    ) -> CollectionJob | None:
        active = session.scalar(
            select(CollectionJob.id).where(
                CollectionJob.trigger_type == CollectionTriggerType.SCHEDULED_FULL,
                CollectionJob.status.in_((CollectionJobStatus.PENDING, CollectionJobStatus.RUNNING)),
            ).limit(1)
        )
        if active is not None:
            return None
        slot = latest_schedule_slot(settings, now)
        slot_key = f"scheduled-full:{slot.isoformat()}"
        service = CollectionJobService(session)
        try:
            return service.create_from_sources(
                CollectionTriggerType.SCHEDULED_FULL,
                service.scheduled_sources(),
                priority=SCHEDULED_FULL_PRIORITY,
                trigger_context={"scope": "SCHEDULED_FULL", "slot": slot.isoformat()},
                schedule_slot_key=slot_key,
            )
        except (CollectionJobError, IntegrityError):
            session.rollback()
            return None

    def _create_due_retry(
        self, session: Session, settings: SettingsSnapshot, now: datetime
    ) -> CollectionJob | None:
        if not settings.retry_failed_next_day:
            return None
        parent = session.scalar(
            select(CollectionJob)
            .where(
                CollectionJob.trigger_type == CollectionTriggerType.SCHEDULED_FULL,
            )
            .order_by(CollectionJob.created_at.desc())
            .limit(1)
        )
        if (
            parent is None
            or parent.status is not CollectionJobStatus.COMPLETED_WITH_ERRORS
            or parent.failed_items <= 0
            or session.scalar(select(CollectionJob.id).where(CollectionJob.parent_job_id == parent.id).limit(1)) is not None
        ):
            return None
        slot_text = str(parent.trigger_context.get("slot") or "")
        try:
            retry_due = datetime.fromisoformat(slot_text).astimezone(SEOUL) + timedelta(days=1)
        except ValueError:
            return None
        if retry_due > now:
            return None
        failed_ids = set(session.scalars(
            select(CollectionJobItem.source_id).where(
                CollectionJobItem.job_id == parent.id,
                CollectionJobItem.status == CollectionJobItemStatus.FAILED,
            )
        ))
        service = CollectionJobService(session)
        try:
            return service.create_from_sources(
                CollectionTriggerType.SCHEDULED_RETRY,
                service.scheduled_sources(failed_ids),
                priority=SCHEDULED_RETRY_PRIORITY,
                trigger_context={
                    "scope": "SCHEDULED_RETRY", "parent_job_id": str(parent.id),
                    "slot": retry_due.isoformat(),
                },
                schedule_slot_key=f"scheduled-retry:{parent.id}",
                parent_job_id=parent.id,
            )
        except (CollectionJobError, IntegrityError):
            session.rollback()
            return None
