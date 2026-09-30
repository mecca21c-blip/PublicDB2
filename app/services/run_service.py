"""Real CrawlRun list/detail projection without assigning runs to one binding."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Agency,
    CrawlRun,
    ExtractedContactCandidate,
    ExtractedDirectoryRecord,
    ExtractionRun,
    Observation,
    RunStatus,
    Source,
    SourceBinding,
)


STATUS_LABELS = {
    RunStatus.PENDING: ("대기", "neutral"),
    RunStatus.RUNNING: ("수집 중", "info"),
    RunStatus.SUCCESS: ("추출 성공", "success"),
    RunStatus.PARTIAL: ("부분 완료", "warning"),
    RunStatus.FAILED: ("오류", "danger"),
}
STAGE_LABELS = {
    "PENDING": ("대기", "neutral"),
    "SUCCESS": ("성공", "success"),
    "FAILED": ("오류", "danger"),
    "SKIPPED": ("미수행", "neutral"),
}


class RunService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_page(
        self,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        agency_id: uuid.UUID | None = None,
        status: RunStatus | None = None,
        search: str | None = None,
    ) -> dict[str, tuple[dict, ...]]:
        statement = (
            select(CrawlRun)
            .join(CrawlRun.source)
            .options(
                selectinload(CrawlRun.source).selectinload(Source.bindings).selectinload(SourceBinding.agency),
                selectinload(CrawlRun.source).selectinload(Source.bindings).selectinload(SourceBinding.org_unit),
                selectinload(CrawlRun.observations).selectinload(Observation.extraction_runs),
            )
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
        )
        if date_from:
            statement = statement.where(CrawlRun.started_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
        if date_to:
            statement = statement.where(CrawlRun.started_at <= datetime.combine(date_to, time.max, tzinfo=timezone.utc))
        if status:
            statement = statement.where(CrawlRun.status == status)
        if agency_id:
            statement = statement.where(
                CrawlRun.source.has(Source.bindings.any(SourceBinding.agency_id == agency_id))
            )
        term = (search or "").strip()
        if term:
            pattern = f"%{term}%"
            statement = statement.where(
                or_(
                    Source.url.ilike(pattern),
                    Source.title.ilike(pattern),
                    CrawlRun.source.has(
                        Source.bindings.any(SourceBinding.agency.has(Agency.official_name.ilike(pattern)))
                    ),
                )
            )
        runs = list(self.session.scalars(statement).unique())
        projections = [self._projection(run) for run in runs]
        return {"items": tuple(item for item, _ in projections), "details": tuple(detail for _, detail in projections)}

    def _projection(self, run: CrawlRun) -> tuple[dict, dict]:
        bindings = sorted(run.source.bindings, key=lambda item: (item.agency.official_name, item.org_unit.name if item.org_unit else ""))
        binding_labels = [
            f"{binding.agency.official_name} · {binding.org_unit.name if binding.org_unit else '기관 공통'}"
            + ("" if binding.active else " (제외)")
            for binding in bindings
        ]
        if not bindings:
            context = "연결 없음"
        elif len(bindings) == 1:
            context = binding_labels[0]
        else:
            context = f"{bindings[0].agency.official_name} 외 {len(bindings) - 1}개 연결"
        result, tone = STATUS_LABELS[run.status]
        if run.status is RunStatus.SUCCESS and run.records_observed == 0:
            result, tone = "대상 자료 없음", "info"
        duration_seconds = (
            max(0.0, (run.finished_at - run.started_at).total_seconds())
            if run.finished_at else None
        )
        observation = run.observations[0] if run.observations else None
        extraction_runs = observation.extraction_runs if observation else []
        contact_count = sum(
            extraction.candidates_found for extraction in extraction_runs
            if extraction.extractor_name == "html_contact" and extraction.status.value == "SUCCESS"
        )
        directory_count = sum(
            extraction.candidates_found for extraction in extraction_runs
            if extraction.extractor_name == "staff_directory" and extraction.status.value == "SUCCESS"
        )
        artifact_path = observation.artifact_path if observation else None
        if artifact_path and Path(artifact_path).is_absolute():
            artifact_path = None
        stage = lambda value: (*STAGE_LABELS[value.value],)
        item = {
            "id": str(run.id),
            "time": run.started_at.strftime("%Y-%m-%d %H:%M"),
            "agency": context,
            "source": run.source.title or run.source.url,
            "result": result,
            "tone": tone,
            "found": run.records_observed,
            "duration": f"{duration_seconds:.1f}초" if duration_seconds is not None else "진행 중",
        }
        stages = (
            ("접속", *stage(run.connection_status)),
            ("RAW 저장", *stage(run.raw_status)),
            ("추출", *stage(run.extraction_status)),
            ("발견 결과", f"{run.records_observed}건", "info"),
            ("확정 DB 반영", "미반영", "neutral"),
        )
        detail = {
            "id": str(run.id),
            "title": f"{context} · {run.source.title or run.source.url}",
            "reason": run.error_summary or "-",
            "stages": stages,
            "bindings": tuple(binding_labels),
            "http_status": run.http_status if run.http_status is not None else "-",
            "final_url": (observation.final_url or observation.page_url) if observation else "-",
            "content_type": observation.content_type or "-" if observation else "-",
            "response_bytes": observation.response_bytes if observation and observation.response_bytes is not None else "-",
            "artifact_path": artifact_path or "-",
            "artifact_sha256": observation.artifact_sha256 or "-" if observation else "-",
            "contact_count": contact_count,
            "directory_count": directory_count,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else "-",
            "duration": item["duration"],
        }
        return item, detail
