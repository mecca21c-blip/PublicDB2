"""Agency, organization, people, duty, and confirmed-contact entities."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.common import ActiveLifecycleMixin, UTCDateTime, UUIDPrimaryKeyMixin, enum_type
from app.models.enums import AgencyType, ContactType, OrgUnitType


class Agency(UUIDPrimaryKeyMixin, ActiveLifecycleMixin, Base):
    __tablename__ = "agencies"

    official_name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    agency_type: Mapped[AgencyType] = mapped_column(enum_type(AgencyType), nullable=False)
    external_identifier: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    homepage_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    region_code: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    parent_agency_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agencies.id"), nullable=True, index=True)

    parent: Mapped[Agency | None] = relationship("Agency", remote_side="Agency.id", foreign_keys=[parent_agency_id])
    org_units: Mapped[list["OrgUnit"]] = relationship("OrgUnit", back_populates="agency")


class OrgUnit(UUIDPrimaryKeyMixin, ActiveLifecycleMixin, Base):
    __tablename__ = "org_units"

    agency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agencies.id"), nullable=False, index=True)
    parent_org_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("org_units.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    unit_type: Mapped[OrgUnitType] = mapped_column(enum_type(OrgUnitType), nullable=False)

    agency: Mapped[Agency] = relationship("Agency", back_populates="org_units")
    parent: Mapped[OrgUnit | None] = relationship("OrgUnit", remote_side="OrgUnit.id", foreign_keys=[parent_org_unit_id])
    duties: Mapped[list["Duty"]] = relationship("Duty", back_populates="org_unit")


class Duty(UUIDPrimaryKeyMixin, ActiveLifecycleMixin, Base):
    __tablename__ = "duties"

    agency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agencies.id"), nullable=False, index=True)
    org_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("org_units.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    agency: Mapped[Agency] = relationship("Agency")
    org_unit: Mapped[OrgUnit | None] = relationship("OrgUnit", back_populates="duties")


class Person(UUIDPrimaryKeyMixin, ActiveLifecycleMixin, Base):
    __tablename__ = "people"

    agency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agencies.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    agency: Mapped[Agency] = relationship("Agency")
    assignments: Mapped[list["PersonAssignment"]] = relationship("PersonAssignment", back_populates="person")


class PersonAssignment(UUIDPrimaryKeyMixin, ActiveLifecycleMixin, Base):
    __tablename__ = "person_assignments"

    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id"), nullable=False, index=True)
    agency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agencies.id"), nullable=False, index=True)
    org_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("org_units.id"), nullable=True, index=True)
    position_title: Mapped[str] = mapped_column(String(300), nullable=False)
    role_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    person: Mapped[Person] = relationship("Person", back_populates="assignments")
    agency: Mapped[Agency] = relationship("Agency")
    org_unit: Mapped[OrgUnit | None] = relationship("OrgUnit")


class ContactPoint(UUIDPrimaryKeyMixin, ActiveLifecycleMixin, Base):
    __tablename__ = "contact_points"

    agency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agencies.id"), nullable=False, index=True)
    org_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("org_units.id"), nullable=True, index=True)
    person_assignment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person_assignments.id"), nullable=True, index=True)
    duty_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("duties.id"), nullable=True, index=True)
    contact_type: Mapped[ContactType] = mapped_column(enum_type(ContactType), nullable=False, index=True)
    value: Mapped[str] = mapped_column(String(2048), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    purpose_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    agency: Mapped[Agency] = relationship("Agency")
    org_unit: Mapped[OrgUnit | None] = relationship("OrgUnit")
    person_assignment: Mapped[PersonAssignment | None] = relationship("PersonAssignment")
    duty: Mapped[Duty | None] = relationship("Duty")
