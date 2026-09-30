"""Three-way collection configuration, catalog, credentials, and preview."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_admin, require_operator, require_viewer
from app.api.schemas import CatalogActivation, CredentialCreate
from app.models import ApiAuthMode, CollectionMethod, Source, User, UserRole
from app.models import ApiSourceKind
from app.collectors.http_fetcher import HTTPFetchError
from app.services.api_credential_store import ApiCredentialStore, CredentialStoreError
from app.services.catalog_service import CatalogError, CatalogService
from app.services.collection_recovery_service import source_claim_key
from app.services.operation_claim_service import OperationClaimService
from app.services.source_method_service import MethodConfigError, SourceMethodService
from app.services.structured_extraction_service import (
    StructuredExtractionError, mapped_record, parse_structured_records,
)
import xml.etree.ElementTree as ET


router = APIRouter(prefix="/api", tags=["three-way"])


@router.get("/public-source-catalog", dependencies=[Depends(require_viewer)])
def public_source_catalog(session: Session = Depends(get_session)) -> dict:
    items = CatalogService(session).list()
    return {"items": items, "verified_count": len(items)}


@router.post("/public-source-catalog/{catalog_id}/activate", dependencies=[Depends(require_operator)])
def activate_public_source(catalog_id: str, payload: CatalogActivation, session: Session = Depends(get_session)) -> dict:
    try:
        return CatalogService(session).activate(catalog_id, **payload.model_dump())
    except (CatalogError, MethodConfigError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api-credentials", dependencies=[Depends(require_admin)], status_code=201)
def save_api_credential(payload: CredentialCreate, request: Request) -> dict:
    try:
        return ApiCredentialStore(request.app.state.runtime_paths.config_root).save(
            payload.secret_value, payload.credential_ref
        )
    except CredentialStoreError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/api-credentials/{credential_ref}", dependencies=[Depends(require_viewer)])
def api_credential_status(credential_ref: str, request: Request) -> dict:
    return ApiCredentialStore(request.app.state.runtime_paths.config_root).status(credential_ref)


@router.delete("/api-credentials/{credential_ref}", dependencies=[Depends(require_admin)])
def remove_api_credential(credential_ref: str, request: Request) -> dict:
    return {"removed": ApiCredentialStore(request.app.state.runtime_paths.config_root).remove(credential_ref)}


@router.patch("/sources/{source_id}/method", dependencies=[Depends(require_operator)])
def change_source_method(
    source_id: uuid.UUID, payload: dict, user: User = Depends(require_operator),
    session: Session = Depends(get_session),
) -> dict:
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="등록된 수집 소스를 찾을 수 없습니다.")
    claim = None
    try:
        method = CollectionMethod(payload.get("collection_method"))
        config = dict(payload.get("method_config") or {})
        auth = ApiAuthMode(config.get("auth_mode", ApiAuthMode.NONE)) if method is CollectionMethod.API else ApiAuthMode.NONE
        if auth is not ApiAuthMode.NONE and user.role is not UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="API 자격증명 설정은 관리자만 변경할 수 있습니다.")
        acquired = OperationClaimService(session).acquire(
            source_claim_key(source.id), 'METHOD_EDIT'
        )
        if not acquired.acquired:
            raise HTTPException(status_code=409, detail="수집 실행 중에는 수집 방식을 변경할 수 없습니다.")
        claim = acquired.claim
        SourceMethodService(session).configure(source, method, config, commit=False)
        session.delete(claim)
        session.commit()
        return {"source_id": str(source.id), "collection_method": source.collection_method.value}
    except HTTPException:
        session.rollback()
        raise
    except (ValueError, MethodConfigError) as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/source-config/preview", dependencies=[Depends(require_operator)])
def preview_source_config(
    payload: dict, request: Request, user: User = Depends(require_operator),
) -> dict:
    try:
        url = str(payload.get("url") or "")
        config = SourceMethodService._api(dict(payload.get("method_config") or {}))
        if config.auth_mode is not ApiAuthMode.NONE and user.role is not UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="자격증명을 사용하는 설정 미리보기는 관리자만 수행할 수 있습니다.")
        params = dict(config.static_params or {})
        headers = {}
        if config.auth_mode is not ApiAuthMode.NONE:
            secret = ApiCredentialStore(request.app.state.runtime_paths.config_root).resolve(config.credential_ref)
            if config.auth_mode is ApiAuthMode.QUERY_API_KEY:
                params[config.credential_name] = secret
            else:
                headers[config.credential_name] = secret
        fetched = request.app.state.preview_fetcher_factory().fetch(url, params=params, headers=headers)
        if config.kind in {ApiSourceKind.RSS, ApiSourceKind.ATOM}:
            root = ET.fromstring(fetched.content)
            tag = "item" if config.kind is ApiSourceKind.RSS else "entry"
            count = sum(node.tag.rsplit("}", 1)[-1] == tag for node in root.iter())
            sample = []
        else:
            records = parse_structured_records(
                fetched.content, config.response_format, config.record_path,
                declared_charset=fetched.declared_charset,
            )
            count = len(records)
            sample = [
                mapped_record(record, config.field_mapping or {}, config.response_format)
                for record in records[:5]
            ]
        return {
            "http_status": fetched.status_code, "content_type": fetched.content_type,
            "raw_count": count, "mapped_sample": sample, "mapping_errors": [],
        }
    except HTTPException:
        raise
    except (ValueError, MethodConfigError, CredentialStoreError, HTTPFetchError, StructuredExtractionError, ET.ParseError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
