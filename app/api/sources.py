from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.schemas import ExclusionRequest, SourceBindingCreate, SourceBindingUpdate
from app.services.normalization import SourceURLValidationError
from app.services.source_service import SourceBindingConflict, SourceService, SourceServiceError
from app.services.source_import_service import MAX_IMPORT_BYTES, SourceImportError, SourceImportService


router = APIRouter(prefix="/api/source-bindings", tags=["sources"])


def _failure(error: ValueError) -> HTTPException:
    code = status.HTTP_409_CONFLICT if isinstance(error, SourceBindingConflict) else status.HTTP_400_BAD_REQUEST
    return HTTPException(status_code=code, detail=str(error))


@router.get("")
def list_bindings(
    search: str | None = None,
    agency_id: uuid.UUID | None = None,
    org_unit_id: uuid.UUID | None = None,
    source_status: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    return SourceService(session).list_page(search=search, agency_id=agency_id, org_unit_id=org_unit_id, status=source_status)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_binding(payload: SourceBindingCreate, session: Session = Depends(get_session)) -> dict:
    try:
        item, created = SourceService(session).register_binding(**payload.model_dump())
        return {"item": item, "created": created}
    except (SourceServiceError, SourceURLValidationError) as error:
        raise _failure(error) from error


@router.get("/{binding_id}")
def get_binding(binding_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    try:
        return {"item": SourceService(session).get_binding(binding_id)}
    except SourceServiceError as error:
        raise _failure(error) from error


@router.patch("/{binding_id}")
def update_binding(binding_id: uuid.UUID, payload: SourceBindingUpdate, session: Session = Depends(get_session)) -> dict:
    try:
        values = payload.model_dump(exclude_unset=True)
        return {"item": SourceService(session).update_binding(binding_id, **values)}
    except (SourceServiceError, SourceURLValidationError) as error:
        raise _failure(error) from error


@router.post("/{binding_id}/exclude")
def exclude_binding(binding_id: uuid.UUID, payload: ExclusionRequest, session: Session = Depends(get_session)) -> dict:
    try:
        return {"item": SourceService(session).exclude_binding(binding_id, payload.reason)}
    except SourceServiceError as error:
        raise _failure(error) from error


@router.post("/{binding_id}/reactivate")
def reactivate_binding(binding_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    try:
        return {"item": SourceService(session).reactivate_binding(binding_id)}
    except SourceServiceError as error:
        raise _failure(error) from error


@router.post("/imports/preview")
async def preview_import(request: Request, file: UploadFile = File(...), session: Session = Depends(get_session)) -> dict:
    content = await file.read(MAX_IMPORT_BYTES + 1)
    try:
        service = SourceImportService(session, request.app.state.runtime_paths)
        preview = service.preview(file.filename or "upload", content)
        entry = request.app.state.source_import_previews.save(file.filename or "upload", content)
        return {"token": entry.token, "filename": entry.original_filename, **preview}
    except SourceImportError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/imports/{token}/confirm")
def confirm_import(token: str, request: Request, session: Session = Depends(get_session)) -> dict:
    store = request.app.state.source_import_previews
    try:
        entry = store.get(token)
        return SourceImportService(session, request.app.state.runtime_paths).confirm(entry)
    except SourceImportError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    finally:
        store.discard(token)


@router.delete("/imports/{token}")
def discard_import(token: str, request: Request) -> dict[str, bool]:
    request.app.state.source_import_previews.discard(token)
    return {"discarded": True}


@router.get("/imports/template.csv")
def download_import_template() -> Response:
    content = "\ufeff기관명,부서명,URL,소스 설명\r\n"
    return Response(content=content.encode("utf-8"), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="publicdb2-source-import-template.csv"'})
