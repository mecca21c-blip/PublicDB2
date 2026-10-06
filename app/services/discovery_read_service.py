"""Bounded, provenance-preserving read projection for run discoveries."""

from __future__ import annotations

import math
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.discovery_quality import (
    CONTACT_SCOPE_LABELS,
    classify_contact_scope,
    contact_semantic_key,
    meaningful_directory_values,
    preferred_contact,
)
from app.models import (
    CrawlRun,
    ExtractedContactCandidate,
    ExtractedDirectoryRecord,
    ExtractedFeedItem,
    ExtractionRun,
    ExtractionStatus,
    Observation,
)


DEFAULT_DISCOVERY_PAGE_SIZE = 50
MAX_DISCOVERY_PAGE_SIZE = 100
MAX_CONTEXT_DISPLAY_CHARS = 300
MAX_ROW_DISPLAY_CHARS = 500

_CONTACT_TYPE_LABELS = {
    "PHONE": "전화",
    "EMAIL": "이메일",
    "FAX": "팩스",
}


class RunDiscoveryNotFound(LookupError):
    pass


def _bounded(value: str | None, limit: int) -> str | None:
    text = " ".join((value or "").split()).strip()
    return text[:limit] if text else None


def _meaningful_directory(record: ExtractedDirectoryRecord) -> bool:
    return meaningful_directory_values(
        row_text=record.row_text,
        org_unit_text=record.org_unit_text,
        duty_text=record.duty_text,
        position_text=record.position_text,
        person_name_text=record.person_name_text,
        phone_text=record.phone_text,
        email_text=record.email_text,
        fax_text=record.fax_text,
    )


