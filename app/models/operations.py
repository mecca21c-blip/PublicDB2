'''Application users and typed operational settings.'''

from __future__ import annotations

from datetime import datetime

from typing import Any

from sqlalchemy import Boolean, JSON, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import ActiveLifecycleMixin, CreatedUpdatedMixin, UTCDateTime, UUIDPrimaryKeyMixin, enum_type
from app.models.enums import RefreshRecurrence, UserRole


class User(UUIDPrimaryKeyMixin, ActiveLifecycleMixin, Base):
    __tablename__ = 'users'

    username: Mapped[str] = mapped_column(String(150), nullable=False)
    normalized_username: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    role: Mapped[UserRole] = mapped_column(enum_type(UserRole), nullable=False, index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class OperationalSettings(UUIDPrimaryKeyMixin, CreatedUpdatedMixin, Base):
    __tablename__ = 'operational_settings'

    singleton_key: Mapped[str] = mapped_column(String(30), nullable=False, unique=True, default='default')
    http_timeout_seconds: Mapped[float] = mapped_column(nullable=False)
    max_response_bytes: Mapped[int] = mapped_column(nullable=False)
    user_agent: Mapped[str] = mapped_column(String(500), nullable=False)
    automatic_refresh_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refresh_recurrence: Mapped[RefreshRecurrence] = mapped_column(
        enum_type(RefreshRecurrence), nullable=False, default=RefreshRecurrence.WEEKLY
    )
    refresh_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True, default=5)
    refresh_day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    refresh_time_of_day: Mapped[str] = mapped_column(String(5), nullable=False, default='02:00')
    retry_failed_next_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class OperationClaim(UUIDPrimaryKeyMixin, Base):
    """Database-backed claim for cross-session idempotency and exclusion."""

    __tablename__ = 'operation_claims'
    __table_args__ = (UniqueConstraint('resource_key', name='uq_operation_claim_resource'),)

    resource_key: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    owner_token: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='ACTIVE', index=True)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
