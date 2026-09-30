"""Collection evidence, discovery, change, and confirmed-contact history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import UTCDateTime, UUIDPrimaryKeyMixin, enum_type, utc_now
from app.models.enums import (
    CandidateType, ChangeEventType, DetectionMethod, DirectoryRecordType,
    EntityType, ExtractionStatus, ReviewStatus, RunStatus, StageStatus,
    SourceCoverageMode,
)


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
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    declared_charset: Mapped[str | None] = mapped_column(String(100), nullable=True)
    response_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    raw_text_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_payload: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    crawl_run: Mapped[CrawlRun] = relationship("CrawlRun", back_populates="observations")
    source: Mapped["Source"] = relationship("Source")
    occurrences: Mapped[list["SourceOccurrence"]] = relationship("SourceOccurrence", back_populates="observation")
    extraction_runs: Mapped[list["ExtractionRun"]] = relationship("ExtractionRun", back_populates="observation")


class ExtractionRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "extraction_runs"
    __table_args__ = (
        Index("ix_extraction_runs_identity", "observation_id", "extractor_name", "extractor_version"),
    )

    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("observations.id"), nullable=False, index=True)
    extractor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[ExtractionStatus] = mapped_column(enum_type(ExtractionStatus), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    observation: Mapped[Observation] = relationship("Observation", back_populates="extraction_runs")
    candidates: Mapped[list["ExtractedContactCandidate"]] = relationship("ExtractedContactCandidate", back_populates="extraction_run")
    directory_records: Mapped[list["ExtractedDirectoryRecord"]] = relationship("ExtractedDirectoryRecord", back_populates="extraction_run")


class ExtractedContactCandidate(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "extracted_contact_candidates"

    extraction_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction_runs.id"), nullable=False, index=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("observations.id"), nullable=False, index=True)
    candidate_type: Mapped[CandidateType] = mapped_column(enum_type(CandidateType), nullable=False, index=True)
    raw_value: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    context_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_locator: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    detection_method: Mapped[DetectionMethod] = mapped_column(enum_type(DetectionMethod), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    extraction_run: Mapped[ExtractionRun] = relationship("ExtractionRun", back_populates="candidates")
    observation: Mapped[Observation] = relationship("Observation")


class ExtractedDirectoryRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "extracted_directory_records"

    extraction_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction_runs.id"), nullable=False, index=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("observations.id"), nullable=False, index=True)
    record_type: Mapped[DirectoryRecordType] = mapped_column(enum_type(DirectoryRecordType), nullable=False, index=True)
    org_unit_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    duty_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    person_name_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    fax_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_locator: Mapped[str] = mapped_column(String(1000), nullable=False)
    structured_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    extraction_run: Mapped[ExtractionRun] = relationship("ExtractionRun", back_populates="directory_records")
    observation: Mapped[Observation] = relationship("Observation")


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
    __table_args__ = (
        Index("ix_change_detections_identity", "extraction_run_id", "agency_id", "detector_name", "detector_version"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("observations.id"), nullable=False, index=True)
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("extraction_runs.id"), nullable=True, index=True)
    agency_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agencies.id"), nullable=True, index=True)
    detector_name: Mapped[str] = mapped_column(String(100), default="source_change", nullable=False)
    detector_version: Mapped[str] = mapped_column(String(50), default="1.0", nullable=False)
    coverage_mode: Mapped[SourceCoverageMode] = mapped_column(
        enum_type(SourceCoverageMode), default=SourceCoverageMode.UNKNOWN, nullable=False
    )
    status: Mapped[RunStatus] = mapped_column(enum_type(RunStatus), nullable=False)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    source: Mapped["Source"] = relationship("Source")
    observation: Mapped[Observation] = relationship("Observation")
    extraction_run: Mapped[ExtractionRun | None] = relationship("ExtractionRun")
    candidates: Mapped[list["DetectedChangeCandidate"]] = relationship(
        "DetectedChangeCandidate", back_populates="detection_run"
    )


class DetectedChangeCandidate(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "detected_change_candidates"
    __table_args__ = (
        UniqueConstraint("detection_run_id", "candidate_key", name="uq_detected_candidate_key"),
        Index("ix_detected_candidates_review", "review_status", "actionable"),
    )

    detection_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("change_detections.id"), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("observations.id"), nullable=False, index=True)
    agency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agencies.id"), nullable=False, index=True)
    directory_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extracted_directory_records.id"), nullable=True, index=True
    )
    contact_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extracted_contact_candidates.id"), nullable=True, index=True
    )
    entity_type: Mapped[EntityType] = mapped_column(enum_type(EntityType), nullable=False, index=True)
    existing_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proposed_event_type: Mapped[ChangeEventType] = mapped_column(enum_type(ChangeEventType), nullable=False)
    old_value: Mapped[dict[str, Any] | list[Any] | str | int | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict[str, Any] | list[Any] | str | int | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[ReviewStatus] = mapped_column(
        enum_type(ReviewStatus), default=ReviewStatus.PENDING_REVIEW, nullable=False, index=True
    )
    candidate_key: Mapped[str] = mapped_column(String(500), nullable=False)
    source_locator: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    actionable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    detection_run: Mapped[ChangeDetection] = relationship("ChangeDetection", back_populates="candidates")
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
