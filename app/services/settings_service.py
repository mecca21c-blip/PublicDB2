"""Typed operational settings with code-owned safety bounds."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import HTTP_TIMEOUT_SECONDS, MAX_RESPONSE_BYTES, PUBLICDB_USER_AGENT
from app.models import OperationalSettings, RefreshRecurrence


MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 60.0
MIN_RESPONSE_BYTES = 64 * 1024
MAX_RESPONSE_BYTES_SETTING = 50 * 1024 * 1024


class SettingsServiceError(ValueError):
    pass


@dataclass(frozen=True)
class SettingsSnapshot:
    http_timeout_seconds: float
    max_response_bytes: int
    user_agent: str
    automatic_refresh_enabled: bool = False
    refresh_recurrence: RefreshRecurrence = RefreshRecurrence.WEEKLY
    refresh_weekday: int | None = 5
    refresh_day_of_month: int | None = None
    refresh_time_of_day: str = "02:00"
    retry_failed_next_day: bool = True

    @property
    def max_response_mb(self) -> float:
        return self.max_response_bytes / (1024 * 1024)


class SettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def snapshot(self) -> SettingsSnapshot:
        row = self.session.scalar(
            select(OperationalSettings).where(OperationalSettings.singleton_key == "default")
        )
        if row is None:
            return SettingsSnapshot(HTTP_TIMEOUT_SECONDS, MAX_RESPONSE_BYTES, PUBLICDB_USER_AGENT)
        return SettingsSnapshot(
            row.http_timeout_seconds,
            row.max_response_bytes,
            row.user_agent,
            row.automatic_refresh_enabled,
            row.refresh_recurrence,
            row.refresh_weekday,
            row.refresh_day_of_month,
            row.refresh_time_of_day,
            row.retry_failed_next_day,
        )

    def update(
        self,
        *,
        http_timeout_seconds: float,
        max_response_bytes: int,
        user_agent: str,
        automatic_refresh_enabled: bool | None = None,
        refresh_recurrence: RefreshRecurrence | str | None = None,
        refresh_weekday: int | None = None,
        refresh_day_of_month: int | None = None,
        refresh_time_of_day: str | None = None,
        retry_failed_next_day: bool | None = None,
    ) -> SettingsSnapshot:
        timeout = float(http_timeout_seconds)
        response_bytes = int(max_response_bytes)
        agent = (user_agent or "").strip()
        if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise SettingsServiceError("HTTP timeout must be between 1 and 60 seconds.")
        if not MIN_RESPONSE_BYTES <= response_bytes <= MAX_RESPONSE_BYTES_SETTING:
            raise SettingsServiceError("Maximum response size must be between 64KB and 50MB.")
        if not agent or len(agent) > 500 or any(ord(character) < 32 for character in agent):
            raise SettingsServiceError("User-Agent is required.")

        recurrence = RefreshRecurrence(refresh_recurrence or RefreshRecurrence.WEEKLY)
        schedule_time = (refresh_time_of_day or "02:00").strip()
        try:
            hour_text, minute_text = schedule_time.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (TypeError, ValueError) as error:
            raise SettingsServiceError("Automatic refresh time must use HH:MM.") from error
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise SettingsServiceError("Automatic refresh time must use HH:MM.")
        schedule_time = f"{hour:02d}:{minute:02d}"
        if recurrence is RefreshRecurrence.WEEKLY and refresh_weekday is not None and not 0 <= refresh_weekday <= 6:
            raise SettingsServiceError("Weekly refresh weekday must be between 0 and 6.")
        if recurrence is RefreshRecurrence.MONTHLY and refresh_day_of_month is not None and not 1 <= refresh_day_of_month <= 31:
            raise SettingsServiceError("Monthly refresh day must be between 1 and 31.")

        row = self.session.scalar(
            select(OperationalSettings).where(OperationalSettings.singleton_key == "default")
        )
        if row is None:
            row = OperationalSettings(singleton_key="default")
            self.session.add(row)
        row.http_timeout_seconds = timeout
        row.max_response_bytes = response_bytes
        row.user_agent = agent
        row.automatic_refresh_enabled = (
            bool(automatic_refresh_enabled)
            if automatic_refresh_enabled is not None
            else bool(row.automatic_refresh_enabled)
        )
        row.refresh_recurrence = recurrence
        row.refresh_weekday = 5 if recurrence is RefreshRecurrence.WEEKLY and refresh_weekday is None else refresh_weekday
        row.refresh_day_of_month = 1 if recurrence is RefreshRecurrence.MONTHLY and refresh_day_of_month is None else refresh_day_of_month
        row.refresh_time_of_day = schedule_time
        row.retry_failed_next_day = True if retry_failed_next_day is None else bool(retry_failed_next_day)
        self.session.commit()
        return self.snapshot()
