"""One normalized filter contract for the Run History workspace."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.core.time_presentation import SEOUL
from app.models import RunStatus


@dataclass(frozen=True, slots=True)
class RunHistoryFilter:
    date_from: date | None = None
    date_to: date | None = None
    agency_id: uuid.UUID | None = None
    status: RunStatus | None = None
    search: str = ""

    @classmethod
    def normalize(
        cls,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        agency_id: uuid.UUID | None = None,
        status: RunStatus | None = None,
        search: str | None = None,
    ) -> "RunHistoryFilter":
        return cls(
            date_from=date_from,
            date_to=date_to,
            agency_id=agency_id,
            status=status,
            search=(search or "").strip(),
        )

    @property
    def utc_start(self) -> datetime | None:
        if self.date_from is None:
            return None
        return datetime.combine(self.date_from, time.min, tzinfo=SEOUL).astimezone(timezone.utc)

    @property
    def utc_end_exclusive(self) -> datetime | None:
        if self.date_to is None:
            return None
        next_day = self.date_to + timedelta(days=1)
        return datetime.combine(next_day, time.min, tzinfo=SEOUL).astimezone(timezone.utc)

    @property
    def run_statuses(self) -> tuple[RunStatus, ...]:
        if self.status is RunStatus.RUNNING:
            return (RunStatus.PENDING, RunStatus.RUNNING)
        return (self.status,) if self.status is not None else ()
