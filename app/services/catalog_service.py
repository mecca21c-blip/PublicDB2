"""Read and activate immutable built-in source templates."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.catalog import BUILTIN_PUBLIC_SOURCES
from app.models import CollectionMethod
from app.services.source_service import SourceService


class CatalogError(ValueError):
    pass


class CatalogService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> tuple[dict, ...]:
        return tuple(dict(item) for item in BUILTIN_PUBLIC_SOURCES)

    def activate(self, catalog_id: str, *, agency_id: uuid.UUID, org_unit_id: uuid.UUID | None = None) -> dict:
        item = next((entry for entry in BUILTIN_PUBLIC_SOURCES if entry["catalog_id"] == catalog_id), None)
        if item is None:
            raise CatalogError("검증된 기본 공개 소스를 찾을 수 없습니다.")
        if item.get("auth_required"):
            raise CatalogError("기본 공개 소스는 무인증 항목만 활성화할 수 있습니다.")
        config = dict(item.get("config") or {})
        config.update({"catalog_id": item["catalog_id"], "catalog_version": item["catalog_version"]})
        binding, created = SourceService(self.session).register_binding(
            url=item["endpoint"], agency_id=agency_id, org_unit_id=org_unit_id,
            description=item.get("display_name"), collection_method=CollectionMethod.API,
            method_config=config,
        )
        return {"item": binding, "created": created}
