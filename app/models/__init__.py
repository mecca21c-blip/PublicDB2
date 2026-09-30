"""PublicDB2 ORM model registry."""

from app.models.entities import Agency, ContactPoint, Duty, OrgUnit, Person, PersonAssignment
from app.models.enums import (
    ApiAuthMode, ApiPaginationMode, ApiSourceKind,
    AgencyType,
    ChangeEventType,
    CollectionMethod,
    ContactType,
    DataFormat,
    CandidateType,
    DetectionMethod,
    DirectoryRecordType,
    EntityType,
    ExtractionStatus,
    OrgUnitType,
    ReviewStatus,
    RunStatus,
    SourceCoverageMode,
    SourceType,
    StageStatus,
    UserRole, CrawlScope,
)
from app.models.evidence import (
    ChangeDetection, ChangeEvent, ContactHistory, CrawlRun, DetectedChangeCandidate,
    ExtractedContactCandidate, ExtractedDirectoryRecord, ExtractionRun,
    Observation, SourceOccurrence,
)
from app.models.source import Source, SourceBinding
from app.models.collection import SourceScrapeConfig, SourceCrawlConfig, SourceApiConfig, ExtractedFeedItem
from app.models.source_import import SourceImportLog
from app.models.operations import OperationalSettings, OperationClaim, User

__all__ = [
    "Agency", "AgencyType", "ApiAuthMode", "ApiPaginationMode", "ApiSourceKind", "ChangeDetection", "ChangeEvent", "ChangeEventType",
    "CollectionMethod", "ContactHistory", "ContactPoint", "ContactType", "CrawlRun",
    "CandidateType", "CrawlScope", "DataFormat", "DetectedChangeCandidate", "DetectionMethod", "DirectoryRecordType",
    "Duty", "EntityType", "ExtractedContactCandidate", "ExtractedDirectoryRecord", "ExtractedFeedItem",
    "ExtractionRun", "ExtractionStatus", "Observation", "OrgUnit", "OrgUnitType",
    "Person", "PersonAssignment", "ReviewStatus", "RunStatus", "Source",
    "SourceApiConfig", "SourceBinding", "SourceCrawlConfig", "SourceCoverageMode", "SourceImportLog", "SourceOccurrence", "SourceScrapeConfig", "SourceType", "StageStatus",
]

__all__ += ['OperationalSettings', 'OperationClaim', 'User', 'UserRole']
