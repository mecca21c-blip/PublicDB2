"""Server-authoritative preview and registration for row-based Source intake."""

from __future__ import annotations

import difflib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Agency, CollectionMethod, OrgUnit, OrgUnitType, Source, SourceBinding
from app.services.agency_service import AgencyService, AgencyServiceError
from app.services.normalization import (
    SourceURLValidationError, collapse_whitespace, normalize_agency_name,
    normalize_org_unit_name, normalize_source_url,
)
from app.services.source_method_service import MethodConfigError, SourceMethodService
from app.services.source_service import SourceBindingConflict, SourceService, SourceServiceError


MAX_INTERACTIVE_SOURCE_ROWS = 200
MAX_SIMILAR_SUGGESTIONS = 3


class InteractiveBatchError(ValueError):
    pass


class SourceInteractiveBatchService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def preview(self, payload: Any) -> dict:
        rows = list(payload.rows)
        self._validate_batch_shape(payload.collection_method, rows)
        seen: dict[str, str] = {}
        projected: list[dict] = []
        agency_groups: dict[str, dict] = {}
        org_groups: dict[str, dict] = {}

        for line_number, row in enumerate(rows, start=1):
            result = self._preview_row(row, payload.collection_method, line_number)
            normalized = result.get("normalized_url")
            if normalized and normalized in seen:
                result.update({
                    "status": "DUPLICATE", "importable": False,
                    "message": "같은 canonical URL이 이 입력에 이미 있습니다.",
                    "duplicate_of": seen[normalized],
                })
            elif normalized:
                seen[normalized] = row.row_id
            projected.append(result)
            if result.get("agency_resolution"):
                self._merge_resolution(agency_groups, result["agency_resolution"])
            if result.get("org_resolution"):
                self._merge_resolution(org_groups, result["org_resolution"])

        summary = self._summary(projected)
        return {
            "summary": summary,
            "rows": projected,
            "agency_resolutions": list(agency_groups.values()),
            "org_resolutions": list(org_groups.values()),
        }

    def register(self, payload: Any) -> dict:
        preview = self.preview(payload)
        request_rows = {row.row_id: row for row in payload.rows}
        counts = {
            "input": len(payload.rows), "registered": 0, "created_sources": 0,
            "existing_sources_reused": 0, "created_bindings": 0,
            "created_agencies": 0, "created_org_units": 0,
            "duplicates_skipped": preview["summary"]["duplicates"],
            "errors": preview["summary"]["errors"] + preview["summary"]["unresolved"],
        }
        result_rows: list[dict] = []
        source_ids: list[str] = []

        for projected in preview["rows"]:
            if not projected["importable"]:
                result_rows.append({**projected, "result": "SKIPPED"})
                continue
            row = request_rows[projected["row_id"]]
            local = {"agency": 0, "org": 0, "source": 0, "binding": 0, "reused": 0}
            try:
                with self.session.begin_nested():
                    agency_id, agency_created = self._final_agency(row, projected)
                    org_id, org_created = self._final_org(row, projected, agency_id)
                    existing_source = self.session.scalar(select(Source).where(
                        Source.normalized_url == projected["normalized_url"]
                    ))
                    item, binding_created = SourceService(self.session).register_binding(
                        url=row.url, agency_id=agency_id, org_unit_id=org_id,
                        description=payload.description,
                        collection_method=payload.collection_method,
                        method_config=payload.method_config,
                        scheduled_refresh_enabled=payload.scheduled_refresh_enabled,
                        commit=False,
                    )
                    source = self.session.get(Source, uuid.UUID(item["source_id"]))
                    SourceMethodService(self.session).configure(
                        source, payload.collection_method, payload.method_config, commit=False,
                    )
                    source.scheduled_refresh_enabled = payload.scheduled_refresh_enabled
                    local.update({
                        "agency": int(agency_created), "org": int(org_created),
                        "source": int(existing_source is None), "reused": int(existing_source is not None),
                        "binding": int(binding_created),
                    })
                counts["registered"] += 1
                counts["created_agencies"] += local["agency"]
                counts["created_org_units"] += local["org"]
                counts["created_sources"] += local["source"]
                counts["existing_sources_reused"] += local["reused"]
                counts["created_bindings"] += local["binding"]
                if item["source_id"] not in source_ids:
                    source_ids.append(item["source_id"])
                result_rows.append({**projected, "result": "REGISTERED", "source_id": item["source_id"]})
            except (
                AgencyServiceError, SourceServiceError, SourceBindingConflict,
                SourceURLValidationError, MethodConfigError, ValueError,
            ) as error:
                counts["errors"] += 1
                result_rows.append({**projected, "result": "ERROR", "message": str(error)})
            except SQLAlchemyError:
                counts["errors"] += 1
                result_rows.append({
                    **projected, "result": "ERROR",
                    "message": "데이터베이스 충돌로 이 행을 등록하지 못했습니다.",
                })
        self.session.commit()
        return {"summary": counts, "rows": result_rows, "source_ids": source_ids}

    @staticmethod
    def _validate_batch_shape(method: CollectionMethod, rows: list) -> None:
        if not rows:
            raise InteractiveBatchError("등록할 Source 주소를 입력하세요.")
        if len(rows) > MAX_INTERACTIVE_SOURCE_ROWS:
            raise InteractiveBatchError(
                "대화형 등록은 한 번에 최대 200개까지 가능합니다. 더 큰 목록은 엑셀 업로드를 사용하세요."
            )
        if method is not CollectionMethod.WEB_PAGE and len(rows) != 1:
            raise InteractiveBatchError("Index URL과 API/RSS는 한 번에 주소 하나만 등록할 수 있습니다.")

    def _preview_row(self, row: Any, method: CollectionMethod, line_number: int) -> dict:
        base = {
            "row_id": row.row_id, "line_number": line_number, "url": row.url,
            "normalized_url": None, "agency_id": None, "agency_name": collapse_whitespace(row.agency_name),
            "org_unit_id": None, "org_unit_name": collapse_whitespace(row.org_unit_name),
            "status": "NEEDS_REVIEW", "importable": False, "message": "기관을 확인하세요.",
            "agency_resolution": None, "org_resolution": None,
        }
        try:
            normalized = normalize_source_url(row.url)
            base["normalized_url"] = normalized
        except SourceURLValidationError as error:
            base.update({"status": "ERROR", "message": str(error)})
            return base

        agency, agency_state = self._resolve_agency(row)
        if agency_state == "INVALID":
            base.update({"status": "ERROR", "message": "선택한 기관 정보가 현재 입력과 일치하지 않습니다."})
            return base
        if agency is None and row.agency_intent is None:
            name = collapse_whitespace(row.agency_name)
            if not name:
                base.update({"status": "NEEDS_REVIEW", "message": "기관명을 입력하거나 선택하세요."})
                return base
            resolution = {
                "kind": "AGENCY", "key": normalize_agency_name(name).casefold(), "name": name,
                "row_ids": [row.row_id], "similar": self._similar_agencies(name),
            }
            base.update({"agency_resolution": resolution, "message": "등록되지 않은 기관을 확인하세요."})
            return base
        if agency is not None:
            base.update({"agency_id": str(agency.id), "agency_name": agency.official_name})
        else:
            base["agency_name"] = collapse_whitespace(row.agency_intent.official_name)

        org, org_state = self._resolve_org(row, agency)
        if org_state == "INVALID":
            base.update({"status": "ERROR", "message": "선택한 부서가 현재 기관에 속하지 않습니다."})
            return base
        if org is None and row.org_unit_name and row.org_unit_intent is None:
            name = collapse_whitespace(row.org_unit_name)
            agency_key = str(agency.id) if agency else "new:" + normalize_agency_name(base["agency_name"]).casefold()
            resolution = {
                "kind": "ORG_UNIT", "key": agency_key + ":" + normalize_org_unit_name(name).casefold(),
                "name": name, "agency_name": base["agency_name"], "row_ids": [row.row_id],
                "similar": self._similar_org_units(name, agency.id) if agency else [],
            }
            base.update({"org_resolution": resolution, "message": "등록되지 않은 부서를 확인하세요."})
            return base
        if org is not None:
            base.update({"org_unit_id": str(org.id), "org_unit_name": org.name})
        elif row.org_unit_intent is not None:
            base["org_unit_name"] = collapse_whitespace(row.org_unit_intent.name)

        source = self.session.scalar(select(Source).where(Source.normalized_url == base["normalized_url"]))
        if source is not None and source.collection_method is not method:
            base.update({
                "status": "ERROR", "importable": False,
                "message": "같은 canonical URL이 다른 수집 방식으로 이미 등록되어 있습니다.",
                "source_exists": True,
            })
            return base
        binding_exists = False
        if source is not None and base["agency_id"]:
            statement = select(SourceBinding.id).where(
                SourceBinding.source_id == source.id,
                SourceBinding.agency_id == uuid.UUID(base["agency_id"]),
            )
            if base["org_unit_id"]:
                statement = statement.where(SourceBinding.org_unit_id == uuid.UUID(base["org_unit_id"]))
            else:
                statement = statement.where(SourceBinding.org_unit_id.is_(None))
            binding_exists = self.session.scalar(statement.limit(1)) is not None

        planned = (
            (row.agency_intent is not None and base["agency_id"] is None)
            or (row.org_unit_intent is not None and base["org_unit_id"] is None)
        )
        base.update({
            "status": "PLANNED_NEW" if planned else "RESOLVED", "importable": True,
            "message": "신규 기관/부서 등록 예정" if planned else "연결 확인됨",
            "agency_planned": row.agency_intent is not None and base["agency_id"] is None,
            "org_unit_planned": row.org_unit_intent is not None and base["org_unit_id"] is None,
            "source_exists": source is not None, "binding_exists": binding_exists,
            "collection_method": method.value,
        })
        return base

    def _resolve_agency(self, row: Any) -> tuple[Agency | None, str]:
        typed = normalize_agency_name(row.agency_name)
        if row.agency_id:
            agency = self.session.get(Agency, row.agency_id)
            if agency is None or not agency.active:
                return None, "INVALID"
            if typed and typed != agency.normalized_name:
                return self._exact_agency(typed), "TEXT_CHANGED"
            return agency, "EXACT"
        if typed:
            exact = self._exact_agency(typed)
            if exact:
                return exact, "EXACT"
        if row.agency_intent:
            intent_name = normalize_agency_name(row.agency_intent.official_name)
            if typed and typed != intent_name:
                return None, "INVALID"
            exact = self._exact_agency(intent_name)
            if exact and exact.agency_type != row.agency_intent.agency_type:
                return None, "INVALID"
            return exact, "EXACT" if exact else "INTENT"
        return None, "UNRESOLVED"

    def _resolve_org(self, row: Any, agency: Agency | None) -> tuple[OrgUnit | None, str]:
        typed = normalize_org_unit_name(row.org_unit_name)
        if not typed and row.org_unit_id is None and row.org_unit_intent is None:
            return None, "EMPTY"
        if row.org_unit_id:
            org = self.session.get(OrgUnit, row.org_unit_id)
            if org is None or not org.active or agency is None or org.agency_id != agency.id:
                return None, "INVALID"
            if typed and typed != org.normalized_name:
                return self._exact_org(agency.id, typed), "TEXT_CHANGED"
            return org, "EXACT"
        if agency is not None and typed:
            exact = self._exact_org(agency.id, typed)
            if exact:
                return exact, "EXACT"
        if row.org_unit_intent:
            intent_name = normalize_org_unit_name(row.org_unit_intent.name)
            if typed and typed != intent_name:
                return None, "INVALID"
            return None, "INTENT"
        return None, "UNRESOLVED"

    def _exact_agency(self, normalized: str) -> Agency | None:
        matches = list(self.session.scalars(select(Agency).where(
            Agency.normalized_name == normalized, Agency.active.is_(True),
        ).limit(2)))
        return matches[0] if len(matches) == 1 else None

    def _exact_org(self, agency_id: uuid.UUID, normalized: str) -> OrgUnit | None:
        matches = list(self.session.scalars(select(OrgUnit).where(
            OrgUnit.agency_id == agency_id, OrgUnit.normalized_name == normalized,
            OrgUnit.active.is_(True),
        ).limit(2)))
        return matches[0] if len(matches) == 1 else None

    def _similar_agencies(self, name: str) -> list[dict]:
        agencies = list(self.session.scalars(select(Agency).where(Agency.active.is_(True)).order_by(Agency.official_name).limit(500)))
        return self._similar(name, [(item.official_name, str(item.id)) for item in agencies])

    def _similar_org_units(self, name: str, agency_id: uuid.UUID) -> list[dict]:
        units = list(self.session.scalars(select(OrgUnit).where(
            OrgUnit.agency_id == agency_id, OrgUnit.active.is_(True),
        ).order_by(OrgUnit.name).limit(500)))
        return self._similar(name, [(item.name, str(item.id)) for item in units])

    @staticmethod
    def _similar(name: str, candidates: list[tuple[str, str]]) -> list[dict]:
        normalized = normalize_agency_name(name).casefold()
        scored = []
        for label, item_id in candidates:
            ratio = difflib.SequenceMatcher(None, normalized, normalize_agency_name(label).casefold()).ratio()
            if ratio >= 0.55:
                scored.append((ratio, label, item_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [{"id": item_id, "name": label} for _ratio, label, item_id in scored[:MAX_SIMILAR_SUGGESTIONS]]

    def _final_agency(self, row: Any, projected: dict) -> tuple[uuid.UUID, bool]:
        if projected.get("agency_id"):
            return uuid.UUID(projected["agency_id"]), False
        if row.agency_intent is None:
            raise InteractiveBatchError("기관 연결이 해결되지 않았습니다.")
        item, created = AgencyService(self.session).create_agency(**row.agency_intent.model_dump(), commit=False)
        return uuid.UUID(item["id"]), created

    def _final_org(self, row: Any, projected: dict, agency_id: uuid.UUID) -> tuple[uuid.UUID | None, bool]:
        if projected.get("org_unit_id"):
            org_id = uuid.UUID(projected["org_unit_id"])
            org = self.session.get(OrgUnit, org_id)
            if org is None or org.agency_id != agency_id:
                raise InteractiveBatchError("부서가 최종 기관에 속하지 않습니다.")
            return org_id, False
        if row.org_unit_intent is None:
            return None, False
        normalized = normalize_org_unit_name(row.org_unit_intent.name)
        existed = self.session.scalar(select(OrgUnit.id).where(
            OrgUnit.agency_id == agency_id, OrgUnit.normalized_name == normalized,
            OrgUnit.active.is_(True),
        )) is not None
        item = AgencyService(self.session).create_org_unit(
            agency_id=agency_id, name=row.org_unit_intent.name,
            unit_type=row.org_unit_intent.unit_type or OrgUnitType.DEPARTMENT,
            commit=False,
        )
        return uuid.UUID(item["id"]), not existed

    @staticmethod
    def _summary(rows: list[dict]) -> dict:
        agencies = {row["agency_name"] for row in rows if row.get("agency_name") and row.get("importable")}
        org_count = sum(bool(row.get("org_unit_name")) for row in rows if row.get("importable"))
        planned_agencies = {
            normalize_agency_name(row["agency_name"]).casefold()
            for row in rows if row.get("agency_planned") and row.get("agency_name")
        }
        planned_org_units = {
            (
                normalize_agency_name(row.get("agency_name", "")).casefold(),
                normalize_org_unit_name(row.get("org_unit_name", "")).casefold(),
            )
            for row in rows if row.get("org_unit_planned") and row.get("org_unit_name")
        }
        return {
            "input": len(rows), "resolved": sum(row["status"] == "RESOLVED" for row in rows),
            "planned_new": sum(row["status"] == "PLANNED_NEW" for row in rows),
            "unresolved": sum(row["status"] == "NEEDS_REVIEW" for row in rows),
            "duplicates": sum(row["status"] == "DUPLICATE" for row in rows),
            "errors": sum(row["status"] == "ERROR" for row in rows),
            "importable": sum(bool(row["importable"]) for row in rows),
            "agency_count": len(agencies), "org_connections": org_count,
            "new_agencies_planned": len(planned_agencies),
            "new_org_units_planned": len(planned_org_units),
            "agency_wide_connections": sum(not row.get("org_unit_name") for row in rows if row.get("importable")),
            "new_sources": sum(bool(row.get("importable")) and not row.get("source_exists", False) for row in rows),
            "existing_sources": sum(bool(row.get("importable")) and row.get("source_exists", False) for row in rows),
            "new_bindings": sum(bool(row.get("importable")) and not row.get("binding_exists", False) for row in rows),
            "existing_bindings": sum(bool(row.get("importable")) and row.get("binding_exists", False) for row in rows),
        }

    @staticmethod
    def _merge_resolution(groups: dict[str, dict], resolution: dict) -> None:
        existing = groups.get(resolution["key"])
        if existing is None:
            groups[resolution["key"]] = resolution
            return
        for row_id in resolution["row_ids"]:
            if row_id not in existing["row_ids"]:
                existing["row_ids"].append(row_id)
