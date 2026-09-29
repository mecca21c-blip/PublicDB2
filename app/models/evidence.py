"""Collection evidence, discovery, change, and confirmed-contact history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, JSON, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import UTCDateTime, UUIDPrimaryKeyMixin, enum_type, utc_now
from app.models.enums import ChangeEventType, EntityType, ReviewStatus, RunStatus, StageStatus


class CrawlRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "crawl_runs"

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    status: Mapped[RunStatus] = mapped_column(enum_type(RunStatus), nullable=False, index=True)
    connection_status: Mapped[StageStatus] = mapped_column(enum_type(StageStatus), default=StageStatus.PENDING, nullable=False)
    raw_status: Mapped[StageStatus] = mapped_column(enum_type(StageStatus), default=StageStatus.PENDING, nullable=False)
    extraction_status: Mapped[StageStatus] = mapped_column(enum_type(StageStatus), default=StageStatus.PENDING, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    records_observed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped["Source"] = relationship("Source", back_populates="crawl_runs")
    observations: Mapped[list["Observation"]] = relationship("Observation", back_populates="crawl_run")


class Observation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "observations"

    crawl_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    page_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_text_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_payload: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    crawl_run: Mapped[CrawlRun] = relationship("CrawlRun", back_populates="observations")
    source: Mapped["Source"] = relationship("Source")
    occurrences: Mapped[list["SourceOccurrence"]] = relationship("SourceOccurrence", back_populates="observation")


class SourceOccurrence(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "source_occurrences"
    __table_args__ = (Index("ix_source_occurrences_entity", "entity_type", "entity_id"),)

    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("observations.id"), nullable=False, index=True)
    entity_type: Mapped[EntityType] = mapped_column(enum_type(EntityType), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    observed_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_locator: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    observation: Mapped[Observation] = relationship("Observation", back_populates="occurrences")


class ChangeEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "change_events"
    __table_args__ = (Index("ix_change_events_entity", "entity_type", "entity_id"),)

    entity_type: Mapped[EntityType] = mapped_column(enum_type(EntityType), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[ChangeEventType] = mapped_column(enum_type(ChangeEventType), nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    old_value: Mapped[dict[str, Any] | list[Any] | str | int | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict[str, Any] | list[Any] | str | int | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[ReviewStatus] = mapped_column(enum_type(ReviewStatus), nullable=False, index=True)
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class ChangeDetection(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "change_detections"

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("observations.id"), nullable=False, index=True)
    status: Mapped[RunStatus] = mapped_column(enum_type(RunStatus), nullable=False)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    source: Mapped["Source"] = relationship("Source")
    observation: Mapped[Observation] = relationship("Observation")


class ContactHistory(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "contact_history"

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contact_points.id"), nullable=False, index=True)
    value: Mapped[str] = mapped_column(String(2048), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(2048), nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    change_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("change_events.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    contact: Mapped["ContactPoint"] = relationship("ContactPoint")
    change_event: Mapped[ChangeEvent | None] = relationship("ChangeEvent")
