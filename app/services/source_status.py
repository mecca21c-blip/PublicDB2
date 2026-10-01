"""Single operational-status contract for Source display and filtering."""

from __future__ import annotations

from sqlalchemy import and_, exists, select

from app.models import CrawlRun, RunStatus, StageStatus


def classify_source_status(binding_active: bool, run: CrawlRun | None) -> tuple[str, str, str]:
    if not binding_active:
        return "excluded", "제외", "warning"
    if run is None:
        return "unchecked", "미확인", "neutral"
    if run.status == RunStatus.SUCCESS and run.records_observed == 0 and all(
        value == StageStatus.SUCCESS for value in (run.connection_status, run.raw_status, run.extraction_status)
    ):
        return "empty", "자료없음", "info"
    if run.status == RunStatus.SUCCESS:
        return "success", "정상", "success"
    return "error", "오류", "danger"


def latest_terminal_run_id(source_id_column, correlate_from):
    return (
        select(CrawlRun.id)
        .where(
            CrawlRun.source_id == source_id_column,
            CrawlRun.status.in_((RunStatus.SUCCESS, RunStatus.PARTIAL, RunStatus.FAILED)),
        )
        .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
        .limit(1).correlate(correlate_from).scalar_subquery()
    )


def source_run_status_condition(status: str, source_id_column, correlate_from):
    latest_id = latest_terminal_run_id(source_id_column, correlate_from)
    if status == "unchecked":
        return latest_id.is_(None)
    empty_result = and_(
        CrawlRun.status == RunStatus.SUCCESS,
        CrawlRun.records_observed == 0,
        CrawlRun.connection_status == StageStatus.SUCCESS,
        CrawlRun.raw_status == StageStatus.SUCCESS,
        CrawlRun.extraction_status == StageStatus.SUCCESS,
    )
    run_filters = [CrawlRun.id == latest_id]
    if status == "error":
        run_filters.append(CrawlRun.status.in_((RunStatus.PARTIAL, RunStatus.FAILED)))
    elif status == "empty":
        run_filters.append(empty_result)
    elif status == "success":
        run_filters.extend((CrawlRun.status == RunStatus.SUCCESS, ~empty_result))
    else:
        raise ValueError(f"Unsupported operational status: {status}")
    return exists(select(CrawlRun.id).where(*run_filters))
