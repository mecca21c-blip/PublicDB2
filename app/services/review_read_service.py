"""DetectedChangeCandidate list/detail projection."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Agency, ChangeEventType, DetectedChangeCandidate, ReviewStatus


_STATE = {
    ReviewStatus.PENDING_REVIEW: ("검토 대기", "warning"),
    ReviewStatus.DEFERRED: ("보류", "neutral"),
    ReviewStatus.APPROVED: ("반영 완료", "success"),
    ReviewStatus.REJECTED: ("유지", "info"),
    ReviewStatus.IGNORED: ("제외", "neutral"),
}


class ReviewReadService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_page(
        self, *, search: str | None = None, agency_id: uuid.UUID | None = None,
        review_status: ReviewStatus | None = None,
        change_type: ChangeEventType | None = None,
    ) -> dict:
        statement = (
            select(DetectedChangeCandidate)
            .options(
                selectinload(DetectedChangeCandidate.source),
                selectinload(DetectedChangeCandidate.observation),
            )
            .order_by(DetectedChangeCandidate.created_at.desc(), DetectedChangeCandidate.id.desc())
        )
        if agency_id:
            statement = statement.where(DetectedChangeCandidate.agency_id == agency_id)
        if review_status:
            statement = statement.where(DetectedChangeCandidate.review_status == review_status)
        if change_type:
            statement = statement.where(DetectedChangeCandidate.proposed_event_type == change_type)
        candidates = list(self.session.scalars(statement))
        agencies = {
            item.id: item for item in self.session.scalars(
                select(Agency).where(Agency.id.in_({item.agency_id for item in candidates}))
            )
        } if candidates else {}
        term = (search or "").strip().casefold()
        items, details = [], []
        for candidate in candidates:
            agency = agencies.get(candidate.agency_id)
            old = self._display(candidate.old_value)
            new = self._display(candidate.new_value)
            target = self._target(candidate)
            field = candidate.proposed_event_type.value
            haystack = " ".join((agency.official_name if agency else "", target, field, old, new, candidate.reason)).casefold()
            if term and term not in haystack:
                continue
            state, tone = _STATE[candidate.review_status]
            if not candidate.actionable and candidate.review_status in (ReviewStatus.PENDING_REVIEW, ReviewStatus.DEFERRED):
                state, tone = "확인 필요", "warning"
            row = {
                "id": str(candidate.id), "agency": agency.official_name if agency else "-",
                "target": target, "field": field, "current": old, "discovered": new,
                "detected": candidate.observation.observed_at.strftime("%Y-%m-%d %H:%M"),
                "state": state, "tone": tone,
            }
            items.append(row)
            details.append({
                **row, "title": f"{row['agency']} / {target}",
                "source": candidate.source.url, "evidence": candidate.reason,
                "blocker": candidate.blocked_reason or "-",
                "actionable": candidate.actionable,
                "can_approve": candidate.actionable and candidate.review_status in (ReviewStatus.PENDING_REVIEW, ReviewStatus.DEFERRED),
                "can_reject": candidate.review_status in (ReviewStatus.PENDING_REVIEW, ReviewStatus.DEFERRED),
                "can_defer": candidate.review_status is ReviewStatus.PENDING_REVIEW,
            })
        return {"items": tuple(items), "details": tuple(details)}

    @staticmethod
    def _display(value):
        if value is None:
            return "-"
        if isinstance(value, dict):
            return str(value.get("value") or value.get("title") or value.get("name") or value)
        return str(value)

    @staticmethod
    def _target(candidate):
        values = candidate.new_value if isinstance(candidate.new_value, dict) else {}
        return values.get("duty_title") or values.get("title") or values.get("org_unit_name") or values.get("name") or candidate.entity_type.value
