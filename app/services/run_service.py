"""Real CrawlRun list/detail projection without assigning runs to one binding."""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Agency,
    ChangeDetection,
    CrawlRun,
    ExtractionRun,
    Observation,
    OrgUnit,
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
from app.services.run_history_filter import RunHistoryFilter
from app.services.semantic_discovery_service import SemanticDiscoveryProjector
from app.core.time_presentation import format_kst_datetime


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
DETAIL_LABELS = {
    "target_url": "시작 URL",
    "start_url": "시작 URL",
    "scope": "탐색 범위",
    "crawl_scope": "탐색 범위",
    "allowed_paths": "포함 경로",
    "excluded_paths": "제외 경로",
    "max_depth": "최대 깊이",
    "max_pages": "최대 페이지",
    "request_delay_ms": "요청 간격",
    "pages_discovered": "발견 페이지",
    "pages_attempted": "시도 페이지",
    "pages_succeeded": "성공 페이지",
    "pages_failed": "실패 페이지",
    "max_depth_reached": "최대 탐색 깊이",
    "robots_crawl_delay_ms": "robots 지연",
    "effective_request_delay_ms": "실제 요청 간격",
}
DELAY_KEYS = {"request_delay_ms", "robots_crawl_delay_ms", "effective_request_delay_ms"}
DETAIL_VALUE_LABELS = {
    "PATH_PREFIX": "시작 주소의 경로 안",
    "SAME_DOMAIN": "같은 도메인 전체",
}
MAX_OBSERVATION_URLS = 20


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
        filters: RunHistoryFilter | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, tuple[dict, ...]]:
        filters = filters or RunHistoryFilter.normalize(
            date_from=date_from, date_to=date_to, agency_id=agency_id,
            status=status, search=search,
        )
        page, page_size, offset = page_values(page, page_size)
        statement = (
            select(CrawlRun)
            .join(CrawlRun.source)
            .options(
                selectinload(CrawlRun.source).selectinload(Source.bindings).selectinload(SourceBinding.agency),
                selectinload(CrawlRun.source).selectinload(Source.bindings).selectinload(SourceBinding.org_unit),
            )
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
        )
        if filters.utc_start:
            statement = statement.where(CrawlRun.started_at >= filters.utc_start)
        if filters.utc_end_exclusive:
            statement = statement.where(CrawlRun.started_at < filters.utc_end_exclusive)
        if filters.run_statuses:
            statement = statement.where(CrawlRun.status.in_(filters.run_statuses))
        if filters.agency_id:
            statement = statement.where(
                CrawlRun.source.has(Source.bindings.any(SourceBinding.agency_id == filters.agency_id))
            )
        term = filters.search
        if term:
            pattern = f"%{term}%"
            statement = statement.where(
                or_(
                    Source.url.ilike(pattern),
                    Source.title.ilike(pattern),
                    CrawlRun.source.has(
                        Source.bindings.any(or_(
                            SourceBinding.agency.has(Agency.official_name.ilike(pattern)),
                            SourceBinding.org_unit.has(OrgUnit.name.ilike(pattern)),
                        ))
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
        observation_count = self.session.scalar(
            select(func.count()).select_from(Observation).where(Observation.crawl_run_id == run.id)
        ) or 0
        observations = list(self.session.scalars(
            select(Observation)
            .where(Observation.crawl_run_id == run.id)
            .order_by(Observation.observed_at, Observation.id)
            .limit(MAX_OBSERVATION_URLS)
        ))
        observation = observations[0] if observations else None
        staff_extractions = self.session.scalars(
            select(ExtractionRun)
            .join(Observation, Observation.id == ExtractionRun.observation_id)
            .where(
                Observation.crawl_run_id == run.id,
                ExtractionRun.extractor_name == "staff_directory",
            )
            .order_by(ExtractionRun.started_at, ExtractionRun.id)
        )
        discovery_reader = SemanticDiscoveryProjector(self.session)
        discovery_summary = discovery_reader.summary(run.id)
        contact_count = discovery_summary["valid_contacts"]
        directory_count = discovery_summary["directory_records"]
        staff_extraction = next((
            extraction for extraction in staff_extractions
            if extraction.status.value == "SUCCESS"
            and discovery_reader.has_meaningful_directory_records(extraction.id)
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
            "time": format_kst_datetime(run.started_at),
            "agency": context,
            "source": run.source.title or run.source.url,
            "method": user_method_label(
                run.collection_method_snapshot or CollectionMethod.WEB_PAGE,
                run.collection_kind_snapshot,
            ),
            "result": result,
            "tone": tone,
            "found": discovery_summary["meaningful_total"],
            "duration": f"{duration_seconds:.1f}초" if duration_seconds is not None else "진행 중",
        }
        stages = (
            ("접속", *stage(run.connection_status)),
            ("RAW 저장", *stage(run.raw_status)),
            ("추출", *stage(run.extraction_status)),
            ("발견 결과", f"{discovery_summary['meaningful_total']}건", "info"),
            ("확정 DB 반영", "미반영", "neutral"),
        )
        stages = (*stages[:-1], (stages[-1][0], master["state"], master["tone"]))
        observation_urls = tuple(
            value.final_url or value.page_url
            for value in observations[:MAX_OBSERVATION_URLS]
            if value.final_url or value.page_url
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
            "recorded_count": run.records_observed,
            "discovery_summary": discovery_summary,
            "started_at": format_kst_datetime(run.started_at),
            "finished_at": format_kst_datetime(run.finished_at),
            "duration": item["duration"],
            "master": master,
            "method": item["method"],
            "method_long": user_method_label(
                run.collection_method_snapshot or CollectionMethod.WEB_PAGE,
                run.collection_kind_snapshot, long=True,
            ),
            "legacy_snapshot": run.collection_method_snapshot is None,
            "config_rows": self._detail_rows(run.collection_config_snapshot or {"target_url": run.source.url}),
            "statistic_rows": self._detail_rows(run.collection_statistics or {}),
            "observation_count": observation_count,
            "observation_urls": observation_urls,
            "observation_urls_remaining": max(0, observation_count - len(observation_urls)),
        }
        return item, detail

    @staticmethod
    def _detail_rows(values: dict) -> tuple[dict[str, str], ...]:
        rows = []
        for key, value in values.items():
            if isinstance(value, (list, tuple)):
                display = "\n".join(str(item) for item in value) or "-"
            elif isinstance(value, dict):
                display = json.dumps(value, ensure_ascii=False, sort_keys=True)
            elif value is None or value == "":
                display = "-"
            elif key in DELAY_KEYS:
                display = f"{value}ms"
            else:
                display = DETAIL_VALUE_LABELS.get(str(value), str(value))
            rows.append({"label": DETAIL_LABELS.get(key, key), "value": display})
        return tuple(rows)
