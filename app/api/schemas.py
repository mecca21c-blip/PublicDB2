from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.models import (
    AgencyType, CollectionMethod, CollectionTriggerType, OrgUnitType,
    RefreshRecurrence, SourceCoverageMode, UserRole,
)


class AgencyCreate(BaseModel):
    official_name: str = Field(min_length=1, max_length=300)
    agency_type: AgencyType = AgencyType.OTHER
    external_identifier: str | None = None
    address: str | None = None
    region_code: str | None = None


class AgencyUpdate(BaseModel):
    official_name: str | None = None
    agency_type: AgencyType | None = None
    external_identifier: str | None = None
    address: str | None = None
    region_code: str | None = None


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
    collection_method: CollectionMethod = CollectionMethod.WEB_PAGE
    method_config: dict = Field(default_factory=dict)
    scheduled_refresh_enabled: bool = True


class SourceBindingUpdate(BaseModel):
    agency_id: uuid.UUID | None = None
    org_unit_id: uuid.UUID | None = None
    url: str | None = None
    description: str | None = None
    collection_method: CollectionMethod | None = None
    method_config: dict | None = None
    scheduled_refresh_enabled: bool | None = None


class ScrapeBatchRequest(BaseModel):
    urls: str = Field(min_length=1, max_length=500_000)
    agency_id: uuid.UUID
    org_unit_id: uuid.UUID | None = None
    binding_scope: str = "AGENCY_WIDE"
    description: str | None = None
    method_config: dict = Field(default_factory=dict)
    scheduled_refresh_enabled: bool = True


class SourceAgencyDiscoveryRequest(BaseModel):
    collection_method: CollectionMethod
    representative_url: str = Field(min_length=1, max_length=2048)
    api_kind: str | None = Field(default=None, max_length=30)
    auth_mode: str | None = Field(default=None, max_length=30)


class CredentialCreate(BaseModel):
    secret_value: str = Field(min_length=1, max_length=10_000)
    credential_ref: str | None = Field(default=None, max_length=200)


class CatalogActivation(BaseModel):
    agency_id: uuid.UUID
    org_unit_id: uuid.UUID | None = None


class ExclusionRequest(BaseModel):
    reason: str | None = None


class ReviewDecision(BaseModel):
    note: str | None = None


class SourceCoverageUpdate(BaseModel):
    coverage_mode: SourceCoverageMode


class SettingsUpdate(BaseModel):
    http_timeout_seconds: float
    max_response_bytes: int
    user_agent: str
    automatic_refresh_enabled: bool = False
    refresh_recurrence: RefreshRecurrence = RefreshRecurrence.WEEKLY
    refresh_weekday: int | None = 5
    refresh_day_of_month: int | None = None
    refresh_time_of_day: str = "02:00"
    retry_failed_next_day: bool = True


class CollectionJobCreate(BaseModel):
    trigger_type: CollectionTriggerType
    source_id: uuid.UUID | None = None
    source_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    agency_id: uuid.UUID | None = None
    org_unit_id: uuid.UUID | None = None
    region_code: str | None = None
    filter: "SourceFilterPayload | None" = None


class SourceFilterPayload(BaseModel):
    search: str = Field(default="", max_length=100)
    region_codes: list[str] = Field(default_factory=list, max_length=20)
    agency_id: uuid.UUID | None = None
    org_unit_id: uuid.UUID | None = None
    methods: list[CollectionMethod] = Field(default_factory=lambda: [
        CollectionMethod.WEB_PAGE, CollectionMethod.WEB_CRAWL, CollectionMethod.API,
    ], max_length=3)
    status: str | None = None
    scheduled: str = "all"


CollectionJobCreate.model_rebuild()


class UserCreate(BaseModel):
    username: str
    display_name: str | None = None
    password: str
    role: UserRole


class UserRoleUpdate(BaseModel):
    role: UserRole


class UserActiveUpdate(BaseModel):
    active: bool


class UserPasswordUpdate(BaseModel):
    password: str

