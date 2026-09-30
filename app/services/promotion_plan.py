"""Read-only baseline promotion plan values."""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid

from app.models import ContactType, ExtractedDirectoryRecord


@dataclass(frozen=True)
class PlannedContact:
    contact_type: ContactType
    value: str
    normalized_value: str
    existing_id: uuid.UUID | None = None


@dataclass
class PlannedRow:
    record: ExtractedDirectoryRecord
    org_name: str
    duty_title: str
    org_id: uuid.UUID | None
    duty_id: uuid.UUID | None
    contacts: list[PlannedContact] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return not self.blockers