class DiscoveryReadService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _run(self, run_id: uuid.UUID) -> CrawlRun:
        run = self.session.get(CrawlRun, run_id)
        if run is None:
            raise RunDiscoveryNotFound("수집 실행을 찾을 수 없습니다.")
        return run

    def _contacts(self, run_id: uuid.UUID) -> list[ExtractedContactCandidate]:
        candidates = list(self.session.scalars(
            select(ExtractedContactCandidate)
            .join(ExtractionRun, ExtractionRun.id == ExtractedContactCandidate.extraction_run_id)
            .join(Observation, Observation.id == ExtractedContactCandidate.observation_id)
            .where(
                Observation.crawl_run_id == run_id,
                ExtractionRun.status == ExtractionStatus.SUCCESS,
                ExtractionRun.extractor_name == "html_contact",
            )
            .order_by(
                ExtractedContactCandidate.observation_id,
                ExtractedContactCandidate.created_at,
                ExtractedContactCandidate.id,
            )
        ))
        selected: dict[tuple[uuid.UUID, str, str], ExtractedContactCandidate] = {}
        for candidate in candidates:
            semantic = contact_semantic_key(candidate)
            key = (candidate.observation_id, *semantic)
            selected[key] = (
                candidate
                if key not in selected
                else preferred_contact(selected[key], candidate)
            )
        return [selected[key] for key in sorted(selected, key=lambda value: tuple(map(str, value)))]

    def _directories(self, run_id: uuid.UUID) -> list[ExtractedDirectoryRecord]:
        records = list(self.session.scalars(
            select(ExtractedDirectoryRecord)
            .join(ExtractionRun, ExtractionRun.id == ExtractedDirectoryRecord.extraction_run_id)
            .join(Observation, Observation.id == ExtractedDirectoryRecord.observation_id)
            .where(
                Observation.crawl_run_id == run_id,
                ExtractionRun.status == ExtractionStatus.SUCCESS,
            )
            .order_by(ExtractedDirectoryRecord.created_at, ExtractedDirectoryRecord.id)
        ))
        return [record for record in records if _meaningful_directory(record)]

    def _feed_items(self, run_id: uuid.UUID) -> list[ExtractedFeedItem]:
        return list(self.session.scalars(
            select(ExtractedFeedItem)
            .join(ExtractionRun, ExtractionRun.id == ExtractedFeedItem.extraction_run_id)
            .join(Observation, Observation.id == ExtractedFeedItem.observation_id)
            .where(
                Observation.crawl_run_id == run_id,
                ExtractionRun.status == ExtractionStatus.SUCCESS,
            )
            .order_by(ExtractedFeedItem.created_at, ExtractedFeedItem.id)
        ))

    def summary(self, run_id: uuid.UUID) -> dict[str, int]:
        self._run(run_id)
        contacts = self._contacts(run_id)
        scopes = [
            classify_contact_scope(item.source_locator, item.context_text)
            for item in contacts
        ]
        directories = self._directories(run_id)
        feeds = self._feed_items(run_id)
        business_contacts = sum(scope.value == "BUSINESS" for scope in scopes)
        site_wide_contacts = sum(scope.value == "SITE_WIDE" for scope in scopes)
        unknown_contacts = sum(scope.value == "UNKNOWN" for scope in scopes)
        return {
            "valid_contacts": len(contacts),
            "business_contacts": business_contacts,
            "site_wide_contacts": site_wide_contacts,
            "unknown_contacts": unknown_contacts,
            "directory_records": len(directories),
            "feed_items": len(feeds),
            "business_directory": business_contacts + len(directories),
            "meaningful_total": len(contacts) + len(directories) + len(feeds),
        }

    def has_meaningful_directory_records(self, extraction_run_id: uuid.UUID) -> bool:
        records = list(self.session.scalars(
            select(ExtractedDirectoryRecord)
            .where(ExtractedDirectoryRecord.extraction_run_id == extraction_run_id)
            .order_by(ExtractedDirectoryRecord.id)
        ))
        return any(_meaningful_directory(record) for record in records)

    def page(
        self,
        run_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = DEFAULT_DISCOVERY_PAGE_SIZE,
    ) -> dict:
        run = self._run(run_id)
        safe_page = max(int(page or 1), 1)
        safe_size = min(max(int(page_size or DEFAULT_DISCOVERY_PAGE_SIZE), 1), MAX_DISCOVERY_PAGE_SIZE)
        items: list[dict] = []

        for candidate in self._contacts(run_id):
            scope = classify_contact_scope(candidate.source_locator, candidate.context_text)
            items.append({
                "id": str(candidate.id),
                "kind": "CONTACT",
                "kind_label": "연락처",
                "scope": scope.value,
                "scope_label": CONTACT_SCOPE_LABELS[scope],
                "candidate_type": candidate.candidate_type.value,
                "candidate_type_label": _CONTACT_TYPE_LABELS[candidate.candidate_type.value],
                "value": candidate.raw_value,
                "normalized_value": candidate.normalized_value,
                "context": _bounded(candidate.context_text, MAX_CONTEXT_DISPLAY_CHARS),
                "source_locator": candidate.source_locator or "-",
                "observation_id": str(candidate.observation_id),
                "source_url": run.source.url,
            })

        for record in self._directories(run_id):
            items.append({
                "id": str(record.id),
                "kind": "DIRECTORY",
                "kind_label": "명부",
                "scope": "BUSINESS",
                "scope_label": CONTACT_SCOPE_LABELS[classify_contact_scope(record.source_locator, record.row_text)],
                "org_unit": record.org_unit_text,
                "duty": record.duty_text,
                "position": record.position_text,
                "person_name": record.person_name_text,
                "phone": record.phone_text,
                "email": record.email_text,
                "fax": record.fax_text,
                "row_summary": _bounded(record.row_text, MAX_ROW_DISPLAY_CHARS),
                "source_locator": record.source_locator,
                "observation_id": str(record.observation_id),
                "source_url": run.source.url,
            })

        for feed in self._feed_items(run_id):
            items.append({
                "id": str(feed.id),
                "kind": "FEED",
                "kind_label": "피드 항목",
                "scope": "BUSINESS",
                "scope_label": "업무/본문 연락처",
                "title": _bounded(feed.title, MAX_ROW_DISPLAY_CHARS),
                "link": feed.link,
                "published_at": feed.published_at,
                "row_summary": _bounded(feed.summary, MAX_ROW_DISPLAY_CHARS),
                "source_locator": feed.source_locator,
                "observation_id": str(feed.observation_id),
                "source_url": run.source.url,
            })

        items.sort(key=lambda item: (item["observation_id"], item["kind"], item["id"]))
        total = len(items)
        pages = max(1, math.ceil(total / safe_size))
        safe_page = min(safe_page, pages)
        offset = (safe_page - 1) * safe_size
        return {
            "run_id": str(run.id),
            "summary": self.summary(run_id),
            "items": items[offset : offset + safe_size],
            "pagination": {
                "page": safe_page,
                "page_size": safe_size,
                "total": total,
                "pages": pages,
                "has_previous": safe_page > 1,
                "has_next": safe_page < pages,
            },
        }
