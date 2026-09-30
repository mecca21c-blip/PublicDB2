'''Typed operational settings with code-owned safety bounds.'''

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import HTTP_TIMEOUT_SECONDS, MAX_RESPONSE_BYTES, PUBLICDB_USER_AGENT
from app.models import OperationalSettings


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

    @property
    def max_response_mb(self) -> float:
        return self.max_response_bytes / (1024 * 1024)


class SettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def snapshot(self) -> SettingsSnapshot:
        row = self.session.scalar(
            select(OperationalSettings).where(OperationalSettings.singleton_key == 'default')
        )
        if row is None:
            return SettingsSnapshot(HTTP_TIMEOUT_SECONDS, MAX_RESPONSE_BYTES, PUBLICDB_USER_AGENT)
        return SettingsSnapshot(row.http_timeout_seconds, row.max_response_bytes, row.user_agent)

    def update(self, *, http_timeout_seconds: float, max_response_bytes: int, user_agent: str) -> SettingsSnapshot:
        timeout = float(http_timeout_seconds)
        response_bytes = int(max_response_bytes)
        agent = (user_agent or '').strip()
        if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise SettingsServiceError('요청 제한 시간은 1초 이상 60초 이하여야 합니다.')
        if not MIN_RESPONSE_BYTES <= response_bytes <= MAX_RESPONSE_BYTES_SETTING:
            raise SettingsServiceError('최대 응답 크기는 64KB 이상 50MB 이하여야 합니다.')
        if not agent or len(agent) > 500 or any(ord(character) < 32 for character in agent):
            raise SettingsServiceError('User-Agent를 입력하세요.')
        row = self.session.scalar(
            select(OperationalSettings).where(OperationalSettings.singleton_key == 'default')
        )
        if row is None:
            row = OperationalSettings(singleton_key='default')
            self.session.add(row)
        row.http_timeout_seconds = timeout
        row.max_response_bytes = response_bytes
        row.user_agent = agent
        self.session.commit()
        return self.snapshot()
