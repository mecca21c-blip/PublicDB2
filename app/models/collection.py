"""Typed per-method Source configuration and structured feed evidence."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import UTCDateTime, UUIDPrimaryKeyMixin, enum_type, utc_now
from app.models.enums import ApiAuthMode, ApiPaginationMode, ApiSourceKind, CrawlScope, DataFormat


class SourceScrapeConfig(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "source_scrape_configs"

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), unique=True, index=True)
    extract_contacts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    extract_directory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False)
    source: Mapped["Source"] = relationship("Source", back_populates="scrape_config")


class SourceCrawlConfig(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "source_crawl_configs"

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), unique=True, index=True)
    scope: Mapped[CrawlScope] = mapped_column(enum_type(CrawlScope), default=CrawlScope.PATH_PREFIX, nullable=False)
    allowed_path: Mapped[str] = mapped_column(String(2048), default="/", nullable=False)
    max_depth: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    request_delay_ms: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    extract_contacts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    extract_directory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False)
    source: Mapped["Source"] = relationship("Source", back_populates="crawl_config")


class SourceApiConfig(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "source_api_configs"

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), unique=True, index=True)
    kind: Mapped[ApiSourceKind] = mapped_column(enum_type(ApiSourceKind), nullable=False)
    response_format: Mapped[DataFormat] = mapped_column(enum_type(DataFormat), nullable=False)
    record_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    field_mapping: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    static_params: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    discovery_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pagination_mode: Mapped[ApiPaginationMode] = mapped_column(enum_type(ApiPaginationMode), default=ApiPaginationMode.NONE, nullable=False)
    page_parameter: Mapped[str | None] = mapped_column(String(100), nullable=True)
    page_size_parameter: Mapped[str | None] = mapped_column(String(100), nullable=True)
    page_size: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    start_page: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    request_delay_ms: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    auth_mode: Mapped[ApiAuthMode] = mapped_column(enum_type(ApiAuthMode), default=ApiAuthMode.NONE, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    credential_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    credential_expires_on: Mapped[date | None] = mapped_column(nullable=True)
    catalog_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    catalog_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False)
    source: Mapped["Source"] = relationship("Source", back_populates="api_config")


class ExtractedFeedItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "extracted_feed_items"

    extraction_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction_runs.id", ondelete="CASCADE"), index=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("observations.id", ondelete="CASCADE"), index=True)
    item_identity: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    published_at: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_locator: Mapped[str] = mapped_column(String(1000), nullable=False)
    structured_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    extraction_run: Mapped["ExtractionRun"] = relationship("ExtractionRun", back_populates="feed_items")
    observation: Mapped["Observation"] = relationship("Observation")
