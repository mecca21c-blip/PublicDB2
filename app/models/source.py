"""Canonical source identity and contextual business bindings."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import ActiveLifecycleMixin, UTCDateTime, UUIDPrimaryKeyMixin, enum_type, utc_now
from app.models.entities import Agency, OrgUnit
from app.models.enums import CollectionMethod, DataFormat, SourceCoverageMode, SourceType


class Source(UUIDPrimaryKeyMixin, ActiveLifecycleMixin, Base):
    __tablename__ = "sources"

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    normalized_url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_type: Mapped[SourceType] = mapped_column(enum_type(SourceType), default=SourceType.GENERAL_PAGE, nullable=False)
    collection_method: Mapped[CollectionMethod] = mapped_column(
        enum_type(CollectionMethod), default=CollectionMethod.WEB_PAGE, nullable=False, index=True
    )
    data_format: Mapped[DataFormat] = mapped_column(enum_type(DataFormat), default=DataFormat.HTML, nullable=False)
    coverage_mode: Mapped[SourceCoverageMode] = mapped_column(
        enum_type(SourceCoverageMode), default=SourceCoverageMode.UNKNOWN, nullable=False
    )
    scheduled_refresh_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    bindings: Mapped[list["SourceBinding"]] = relationship("SourceBinding", back_populates="source")
    crawl_runs: Mapped[list["CrawlRun"]] = relationship("CrawlRun", back_populates="source")
    scrape_config: Mapped["SourceScrapeConfig | None"] = relationship("SourceScrapeConfig", back_populates="source", uselist=False, cascade="all, delete-orphan")
    crawl_config: Mapped["SourceCrawlConfig | None"] = relationship("SourceCrawlConfig", back_populates="source", uselist=False, cascade="all, delete-orphan")
    api_config: Mapped["SourceApiConfig | None"] = relationship("SourceApiConfig", back_populates="source", uselist=False, cascade="all, delete-orphan")


class SourceBinding(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "source_bindings"
    __table_args__ = (UniqueConstraint("source_id", "scope_key", name="uq_source_binding_scope"),)

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    agency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agencies.id"), nullable=False, index=True)
    org_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("org_units.id"), nullable=True, index=True)
    scope_key: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    excluded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False)

    source: Mapped[Source] = relationship("Source", back_populates="bindings")
    agency: Mapped[Agency] = relationship("Agency")
    org_unit: Mapped[OrgUnit | None] = relationship("OrgUnit")
