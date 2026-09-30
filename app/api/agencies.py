from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_operator, require_viewer
from app.api.schemas import AgencyCreate, AgencyUpdate, DutyCreate, OrgUnitCreate
from app.services.agency_service import AgencyService, AgencyServiceError


router = APIRouter(prefix="/api/agencies", tags=["agencies"], dependencies=[Depends(require_viewer)])


def _service(session: Session) -> AgencyService:
    return AgencyService(session)


def _failure(error: ValueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))


@router.get("")
def list_agencies(search: str | None = None, session: Session = Depends(get_session)) -> dict:
    return _service(session).list_page(search=search)


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_operator)])
def create_agency(payload: AgencyCreate, session: Session = Depends(get_session)) -> dict:
    try:
        agency, created = _service(session).create_agency(**payload.model_dump())
        return {"item": agency, "created": created}
    except AgencyServiceError as error:
        raise _failure(error) from error


@router.get("/{agency_id}")
def get_agency(agency_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    try:
        return {"item": _service(session).agency_detail(agency_id)}
    except AgencyServiceError as error:
        raise _failure(error) from error


@router.patch("/{agency_id}", dependencies=[Depends(require_operator)])
def update_agency(agency_id: uuid.UUID, payload: AgencyUpdate, session: Session = Depends(get_session)) -> dict:
    try:
        values = payload.model_dump(exclude_unset=True)
        return {"item": _service(session).update_agency(agency_id, **values)}
    except AgencyServiceError as error:
        raise _failure(error) from error


@router.get("/{agency_id}/org-units")
def list_org_units(agency_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    try:
        return {"items": _service(session).agency_detail(agency_id)["departments"]}
    except AgencyServiceError as error:
        raise _failure(error) from error


@router.post("/{agency_id}/org-units", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_operator)])
def create_org_unit(agency_id: uuid.UUID, payload: OrgUnitCreate, session: Session = Depends(get_session)) -> dict:
    try:
        return {"item": _service(session).create_org_unit(agency_id=agency_id, **payload.model_dump())}
    except AgencyServiceError as error:
        raise _failure(error) from error


@router.get("/{agency_id}/duties")
def list_duties(agency_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    try:
        return {"items": _service(session).agency_detail(agency_id)["duties"]}
    except AgencyServiceError as error:
        raise _failure(error) from error


@router.post("/{agency_id}/duties", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_operator)])
def create_duty(agency_id: uuid.UUID, payload: DutyCreate, session: Session = Depends(get_session)) -> dict:
    try:
        return {"item": _service(session).create_duty(agency_id=agency_id, **payload.model_dump())}
    except AgencyServiceError as error:
        raise _failure(error) from error

