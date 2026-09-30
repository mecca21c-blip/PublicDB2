from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.models import AgencyType, OrgUnitType, SourceCoverageMode


class AgencyCreate(BaseModel):
    official_name: str = Field(min_length=1, max_length=300)
    agency_type: AgencyType = AgencyType.OTHER
    external_identifier: str | None = None
    address: str | None = None


class AgencyUpdate(BaseModel):
    official_name: str | None = None
    agency_type: AgencyType | None = None
    external_identifier: str | None = None
    address: str | None = None


class OrgUnitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    unit_type: OrgUnitType = OrgUnitType.DEPARTMENT
    parent_org_unit_id: uuid.UUID | None = None


class DutyCreate(BaseModel):
    org_unit_id: uuid.UUID
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None


class SourceBindingCreate(BaseModel):
    agency_id: uuid.UUID
    org_unit_id: uuid.UUID | None = None
    url: str
    description: str | None = None


class SourceBindingUpdate(BaseModel):
    agency_id: uuid.UUID | None = None
    org_unit_id: uuid.UUID | None = None
    url: str | None = None
    description: str | None = None


class ExclusionRequest(BaseModel):
    reason: str | None = None


class ReviewDecision(BaseModel):
    note: str | None = None


class SourceCoverageUpdate(BaseModel):
    coverage_mode: SourceCoverageMode

