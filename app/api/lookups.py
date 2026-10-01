"""Authenticated, bounded selector lookup endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_viewer
from app.services.lookup_service import LookupError, LookupService


router = APIRouter(prefix="/api/lookups", tags=["lookups"], dependencies=[Depends(require_viewer)])


@router.get("/agencies")
def lookup_agencies(
    q: str | None = Query(default=None, max_length=100),
    region_code: str | None = None,
    limit: int = Query(default=20, ge=1, le=30),
    session: Session = Depends(get_session),
) -> dict:
    try:
        return {"items": LookupService(session).agencies(q=q, region_code=region_code, limit=limit)}
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.get("/agencies/{agency_id}/org-units")
def lookup_org_units(
    agency_id: uuid.UUID,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=20, ge=1, le=30),
    session: Session = Depends(get_session),
) -> dict:
    try:
        return {"items": LookupService(session).org_units(agency_id=agency_id, q=q, limit=limit)}
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
