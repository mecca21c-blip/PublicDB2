"""Authorized non-blocking collection-job API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_operator, require_viewer
from app.api.schemas import CollectionJobCreate
from app.models import User
from app.services.collection_job_service import CollectionJobError, CollectionJobService


router = APIRouter(prefix="/api/collection-jobs", tags=["collection-jobs"])


@router.get("", dependencies=[Depends(require_viewer)])
def list_jobs(
    failures_only: bool = False, limit: int = 100,
    session: Session = Depends(get_session),
) -> dict:
    return {"items": CollectionJobService(session).list_recent(failures_only=failures_only, limit=limit)}


@router.get("/{job_id}", dependencies=[Depends(require_viewer)])
def get_job(job_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    try:
        service = CollectionJobService(session)
        return {"job": service.project(service.get(job_id))}
    except CollectionJobError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_job(
    payload: CollectionJobCreate,
    request: Request,
    user: User = Depends(require_operator),
    session: Session = Depends(get_session),
) -> dict:
    try:
        service = CollectionJobService(session)
        job = service.create_manual(
            payload.trigger_type,
            requested_by_user_id=user.id if isinstance(user.id, uuid.UUID) else None,
            source_id=payload.source_id,
            source_ids=payload.source_ids,
            agency_id=payload.agency_id,
            org_unit_id=payload.org_unit_id,
            region_code=payload.region_code,
        )
        request.app.state.collection_background_runtime.notify()
        return {"job": service.project(job)}
    except CollectionJobError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
