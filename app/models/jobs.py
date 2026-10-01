"""Persistent collection job queue and per-source execution records."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import UTCDateTime, UUIDPrimaryKeyMixin, enum_type, utc_now
from app.models.enums import CollectionJobItemStatus, CollectionJobStatus, CollectionTriggerType


class CollectionJob(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "collection_jobs"
    __table_args__ = (
        Index("ix_collection_jobs_dispatch", "status", "priority", "created_at"),
        UniqueConstraint("schedule_slot_key", name="uq_collection_jobs_schedule_slot"),
    )

    trigger_type: Mapped[CollectionTriggerType] = mapped_column(enum_type(CollectionTriggerType), nullable=False, index=True)
    status: Mapped[CollectionJobStatus] = mapped_column(
        enum_type(CollectionJobStatus), default=CollectionJobStatus.PENDING, nullable=False, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    trigger_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("collection_jobs.id"), nullable=True, index=True)
    schedule_slot_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    total_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    succeeded_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False)

    items: Mapped[list["CollectionJobItem"]] = relationship(
        "CollectionJobItem", back_populates="job", cascade="all, delete-orphan", order_by="CollectionJobItem.sequence"
    )
    parent_job: Mapped["CollectionJob | None"] = relationship("CollectionJob", remote_side="CollectionJob.id")


class CollectionJobItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "collection_job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "source_id", name="uq_collection_job_item_source"),
        Index("ix_collection_job_items_dispatch", "status", "job_id", "sequence"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collection_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[CollectionJobItemStatus] = mapped_column(
        enum_type(CollectionJobItemStatus), default=CollectionJobItemStatus.PENDING, nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    crawl_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crawl_runs.id"), nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False)

    job: Mapped[CollectionJob] = relationship("CollectionJob", back_populates="items")
    source: Mapped["Source"] = relationship("Source")
    crawl_run: Mapped["CrawlRun | None"] = relationship("CrawlRun")
