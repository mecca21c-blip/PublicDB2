"""Explicit, authenticated Agency metadata discovery preview."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_operator
from app.api.schemas import SourceAgencyDiscoveryRequest
from app.collectors.http_fetcher import HTTPFetcher
from app.services.normalization import SourceURLValidationError
from app.services.source_agency_discovery_service import AgencyDiscoveryError, SourceAgencyDiscoveryService


router = APIRouter(prefix="/api/source-agency-discovery", tags=["source-agency-discovery"])


@router.post("", dependencies=[Depends(require_operator)])
def discover_source_agency(
    payload: SourceAgencyDiscoveryRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    try:
        fetcher: HTTPFetcher = request.app.state.agency_discovery_fetcher_factory()
        return SourceAgencyDiscoveryService(session, fetcher).discover(**payload.model_dump())
    except (AgencyDiscoveryError, SourceURLValidationError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
