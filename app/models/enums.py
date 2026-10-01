"""Stored domain enumerations."""

from enum import Enum


class AgencyType(str, Enum):
    CENTRAL_GOVERNMENT = "CENTRAL_GOVERNMENT"
    AGENCY = "AGENCY"
    COMMISSION = "COMMISSION"
    METROPOLITAN_GOVERNMENT = "METROPOLITAN_GOVERNMENT"
    BASIC_LOCAL_GOVERNMENT = "BASIC_LOCAL_GOVERNMENT"
    PUBLIC_INSTITUTION = "PUBLIC_INSTITUTION"
    OTHER = "OTHER"


class OrgUnitType(str, Enum):
    OFFICE = "OFFICE"
    BUREAU = "BUREAU"
    DEPARTMENT = "DEPARTMENT"
    DIVISION = "DIVISION"
    TEAM = "TEAM"
    CENTER = "CENTER"
    BRANCH = "BRANCH"
    OTHER = "OTHER"


class ContactType(str, Enum):
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    FAX = "FAX"
    WEB_FORM = "WEB_FORM"
    URL = "URL"
    ADDRESS = "ADDRESS"
    OTHER = "OTHER"


class SourceType(str, Enum):
    STAFF_DIRECTORY = "STAFF_DIRECTORY"
    ORG_CHART = "ORG_CHART"
    DEPARTMENT_PAGE = "DEPARTMENT_PAGE"
    OFFICIAL_API = "OFFICIAL_API"
    PUBLIC_DATA = "PUBLIC_DATA"
    GENERAL_PAGE = "GENERAL_PAGE"
    DOCUMENT = "DOCUMENT"
    OTHER = "OTHER"


class CollectionMethod(str, Enum):
    API = "API"
    WEB_PAGE = "WEB_PAGE"
    WEB_CRAWL = "WEB_CRAWL"
    FILE = "FILE"
    DOCUMENT = "DOCUMENT"


class ApiSourceKind(str, Enum):
    OPEN_API = "OPEN_API"
    RSS = "RSS"
    ATOM = "ATOM"


class CrawlScope(str, Enum):
    PATH_PREFIX = "PATH_PREFIX"
    SAME_DOMAIN = "SAME_DOMAIN"


class ApiPaginationMode(str, Enum):
    NONE = "NONE"
    PAGE_NUMBER = "PAGE_NUMBER"


class ApiAuthMode(str, Enum):
    NONE = "NONE"
    QUERY_API_KEY = "QUERY_API_KEY"
    HEADER_API_KEY = "HEADER_API_KEY"


class DataFormat(str, Enum):
    HTML = "HTML"
    JSON = "JSON"
    XML = "XML"
    CSV = "CSV"
    XLSX = "XLSX"
    PDF = "PDF"
    TEXT = "TEXT"
    UNKNOWN = "UNKNOWN"


class SourceCoverageMode(str, Enum):
    UNKNOWN = "UNKNOWN"
    ADDITIVE_ONLY = "ADDITIVE_ONLY"
    COMPLETE_SNAPSHOT = "COMPLETE_SNAPSHOT"


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ExtractionStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class CandidateType(str, Enum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    FAX = "FAX"


class DetectionMethod(str, Enum):
    TEXT_PATTERN = "TEXT_PATTERN"
    MAILTO = "MAILTO"
    TEL_LINK = "TEL_LINK"


class DirectoryRecordType(str, Enum):
    STAFF_DIRECTORY_ROW = "STAFF_DIRECTORY_ROW"


class EntityType(str, Enum):
    AGENCY = "AGENCY"
    ORG_UNIT = "ORG_UNIT"
    DUTY = "DUTY"
    PERSON = "PERSON"
    PERSON_ASSIGNMENT = "PERSON_ASSIGNMENT"
    CONTACT_POINT = "CONTACT_POINT"
    SOURCE = "SOURCE"
    SOURCE_BINDING = "SOURCE_BINDING"


class ReviewStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    IGNORED = "IGNORED"
    DEFERRED = "DEFERRED"


class UserRole(str, Enum):
    ADMIN = 'ADMIN'
    OPERATOR = 'OPERATOR'
    VIEWER = 'VIEWER'


class ChangeEventType(str, Enum):
    ENTITY_ADDED = "ENTITY_ADDED"
    ENTITY_CHANGED = "ENTITY_CHANGED"
    ENTITY_MISSING = "ENTITY_MISSING"
    ENTITY_RESTORED = "ENTITY_RESTORED"
    CONTACT_CHANGED = "CONTACT_CHANGED"
    CONTACT_ADDED = "CONTACT_ADDED"
    CONTACT_MISSING = "CONTACT_MISSING"
    OTHER = "OTHER"


class CollectionJobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CollectionJobItemStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class CollectionTriggerType(str, Enum):
    MANUAL_SOURCE = "MANUAL_SOURCE"
    MANUAL_SELECTION = "MANUAL_SELECTION"
    MANUAL_ORG_UNIT = "MANUAL_ORG_UNIT"
    MANUAL_AGENCY = "MANUAL_AGENCY"
    MANUAL_REGION = "MANUAL_REGION"
    MANUAL_ALL = "MANUAL_ALL"
    SCHEDULED_FULL = "SCHEDULED_FULL"
    SCHEDULED_RETRY = "SCHEDULED_RETRY"


class RefreshRecurrence(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
