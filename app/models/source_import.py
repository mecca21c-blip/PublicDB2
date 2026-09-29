"""Confirmed source import audit metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import UTCDateTime, UUIDPrimaryKeyMixin


class SourceImportLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "source_import_logs"

    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    input_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    result_counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
