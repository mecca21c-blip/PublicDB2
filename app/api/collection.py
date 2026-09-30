"""Explicit canonical Source collection action."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_operator
from app.services.collection_service import (
    CollectionBusyError,
    SourceNotFoundError,
    UnsupportedCollectionMethod,
)


router = APIRouter(prefix="/api/sources", tags=["collection"])


@router.post("/{source_id}/collect", dependencies=[Depends(require_operator)])
def collect_source(source_id: uuid.UUID, request: Request, session: Session = Depends(get_session)) -> dict:
    try:
        result = request.app.state.collection_service_factory(session).collect(source_id)
        return {"run": result.projection()}
    except SourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (CollectionBusyError, UnsupportedCollectionMethod) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
