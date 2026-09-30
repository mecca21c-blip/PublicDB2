'''Application users and typed operational settings.'''

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
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
