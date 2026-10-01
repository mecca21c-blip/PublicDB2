"""Agency, organization-unit, and duty application service."""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import Agency, AgencyType, ContactPoint, Duty, OrgUnit, OrgUnitType, SourceBinding
from app.repositories.agency_repository import AgencyRepository
from app.services.normalization import collapse_whitespace, normalize_agency_name, normalize_org_unit_name
from app.services.pagination import page_metadata, page_values
from app.services.regions import REGION_LABELS, validate_region_code


class AgencyServiceError(ValueError):
    pass


AGENCY_TYPE_LABELS = {
    AgencyType.CENTRAL_GOVERNMENT: "중앙행정기관",
    AgencyType.AGENCY: "행정기관",
    AgencyType.COMMISSION: "위원회",
    AgencyType.METROPOLITAN_GOVERNMENT: "광역자치단체",
    AgencyType.BASIC_LOCAL_GOVERNMENT: "기초자치단체",
    AgencyType.PUBLIC_INSTITUTION: "공공기관",
    AgencyType.OTHER: "기타",
}


class AgencyService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = AgencyRepository(session)

    def create_agency(
        self,
        *,
        official_name: str,
        agency_type: AgencyType,
        external_identifier: str | None = None,
        address: str | None = None,
        region_code: str | None = None,
        commit: bool = True,
    ) -> tuple[dict, bool]:
        displayed = collapse_whitespace(official_name)
        normalized = normalize_agency_name(official_name)
        if not normalized:
            raise AgencyServiceError("기관명은 필수입니다.")
        if len(displayed) > 300:
            raise AgencyServiceError("기관명은 300자를 초과할 수 없습니다.")
        external_identifier = collapse_whitespace(external_identifier or "") or None
        address = collapse_whitespace(address or "") or None
        region_code = validate_region_code(region_code)
        matches = self.repository.get_by_normalized_name(normalized)
        if matches:
            if len(matches) == 1 and matches[0].agency_type == agency_type:
                return self.agency_summary(matches[0]), False
            raise AgencyServiceError("같은 이름의 기관이 충돌하는 정보로 이미 등록되어 있습니다.")
        agency = Agency(
            official_name=displayed,
            normalized_name=normalized,
            agency_type=agency_type,
            external_identifier=external_identifier,
            address=address,
            region_code=region_code,
        )
        try:
            self.repository.add(agency)
            self.session.commit() if commit else self.session.flush()
        except Exception:
            if commit:
                self.session.rollback()
            raise
        return self.agency_summary(agency), True

    def update_agency(self, agency_id: uuid.UUID, **values: object) -> dict:
        agency = self.repository.get(agency_id)
        if agency is None:
            raise AgencyServiceError("기관을 찾을 수 없습니다.")
        if "official_name" in values and values["official_name"] is not None:
            name = collapse_whitespace(str(values["official_name"]))
            if not name:
                raise AgencyServiceError("기관명은 필수입니다.")
            conflicts = [item for item in self.repository.get_by_normalized_name(normalize_agency_name(name)) if item.id != agency.id]
            if conflicts:
                raise AgencyServiceError("같은 이름의 기관이 이미 등록되어 있습니다.")
            agency.official_name = name
            agency.normalized_name = normalize_agency_name(name)
        if values.get("agency_type") is not None:
            agency.agency_type = values["agency_type"]
        for field in ("external_identifier", "address"):
            if field in values:
                cleaned = collapse_whitespace(str(values[field] or "")) or None
                setattr(agency, field, cleaned)
        if "region_code" in values:
            agency.region_code = validate_region_code(values["region_code"])
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return self.agency_summary(agency)

    def create_org_unit(
        self,
        *,
        agency_id: uuid.UUID,
        name: str,
        unit_type: OrgUnitType,
        parent_org_unit_id: uuid.UUID | None = None,
        commit: bool = True,
    ) -> dict:
        agency = self.repository.get(agency_id)
        if agency is None:
            raise AgencyServiceError("기관을 찾을 수 없습니다.")
        normalized = normalize_org_unit_name(name)
        if not normalized:
            raise AgencyServiceError("부서명은 필수입니다.")
        if parent_org_unit_id:
            parent = self.session.get(OrgUnit, parent_org_unit_id)
            if parent is None or parent.agency_id != agency_id:
                raise AgencyServiceError("상위 부서는 같은 기관에 속해야 합니다.")
        existing = self.session.scalar(
            select(OrgUnit).where(
                OrgUnit.agency_id == agency_id,
                OrgUnit.parent_org_unit_id == parent_org_unit_id,
                OrgUnit.normalized_name == normalized,
                OrgUnit.active.is_(True),
            )
        )
        if existing:
            return self.org_unit_projection(existing)
        unit = OrgUnit(
            agency_id=agency_id,
            parent_org_unit_id=parent_org_unit_id,
            name=collapse_whitespace(name),
            normalized_name=normalized,
            unit_type=unit_type,
        )
        try:
            self.session.add(unit)
            self.session.commit() if commit else self.session.flush()
        except Exception:
            if commit:
                self.session.rollback()
            raise
        return self.org_unit_projection(unit)

    def create_duty(self, *, agency_id: uuid.UUID, org_unit_id: uuid.UUID, title: str, description: str | None = None) -> dict:
        unit = self.session.get(OrgUnit, org_unit_id)
        if unit is None or unit.agency_id != agency_id:
            raise AgencyServiceError("업무 부서는 지정한 기관에 속해야 합니다.")
        cleaned_title = collapse_whitespace(title)
        if not cleaned_title:
            raise AgencyServiceError("업무명은 필수입니다.")
        duty = Duty(agency_id=agency_id, org_unit_id=org_unit_id, title=cleaned_title, description=collapse_whitespace(description or "") or None)
        try:
            self.session.add(duty)
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return {"id": str(duty.id), "org_unit_id": str(duty.org_unit_id), "title": duty.title, "description": duty.description}

    def list_page(
        self, *, search: str | None = None, agency_type: AgencyType | None = None,
        region_code: str | None = None,
        page: int = 1, page_size: int = 100,
    ) -> dict:
        page, page_size, offset = page_values(page, page_size)
        total = self.repository.count(search=search, agency_type=agency_type, region_code=region_code)
        agencies = self.repository.list(
            search=search, agency_type=agency_type, region_code=region_code, offset=offset, limit=page_size
        )
        ids = [agency.id for agency in agencies]
        unit_counts = {key: count for key, count in self.session.execute(
            select(OrgUnit.agency_id, func.count()).where(
                OrgUnit.agency_id.in_(ids), OrgUnit.active.is_(True)
            ).group_by(OrgUnit.agency_id)
        )} if ids else {}
        contact_counts = {key: count for key, count in self.session.execute(
            select(ContactPoint.agency_id, func.count()).where(
                ContactPoint.agency_id.in_(ids), ContactPoint.active.is_(True)
            ).group_by(ContactPoint.agency_id)
        )} if ids else {}
        source_counts = {key: count for key, count in self.session.execute(
            select(SourceBinding.agency_id, func.count()).where(
                SourceBinding.agency_id.in_(ids)
            ).group_by(SourceBinding.agency_id)
        )} if ids else {}
        units = defaultdict(list)
        duties = defaultdict(list)
        bindings = defaultdict(list)
        if ids:
            for unit in self.session.scalars(select(OrgUnit).where(
                OrgUnit.agency_id.in_(ids), OrgUnit.active.is_(True)
            ).order_by(OrgUnit.name)):
                units[unit.agency_id].append(unit)
            for duty in self.session.scalars(select(Duty).where(
                Duty.agency_id.in_(ids), Duty.active.is_(True)
            ).order_by(Duty.title)):
                duties[duty.agency_id].append(duty)
            for binding in self.session.scalars(
                select(SourceBinding).options(joinedload(SourceBinding.source))
                .where(SourceBinding.agency_id.in_(ids)).order_by(SourceBinding.created_at)
            ):
                bindings[binding.agency_id].append(binding)
        items = []
        details = []
        for agency in agencies:
            items.append({
                "id": str(agency.id), "name": agency.official_name,
                "type": AGENCY_TYPE_LABELS[agency.agency_type],
                "agency_type": agency.agency_type.value,
                "region_code": agency.region_code,
                "region": REGION_LABELS.get(agency.region_code, "미지정"),
                "departments": unit_counts.get(agency.id, 0),
                "contacts": contact_counts.get(agency.id, 0),
                "sources": source_counts.get(agency.id, 0),
                "status": "운영" if agency.active else "비활성",
                "tone": "success" if agency.active else "neutral",
            })
            details.append({
                "id": str(agency.id), "name": agency.official_name,
                "type": AGENCY_TYPE_LABELS[agency.agency_type],
                "identifier": agency.external_identifier or "-",
                "address": agency.address or "-",
                "agency_type": agency.agency_type.value,
                "region_code": agency.region_code,
                "region": REGION_LABELS.get(agency.region_code, "미지정"),
                "departments": tuple(self.org_unit_projection(unit) for unit in units[agency.id]),
                "duties": tuple({
                    "id": str(duty.id),
                    "org_unit_id": str(duty.org_unit_id) if duty.org_unit_id else None,
                    "title": duty.title, "description": duty.description,
                } for duty in duties[agency.id]),
                "sources": tuple({
                    "binding_id": str(binding.id), "url": binding.source.url,
                    "description": binding.description, "active": binding.active,
                } for binding in bindings[agency.id]),
            })
        return {
            "items": tuple(items),
            "details": tuple(details),
            "pagination": page_metadata(total, page, page_size),
        }

    def agency_summary(self, agency: Agency) -> dict:
        unit_count = self.session.scalar(select(func.count()).select_from(OrgUnit).where(OrgUnit.agency_id == agency.id, OrgUnit.active.is_(True))) or 0
        contact_count = self.session.scalar(select(func.count()).select_from(ContactPoint).where(ContactPoint.agency_id == agency.id, ContactPoint.active.is_(True))) or 0
        source_count = self.session.scalar(select(func.count()).select_from(SourceBinding).where(SourceBinding.agency_id == agency.id)) or 0
        return {
            "id": str(agency.id),
            "name": agency.official_name,
            "type": AGENCY_TYPE_LABELS[agency.agency_type],
            "agency_type": agency.agency_type.value,
            "region_code": agency.region_code,
            "region": REGION_LABELS.get(agency.region_code, "미지정"),
            "departments": unit_count,
            "contacts": contact_count,
            "sources": source_count,
            "status": "운영" if agency.active else "비활성",
            "tone": "success" if agency.active else "neutral",
        }

    def agency_detail(self, agency_id: uuid.UUID) -> dict:
        agency = self.repository.get(agency_id)
        if agency is None:
            raise AgencyServiceError("기관을 찾을 수 없습니다.")
        units = list(self.session.scalars(select(OrgUnit).where(OrgUnit.agency_id == agency.id, OrgUnit.active.is_(True)).order_by(OrgUnit.name)))
        duties = list(self.session.scalars(select(Duty).where(Duty.agency_id == agency.id, Duty.active.is_(True)).order_by(Duty.title)))
        bindings = list(self.session.scalars(select(SourceBinding).where(SourceBinding.agency_id == agency.id).order_by(SourceBinding.created_at)))
        return {
            "id": str(agency.id),
            "name": agency.official_name,
            "type": AGENCY_TYPE_LABELS[agency.agency_type],
            "identifier": agency.external_identifier or "-",
            "address": agency.address or "-",
            "agency_type": agency.agency_type.value,
            "region_code": agency.region_code,
            "region": REGION_LABELS.get(agency.region_code, "미지정"),
            "departments": tuple(self.org_unit_projection(unit) for unit in units),
            "duties": tuple({"id": str(duty.id), "org_unit_id": str(duty.org_unit_id) if duty.org_unit_id else None, "title": duty.title, "description": duty.description} for duty in duties),
            "sources": tuple({"binding_id": str(binding.id), "url": binding.source.url, "description": binding.description, "active": binding.active} for binding in bindings),
        }

    @staticmethod
    def org_unit_projection(unit: OrgUnit) -> dict:
        return {"id": str(unit.id), "agency_id": str(unit.agency_id), "parent_org_unit_id": str(unit.parent_org_unit_id) if unit.parent_org_unit_id else None, "name": unit.name, "unit_type": unit.unit_type.value}
