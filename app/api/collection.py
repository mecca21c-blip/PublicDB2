"""Explicit canonical Source collection action."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_operator
from app.models import CollectionTriggerType, Source, User
from app.services.collection_job_service import CollectionJobError, CollectionJobService
router = APIRouter(prefix="/api/sources", tags=["collection"])


@router.post("/{source_id}/collect", status_code=status.HTTP_202_ACCEPTED)
def collect_source(
    source_id: uuid.UUID, request: Request,
    user: User = Depends(require_operator), session: Session = Depends(get_session),
) -> dict:
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection source was not found.")
    try:
        service = CollectionJobService(session)
        job = service.create_manual(
            CollectionTriggerType.MANUAL_SOURCE,
            requested_by_user_id=user.id if isinstance(user.id, uuid.UUID) else None,
            source_id=source_id,
        )
        request.app.state.collection_background_runtime.notify()
        return {"job": service.summary_projection(job)}
    except CollectionJobError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
