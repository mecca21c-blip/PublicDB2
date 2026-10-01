"""Canonical Source facet preview API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_viewer
from app.models import CollectionMethod
from app.services.source_query_service import FILTER_METHODS, SourceFilterError, SourceFilterSpec, SourceQueryService


router = APIRouter(prefix="/api/source-index", tags=["source-index"], dependencies=[Depends(require_viewer)])


def _methods(value: str | None) -> list[CollectionMethod] | None:
    if value is None:
        return list(FILTER_METHODS)
    if not value:
        return []
    try:
        return [CollectionMethod(item) for item in value.split(",") if item]
    except ValueError as error:
        raise SourceFilterError("지원하지 않는 수집 방식 필터입니다.") from error


@router.get("/preview")
def preview_source_filter(
    search: str | None = Query(default=None, max_length=100),
    region_code: list[str] = Query(default=[]),
    agency_id: uuid.UUID | None = None,
    org_unit_id: uuid.UUID | None = None,
    methods: str | None = None,
    source_status: str | None = None,
    scheduled: str = "all",
    session: Session = Depends(get_session),
) -> dict:
    try:
        spec = SourceFilterSpec.build(
            search=search, region_codes=region_code, agency_id=agency_id,
            org_unit_id=org_unit_id, methods=_methods(methods),
            status=source_status, scheduled=scheduled,
        )
        service = SourceQueryService(session)
        return {
            "total": service.count(spec),
            "eligible_total": service.count(spec, eligible_only=True),
            "method_counts": service.method_counts(spec),
            "filter": service.snapshot(spec, resolved_count=service.count(spec, eligible_only=True)),
        }
    except SourceFilterError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
