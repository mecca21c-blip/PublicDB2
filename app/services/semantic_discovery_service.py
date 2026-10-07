"""Shared semantic discovery projection over immutable extraction evidence."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.discovery_quality import (
    CONTACT_SCOPE_LABELS, ContactScope, classify_contact_scope,
    contact_semantic_key, meaningful_directory_values, preferred_contact,
)
from app.models import (
    ContactType, CrawlRun, ExtractedContactCandidate, ExtractedDirectoryRecord,
    ExtractedFeedItem, ExtractionRun, ExtractionStatus, Observation,
)
from app.services.master_normalization import normalize_contact, normalize_text, split_values


DEFAULT_DISCOVERY_PAGE_SIZE = 30
MAX_DISCOVERY_PAGE_SIZE = 100
MAX_CONTEXT_DISPLAY_CHARS = 300
MAX_ROW_DISPLAY_CHARS = 500
_CONTACT_TYPE_LABELS = {"PHONE": "전화", "EMAIL": "이메일", "FAX": "팩스"}


class DiscoveryCategory(str, Enum):
    ALL = "ALL"
    DIRECTORY = "DIRECTORY"
    CONTACT = "CONTACT"
    SITE_WIDE = "SITE_WIDE"


class RunDiscoveryNotFound(LookupError):
    pass


def _bounded(value: str | None, limit: int) -> str | None:
    text = " ".join((value or "").split()).strip()
    return text[:limit] if text else None


def _meaningful_directory(record: ExtractedDirectoryRecord) -> bool:
    return meaningful_directory_values(
        row_text=record.row_text, org_unit_text=record.org_unit_text,
        duty_text=record.duty_text, position_text=record.position_text,
        person_name_text=record.person_name_text, phone_text=record.phone_text,
        email_text=record.email_text, fax_text=record.fax_text,
    )


def directory_contact_keys(record: ExtractedDirectoryRecord) -> frozenset[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for contact_type, raw in (
        (ContactType.PHONE, record.phone_text),
        (ContactType.EMAIL, record.email_text),
        (ContactType.FAX, record.fax_text),
    ):
        for value in split_values(raw):
            normalized = normalize_contact(contact_type, value)
            if normalized:
                keys.add((contact_type.value, normalized))
    return frozenset(keys)


def _directory_identity(record: ExtractedDirectoryRecord) -> tuple:
    return (
        record.observation_id, record.record_type.value,
        normalize_text(record.org_unit_text), normalize_text(record.duty_text),
        normalize_text(record.position_text), normalize_text(record.person_name_text),
        tuple(sorted(directory_contact_keys(record))), normalize_text(record.row_text),
    )


def _feed_identity(feed: ExtractedFeedItem) -> tuple:
    return (
        feed.observation_id, normalize_text(feed.item_identity), normalize_text(feed.link),
        normalize_text(feed.title), normalize_text(feed.published_at),
    )


@dataclass(frozen=True)
class SemanticDiscoveryProjection:
    raw_contact_count: int
    raw_directory_count: int
    raw_feed_count: int
    generic_contacts: tuple[ExtractedContactCandidate, ...]
    directories: tuple[ExtractedDirectoryRecord, ...]
    standalone_contacts: tuple[ExtractedContactCandidate, ...]
    shadowed_contacts: tuple[ExtractedContactCandidate, ...]
    feeds: tuple[ExtractedFeedItem, ...]
    supporting_counts: dict[uuid.UUID, int]

    @property
    def raw_evidence_count(self) -> int:
        return self.raw_contact_count + self.raw_directory_count + self.raw_feed_count

    @property
    def semantic_count(self) -> int:
        return len(self.directories) + len(self.standalone_contacts) + len(self.feeds)


class SemanticDiscoveryProjector:
    """Single owner for evidence consolidation, user counts, and overlap suppression."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _run(self, run_id: uuid.UUID) -> CrawlRun:
        run = self.session.get(CrawlRun, run_id)
        if run is None:
            raise RunDiscoveryNotFound("수집 실행을 찾을 수 없습니다.")
        return run

    def _raw_contacts(self, run_id: uuid.UUID) -> list[ExtractedContactCandidate]:
        result = self.session.scalars(
            select(ExtractedContactCandidate)
            .join(ExtractionRun, ExtractionRun.id == ExtractedContactCandidate.extraction_run_id)
            .join(Observation, Observation.id == ExtractedContactCandidate.observation_id)
            .where(Observation.crawl_run_id == run_id, ExtractionRun.status == ExtractionStatus.SUCCESS)
            .order_by(
                ExtractedContactCandidate.observation_id,
                ExtractedContactCandidate.created_at, ExtractedContactCandidate.id,
            )
        )
        return list(result.yield_per(500))

    def _raw_directories(self, run_id: uuid.UUID) -> list[ExtractedDirectoryRecord]:
        result = self.session.scalars(
            select(ExtractedDirectoryRecord)
            .join(ExtractionRun, ExtractionRun.id == ExtractedDirectoryRecord.extraction_run_id)
            .join(Observation, Observation.id == ExtractedDirectoryRecord.observation_id)
            .where(Observation.crawl_run_id == run_id, ExtractionRun.status == ExtractionStatus.SUCCESS)
            .order_by(
                ExtractedDirectoryRecord.observation_id,
                ExtractedDirectoryRecord.created_at, ExtractedDirectoryRecord.id,
            )
        )
        return list(result.yield_per(500))

    def _raw_feeds(self, run_id: uuid.UUID) -> list[ExtractedFeedItem]:
        result = self.session.scalars(
            select(ExtractedFeedItem)
            .join(ExtractionRun, ExtractionRun.id == ExtractedFeedItem.extraction_run_id)
            .join(Observation, Observation.id == ExtractedFeedItem.observation_id)
            .where(Observation.crawl_run_id == run_id, ExtractionRun.status == ExtractionStatus.SUCCESS)
            .order_by(ExtractedFeedItem.observation_id, ExtractedFeedItem.created_at, ExtractedFeedItem.id)
        )
        return list(result.yield_per(500))

    def project(self, run_id: uuid.UUID) -> SemanticDiscoveryProjection:
        self._run(run_id)
        raw_contacts = self._raw_contacts(run_id)
        raw_directories = self._raw_directories(run_id)
        raw_feeds = self._raw_feeds(run_id)

        contacts: dict[tuple[uuid.UUID, str, str], ExtractedContactCandidate] = {}
        for candidate in raw_contacts:
            semantic = contact_semantic_key(candidate)
            if not semantic[0] or not semantic[1]:
                continue
            key = (candidate.observation_id, *semantic)
            contacts[key] = candidate if key not in contacts else preferred_contact(contacts[key], candidate)

        directories: dict[tuple, ExtractedDirectoryRecord] = {}
        for record in raw_directories:
            if _meaningful_directory(record):
                directories.setdefault(_directory_identity(record), record)

        feeds: dict[tuple, ExtractedFeedItem] = {}
        for feed in raw_feeds:
            feeds.setdefault(_feed_identity(feed), feed)

        keys_by_observation: dict[uuid.UUID, set[tuple[str, str]]] = {}
        for record in directories.values():
            keys_by_observation.setdefault(record.observation_id, set()).update(directory_contact_keys(record))

        standalone: list[ExtractedContactCandidate] = []
        shadowed: list[ExtractedContactCandidate] = []
        for candidate in contacts.values():
            target = shadowed if contact_semantic_key(candidate) in keys_by_observation.get(candidate.observation_id, set()) else standalone
            target.append(candidate)

        shadowed_keys = {
            (candidate.observation_id, *contact_semantic_key(candidate))
            for candidate in shadowed
        }
        supporting_counts: dict[uuid.UUID, int] = {}
        for record in directories.values():
            keys = directory_contact_keys(record)
            supporting_counts[record.id] = sum(
                (record.observation_id, *contact_key) in shadowed_keys
                for contact_key in keys
            )

        order = lambda item: (str(item.observation_id), str(item.id))
        return SemanticDiscoveryProjection(
            raw_contact_count=len(raw_contacts), raw_directory_count=len(raw_directories),
            raw_feed_count=len(raw_feeds),
            generic_contacts=tuple(sorted(contacts.values(), key=order)),
            directories=tuple(sorted(directories.values(), key=order)),
            standalone_contacts=tuple(sorted(standalone, key=order)),
            shadowed_contacts=tuple(sorted(shadowed, key=order)),
            feeds=tuple(sorted(feeds.values(), key=order)), supporting_counts=supporting_counts,
        )

    @staticmethod
    def _summary(projection: SemanticDiscoveryProjection) -> dict[str, int]:
        scopes = [classify_contact_scope(item.source_locator, item.context_text) for item in projection.standalone_contacts]
        business = sum(scope is ContactScope.BUSINESS for scope in scopes)
        site_wide = sum(scope is ContactScope.SITE_WIDE for scope in scopes)
        unknown = sum(scope is ContactScope.UNKNOWN for scope in scopes)
        return {
            "raw_evidence_count": projection.raw_evidence_count,
            "generic_unique": len(projection.generic_contacts),
            "shadowed_contacts": len(projection.shadowed_contacts),
            "valid_contacts": len(projection.standalone_contacts),
            "standalone_contacts": len(projection.standalone_contacts),
            "business_contacts": business, "site_wide_contacts": site_wide,
            "unknown_contacts": unknown, "directory_records": len(projection.directories),
            "feed_items": len(projection.feeds), "business_directory": len(projection.directories),
            "meaningful_total": projection.semantic_count,
            "semantic_discovery_count": projection.semantic_count,
        }

    def summary(self, run_id: uuid.UUID) -> dict[str, int]:
        return self._summary(self.project(run_id))

    def standalone_contacts_for_observation(
        self, run_id: uuid.UUID, observation_id: uuid.UUID,
    ) -> tuple[ExtractedContactCandidate, ...]:
        return tuple(item for item in self.project(run_id).standalone_contacts if item.observation_id == observation_id)

    def has_meaningful_directory_records(self, extraction_run_id: uuid.UUID) -> bool:
        records = self.session.scalars(
            select(ExtractedDirectoryRecord)
            .where(ExtractedDirectoryRecord.extraction_run_id == extraction_run_id)
            .order_by(ExtractedDirectoryRecord.id)
        )
        return any(_meaningful_directory(record) for record in records)

    def page(
        self, run_id: uuid.UUID, *, page: int = 1,
        page_size: int = DEFAULT_DISCOVERY_PAGE_SIZE,
        category: DiscoveryCategory | str = DiscoveryCategory.ALL,
    ) -> dict:
        run = self._run(run_id)
        projection = self.project(run_id)
        safe_page = max(int(page or 1), 1)
        safe_size = min(max(int(page_size or DEFAULT_DISCOVERY_PAGE_SIZE), 1), MAX_DISCOVERY_PAGE_SIZE)
        selected_category = DiscoveryCategory(category)
        items: list[dict] = []

        for record in projection.directories:
            items.append({
                "id": str(record.id), "kind": "DIRECTORY", "kind_label": "업무/명부",
                "category": DiscoveryCategory.DIRECTORY.value,
                "scope": ContactScope.BUSINESS.value, "scope_label": "업무/명부",
                "org_unit": record.org_unit_text, "duty": record.duty_text,
                "position": record.position_text, "person_name": record.person_name_text,
                "phone": record.phone_text, "email": record.email_text, "fax": record.fax_text,
                "contact_display": " · ".join(filter(None, (record.phone_text, record.email_text, record.fax_text))),
                "row_summary": _bounded(record.row_text, MAX_ROW_DISPLAY_CHARS),
                "source_locator": record.source_locator,
                "observation_id": str(record.observation_id), "source_url": run.source.url,
                "supporting_evidence_count": 1 + projection.supporting_counts.get(record.id, 0),
            })

        for candidate in projection.standalone_contacts:
            scope = classify_contact_scope(candidate.source_locator, candidate.context_text)
            items.append({
                "id": str(candidate.id), "kind": "CONTACT", "kind_label": "단독 연락처",
                "category": DiscoveryCategory.CONTACT.value,
                "scope": scope.value, "scope_label": CONTACT_SCOPE_LABELS[scope],
                "candidate_type": candidate.candidate_type.value,
                "candidate_type_label": _CONTACT_TYPE_LABELS[candidate.candidate_type.value],
                "value": candidate.raw_value, "normalized_value": candidate.normalized_value,
                "contact_display": candidate.raw_value,
                "context": _bounded(candidate.context_text, MAX_CONTEXT_DISPLAY_CHARS),
                "source_locator": candidate.source_locator or "-",
                "observation_id": str(candidate.observation_id), "source_url": run.source.url,
                "supporting_evidence_count": 1,
            })

        for feed in projection.feeds:
            items.append({
                "id": str(feed.id), "kind": "FEED", "kind_label": "피드 항목",
                "category": "FEED", "scope": ContactScope.BUSINESS.value,
                "scope_label": "업무/본문", "title": _bounded(feed.title, MAX_ROW_DISPLAY_CHARS),
                "link": feed.link, "published_at": feed.published_at,
                "row_summary": _bounded(feed.summary, MAX_ROW_DISPLAY_CHARS),
                "source_locator": feed.source_locator,
                "observation_id": str(feed.observation_id), "source_url": run.source.url,
                "supporting_evidence_count": 1,
            })

        items.sort(key=lambda item: (item["observation_id"], item["kind"], item["id"]))
        counts = {
            DiscoveryCategory.ALL.value: len(items),
            DiscoveryCategory.DIRECTORY.value: sum(item["kind"] == "DIRECTORY" for item in items),
            DiscoveryCategory.CONTACT.value: sum(item["kind"] == "CONTACT" for item in items),
            DiscoveryCategory.SITE_WIDE.value: sum(
                item["kind"] == "CONTACT" and item["scope"] == ContactScope.SITE_WIDE.value for item in items
            ),
        }
        if selected_category is DiscoveryCategory.DIRECTORY:
            items = [item for item in items if item["kind"] == "DIRECTORY"]
        elif selected_category is DiscoveryCategory.CONTACT:
            items = [item for item in items if item["kind"] == "CONTACT"]
        elif selected_category is DiscoveryCategory.SITE_WIDE:
            items = [item for item in items if item["kind"] == "CONTACT" and item["scope"] == ContactScope.SITE_WIDE.value]

        total = len(items)
        pages = max(1, math.ceil(total / safe_size))
        safe_page = min(safe_page, pages)
        offset = (safe_page - 1) * safe_size
        return {
            "run_id": str(run.id), "summary": self._summary(projection),
            "category": selected_category.value, "category_counts": counts,
            "items": items[offset:offset + safe_size], "total": total,
            "page": safe_page, "page_size": safe_size,
            "pagination": {"page": safe_page, "page_size": safe_size, "total": total, "pages": pages,
                           "has_previous": safe_page > 1, "has_next": safe_page < pages},
        }
