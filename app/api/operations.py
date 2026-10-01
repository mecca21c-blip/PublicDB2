'''Authenticated operational settings, users, and exports.'''
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_admin, require_operator
from app.api.schemas import SettingsUpdate, UserActiveUpdate, UserCreate, UserPasswordUpdate, UserRoleUpdate
from app.models import ContactType, UserRole
from app.services.contact_export_service import ContactExportService
from app.services.settings_service import SettingsService, SettingsServiceError
from app.services.user_service import UserService, UserServiceError

router = APIRouter(prefix='/api', tags=['operations'])
logger = logging.getLogger('publicdb2')


def failure(error: ValueError):
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))


def _settings_projection(value, next_run=None):
    return {
        'http_timeout_seconds': value.http_timeout_seconds,
        'max_response_bytes': value.max_response_bytes,
        'user_agent': value.user_agent,
        'automatic_refresh_enabled': value.automatic_refresh_enabled,
        'refresh_recurrence': value.refresh_recurrence.value,
        'refresh_weekday': value.refresh_weekday,
        'refresh_day_of_month': value.refresh_day_of_month,
        'refresh_time_of_day': value.refresh_time_of_day,
        'retry_failed_next_day': value.retry_failed_next_day,
        'next_run': next_run.isoformat() if next_run else None,
    }


@router.get('/settings', dependencies=[Depends(require_admin)])
def get_settings(request: Request, session: Session = Depends(get_session)):
    value = SettingsService(session).snapshot()
    return _settings_projection(value, request.app.state.collection_scheduler.next_run())


@router.put('/settings', dependencies=[Depends(require_admin)])
def update_settings(payload: SettingsUpdate, request: Request, session: Session = Depends(get_session)):
    try:
        value = SettingsService(session).update(**payload.model_dump())
        logger.info('operational settings updated')
        request.app.state.collection_background_runtime.notify()
        return _settings_projection(value, request.app.state.collection_scheduler.next_run())
    except SettingsServiceError as error:
        raise failure(error) from error


@router.get('/users', dependencies=[Depends(require_admin)])
def list_users(session: Session = Depends(get_session)):
    return {'items': UserService(session).list_users()}


@router.post('/users', status_code=201, dependencies=[Depends(require_admin)])
def create_user(payload: UserCreate, session: Session = Depends(get_session)):
    try:
        return {'item': UserService.project(UserService(session).create(**payload.model_dump()))}
    except UserServiceError as error:
        raise failure(error) from error


@router.patch('/users/{user_id}/role', dependencies=[Depends(require_admin)])
def change_role(user_id: uuid.UUID, payload: UserRoleUpdate, session: Session = Depends(get_session)):
    try:
        service = UserService(session)
        return {'item': service.project(service.set_role(user_id, payload.role))}
    except UserServiceError as error:
        raise failure(error) from error


@router.patch('/users/{user_id}/active', dependencies=[Depends(require_admin)])
def change_active(user_id: uuid.UUID, payload: UserActiveUpdate, session: Session = Depends(get_session)):
    try:
        service = UserService(session)
        return {'item': service.project(service.set_active(user_id, payload.active))}
    except UserServiceError as error:
        raise failure(error) from error


@router.post('/users/{user_id}/password', dependencies=[Depends(require_admin)])
def reset_password(user_id: uuid.UUID, payload: UserPasswordUpdate, session: Session = Depends(get_session)):
    try:
        service = UserService(session)
        return {'item': service.project(service.set_password(user_id, payload.password))}
    except UserServiceError as error:
        raise failure(error) from error


@router.post('/contacts/export', dependencies=[Depends(require_operator)])
def export_contacts(
    request: Request, search: str | None = None,
    agency_id: uuid.UUID | None = None, org_unit_id: uuid.UUID | None = None,
    contact_type: ContactType | None = Query(default=None), session: Session = Depends(get_session),
):
    path = ContactExportService(session, export_root=request.app.state.runtime_paths.export_root).export(
        search=search, agency_id=agency_id, org_unit_id=org_unit_id, contact_type=contact_type
    )
    logger.info('confirmed contact export created: %s', path.name)
    return FileResponse(path, filename=path.name, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
