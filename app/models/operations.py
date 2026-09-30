'''Application users and typed operational settings.'''

from __future__ import annotations

from datetime import datetime

from typing import Any

from sqlalchemy import JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import ActiveLifecycleMixin, CreatedUpdatedMixin, UTCDateTime, UUIDPrimaryKeyMixin, enum_type
from app.models.enums import UserRole


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
