"""Real CrawlRun list/detail projection without assigning runs to one binding."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Agency,
    ChangeDetection,
    CrawlRun,
    ExtractedContactCandidate,
    ExtractedDirectoryRecord,
    ExtractionRun,
    Observation,
    RunStatus,
    Source,
    SourceBinding,
    SourceOccurrence,
    CollectionMethod,
)
from app.services.source_method_service import user_method_label
from app.services.master_promotion_apply_service import MasterPromotionApplyService
from app.services.master_promotion_planner import MasterPromotionPlanner, PromotionError
from app.services.source_change_detection_service import SourceChangeDetectionService
from app.services.pagination import page_metadata, page_values


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
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, tuple[dict, ...]]:
        page, page_size, offset = page_values(page, page_size)
        statement = (
            select(CrawlRun)
            .join(CrawlRun.source)
            .options(
                selectinload(CrawlRun.source).selectinload(Source.bindings).selectinload(SourceBinding.agency),
                selectinload(CrawlRun.source).selectinload(Source.bindings).selectinload(SourceBinding.org_unit),
                selectinload(CrawlRun.observations).selectinload(Observation.extraction_runs).selectinload(ExtractionRun.candidates),
                selectinload(CrawlRun.observations).selectinload(Observation.extraction_runs).selectinload(ExtractionRun.directory_records),
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
        total_statement = statement.order_by(None).with_only_columns(CrawlRun.id).subquery()
        total = self.session.scalar(select(func.count()).select_from(total_statement)) or 0
        runs = list(self.session.scalars(statement.offset(offset).limit(page_size)).unique())
        projections = [self._projection(run) for run in runs]
        return {
            "items": tuple(item for item, _ in projections),
            "details": tuple(detail for _, detail in projections),
            "pagination": page_metadata(total, page, page_size),
        }

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
        observations = sorted(run.observations, key=lambda value: (value.observed_at, value.id))
        observation = observations[0] if observations else None
        extraction_runs = [extraction for observed in observations for extraction in observed.extraction_runs]
        contact_count = sum(
            len(extraction.candidates) for extraction in extraction_runs
            if extraction.status.value == "SUCCESS"
        )
        directory_count = sum(
            len(extraction.directory_records) for extraction in extraction_runs
            if extraction.status.value == "SUCCESS"
        )
        staff_extraction = next((
            extraction for extraction in extraction_runs
            if extraction.extractor_name == "staff_directory"
            and extraction.status.value == "SUCCESS"
            and extraction.directory_records
        ), None)
        master = {
            "available": False, "extraction_id": None, "agency_choices": (),
            "context_required": False, "action": None, "preview": None,
            "state": "미반영", "tone": "neutral",
        }
        if staff_extraction is not None:
            planner = MasterPromotionPlanner(self.session)
            detector = SourceChangeDetectionService(self.session)
            choices = planner.agency_choices(run.source_id)
            master.update({
                "available": True,
                "extraction_id": str(staff_extraction.id),
                "agency_choices": choices,
                "context_required": len(choices) != 1,
            })
            if len(choices) == 1:
                resolved_agency = uuid.UUID(choices[0]["id"])
                baseline = detector.baseline_exists(run.source_id, resolved_agency)
                current_occurrence = self.session.scalar(
                    select(SourceOccurrence.id)
                    .where(SourceOccurrence.observation_id == staff_extraction.observation_id)
                    .limit(1)
                )
                detection = self.session.scalar(
                    select(ChangeDetection)
                    .where(
                        ChangeDetection.extraction_run_id == staff_extraction.id,
                        ChangeDetection.agency_id == resolved_agency,
                        ChangeDetection.status == RunStatus.SUCCESS,
                    )
                    .limit(1)
                )
                if detection is not None:
                    unresolved = any(
                        candidate.review_status.value in ("PENDING_REVIEW", "DEFERRED")
                        for candidate in detection.candidates
                    )
                    master.update({
                        "state": "검토 필요" if unresolved else "반영 완료",
                        "tone": "warning" if unresolved else "success",
                    })
                elif current_occurrence is not None:
                    reapplied = MasterPromotionApplyService(self.session).preview(
                        staff_extraction.id, resolved_agency
                    )
                    partial = reapplied["rows_requiring_review"] > 0
                    master.update({
                        "state": "일부 반영" if partial else "반영 완료",
                        "tone": "warning" if partial else "success",
                    })
                elif not baseline:
                    try:
                        master["preview"] = MasterPromotionApplyService(self.session).preview(
                            staff_extraction.id, resolved_agency
                        )
                        master.update({"action": "promote", "state": "미반영", "tone": "neutral"})
                    except PromotionError:
                        master.update({"state": "검토 필요", "tone": "warning"})
                else:
                    master.update({"action": "detect", "state": "변경 검토 대기", "tone": "warning"})
            else:
                master.update({"state": "검토 필요", "tone": "warning"})
        artifact_path = observation.artifact_path if observation else None
        if artifact_path and Path(artifact_path).is_absolute():
            artifact_path = None
        stage = lambda value: (*STAGE_LABELS[value.value],)
        item = {
            "id": str(run.id),
            "time": run.started_at.strftime("%Y-%m-%d %H:%M"),
            "agency": context,
            "source": run.source.title or run.source.url,
            "method": user_method_label(
                run.collection_method_snapshot or CollectionMethod.WEB_PAGE,
                run.collection_kind_snapshot,
            ),
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
        stages = (*stages[:-1], (stages[-1][0], master["state"], master["tone"]))
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
            "master": master,
            "method": item["method"],
            "method_long": user_method_label(
                run.collection_method_snapshot or CollectionMethod.WEB_PAGE,
                run.collection_kind_snapshot, long=True,
            ),
            "legacy_snapshot": run.collection_method_snapshot is None,
            "config_snapshot": run.collection_config_snapshot or {"target_url": run.source.url},
            "statistics": run.collection_statistics or {},
            "observation_count": len(observations),
            "observations": tuple({
                "url": value.final_url or value.page_url,
                "artifact_path": value.artifact_path if value.artifact_path and not Path(value.artifact_path).is_absolute() else "-",
                "content_type": value.content_type or "-",
                "response_bytes": value.response_bytes if value.response_bytes is not None else "-",
            } for value in observations),
        }
        return item, detail
