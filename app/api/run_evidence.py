"""Authenticated bounded read API for CrawlRun discovery evidence."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_viewer
from app.models import User
from app.services.semantic_discovery_service import (
    DEFAULT_DISCOVERY_PAGE_SIZE,
    MAX_DISCOVERY_PAGE_SIZE,
    DiscoveryCategory,
    RunDiscoveryNotFound,
    SemanticDiscoveryProjector,
)


router = APIRouter(prefix="/api/runs", tags=["run-evidence"])


@router.get("/{run_id}/discoveries")
def run_discoveries(
    run_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(
        DEFAULT_DISCOVERY_PAGE_SIZE, ge=1, le=MAX_DISCOVERY_PAGE_SIZE,
    ),
    category: DiscoveryCategory = Query(DiscoveryCategory.ALL),
    _user: User = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> dict:
    try:
        return SemanticDiscoveryProjector(session).page(
            run_id,
            page=page,
            page_size=page_size,
            category=category,
        )
    except RunDiscoveryNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
