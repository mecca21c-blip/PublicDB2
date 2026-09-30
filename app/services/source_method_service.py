"""Validation and ownership for one current collection method per Source."""

from __future__ import annotations

import re
import uuid
from datetime import date
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ApiAuthMode, ApiPaginationMode, ApiSourceKind, CollectionMethod, CrawlRun,
    CrawlScope, DataFormat, RunStatus, Source, SourceApiConfig, SourceCrawlConfig,
    SourceScrapeConfig,
)
from app.services.normalization import normalize_source_url


MAX_CRAWL_DEPTH = 5
MAX_CRAWL_PAGES = 200
MIN_REQUEST_DELAY_MS = 500
MAX_API_PAGES = 100
MAX_API_PAGE_SIZE = 1000
SEMANTIC_FIELDS = {"org_unit", "duty", "position", "person_name", "phone", "email", "fax", "record_url"}
SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,99}$")
SECRET_PARAMETER_NAMES = {
    "servicekey", "service_key", "apikey", "api_key", "accesskey", "access_key",
    "token", "access_token", "secret", "password", "authorization",
}

METHOD_LABELS = {
    CollectionMethod.WEB_PAGE: "스크래핑",
    CollectionMethod.WEB_CRAWL: "크롤링",
    CollectionMethod.API: "OpenAPI",
}


class MethodConfigError(ValueError):
    pass


def user_method_label(method: CollectionMethod, kind: ApiSourceKind | str | None = None, *, long: bool = False) -> str:
    if method is CollectionMethod.WEB_PAGE:
        return "개별 URL · 스크래핑" if long else "스크래핑"
    if method is CollectionMethod.WEB_CRAWL:
        return "Index URL · 크롤링" if long else "크롤링"
    value = kind.value if isinstance(kind, ApiSourceKind) else kind
    if value == ApiSourceKind.RSS.value:
        return "RSS"
    if value == ApiSourceKind.ATOM.value:
        return "Atom"
    return "공개 API / RSS" if long else "OpenAPI"


class SourceMethodService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_default(self, source: Source) -> None:
        if source.collection_method is CollectionMethod.WEB_PAGE and source.scrape_config is None:
            source.scrape_config = SourceScrapeConfig(extract_contacts=True, extract_directory=True)

    def configure(
        self,
        source: Source,
        method: CollectionMethod | str,
        config: dict | None = None,
        *,
        commit: bool = True,
    ) -> Source:
        selected = CollectionMethod(method)
        if selected not in {CollectionMethod.WEB_PAGE, CollectionMethod.WEB_CRAWL, CollectionMethod.API}:
            raise MethodConfigError("지원하는 수집 방식을 선택하세요.")
        running = self.session.scalar(
            select(CrawlRun.id).where(CrawlRun.source_id == source.id, CrawlRun.status == RunStatus.RUNNING).limit(1)
        )
        if running is not None and source.collection_method is not selected:
            raise MethodConfigError("수집 실행 중에는 수집 방식을 변경할 수 없습니다.")
        values = dict(config or {})
        if selected is CollectionMethod.WEB_PAGE:
            configured = self._scrape(values)
            if source.scrape_config is not None:
                source.scrape_config.extract_contacts = configured.extract_contacts
                source.scrape_config.extract_directory = configured.extract_directory
            else:
                source.scrape_config = configured
            source.crawl_config = None
            source.api_config = None
            source.data_format = DataFormat.HTML
        elif selected is CollectionMethod.WEB_CRAWL:
            configured = self._crawl(source.url, values)
            existing = source.crawl_config
            if existing is not None:
                for name in (
                    "scope", "allowed_path", "max_depth", "max_pages", "request_delay_ms",
                    "extract_contacts", "extract_directory",
                ):
                    setattr(existing, name, getattr(configured, name))
                configured = existing
            source.scrape_config = None
            source.crawl_config = configured
            source.api_config = None
            source.data_format = DataFormat.HTML
        else:
            configured = self._api(values)
            query_names = {key.casefold() for key, _value in parse_qsl(urlsplit(source.url).query)}
            if query_names & SECRET_PARAMETER_NAMES:
                raise MethodConfigError("API secret parameter는 Source URL에 저장할 수 없습니다.")
            if (
                configured.auth_mode is ApiAuthMode.QUERY_API_KEY
                and configured.credential_name
                and configured.credential_name in {key for key, _value in parse_qsl(urlsplit(source.url).query)}
            ):
                raise MethodConfigError("API Key 값은 Source URL query에 저장할 수 없습니다.")
            existing = source.api_config
            if existing is not None:
                for name in (
                    "kind", "response_format", "record_path", "field_mapping", "static_params",
                    "discovery_only", "pagination_mode", "page_parameter", "page_size_parameter",
                    "page_size", "start_page", "max_pages", "request_delay_ms", "auth_mode",
                    "credential_ref", "credential_name", "credential_expires_on", "catalog_id",
                    "catalog_version",
                ):
                    setattr(existing, name, getattr(configured, name))
                configured = existing
            source.scrape_config = None
            source.crawl_config = None
            source.api_config = configured
            source.data_format = configured.response_format
        source.collection_method = selected
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return source

    @staticmethod
    def _scrape(values: dict) -> SourceScrapeConfig:
        contacts = bool(values.get("extract_contacts", True))
        directory = bool(values.get("extract_directory", True))
        if not contacts and not directory:
            raise MethodConfigError("연락처 또는 직원/업무 명부 추출을 하나 이상 선택하세요.")
        return SourceScrapeConfig(extract_contacts=contacts, extract_directory=directory)

    @staticmethod
    def _crawl(url: str, values: dict) -> SourceCrawlConfig:
        contacts = bool(values.get("extract_contacts", True))
        directory = bool(values.get("extract_directory", True))
        if not contacts and not directory:
            raise MethodConfigError("연락처 또는 직원/업무 명부 추출을 하나 이상 선택하세요.")
        try:
            scope = CrawlScope(values.get("scope", CrawlScope.PATH_PREFIX))
            depth = int(values.get("max_depth", 2))
            pages = int(values.get("max_pages", 50))
            delay = int(values.get("request_delay_ms", 1000))
        except (TypeError, ValueError) as error:
            raise MethodConfigError("크롤링 제한값이 올바르지 않습니다.") from error
        if not 0 <= depth <= MAX_CRAWL_DEPTH:
            raise MethodConfigError(f"최대 깊이는 0~{MAX_CRAWL_DEPTH} 범위여야 합니다.")
        if not 1 <= pages <= MAX_CRAWL_PAGES:
            raise MethodConfigError(f"최대 페이지는 1~{MAX_CRAWL_PAGES} 범위여야 합니다.")
        if delay < MIN_REQUEST_DELAY_MS:
            raise MethodConfigError(f"요청 간격은 최소 {MIN_REQUEST_DELAY_MS}ms입니다.")
        seed_path = urlsplit(normalize_source_url(url)).path or "/"
        default_path = seed_path if seed_path.endswith("/") else seed_path.rsplit("/", 1)[0] or "/"
        path = str(values.get("allowed_path") or default_path).strip()
        if not path.startswith("/") or ".." in path.split("/"):
            raise MethodConfigError("허용 경로는 /로 시작하는 안전한 경로여야 합니다.")
        prefix = path.rstrip("/") or "/"
        if scope is CrawlScope.PATH_PREFIX and seed_path != prefix and not seed_path.startswith(prefix.rstrip("/") + "/"):
            raise MethodConfigError("Index URL은 지정한 허용 경로 안에 있어야 합니다.")
        return SourceCrawlConfig(
            scope=scope, allowed_path=path, max_depth=depth, max_pages=pages,
            request_delay_ms=delay, extract_contacts=contacts, extract_directory=directory,
        )

    @staticmethod
    def _api(values: dict) -> SourceApiConfig:
        try:
            kind = ApiSourceKind(values.get("kind", ApiSourceKind.OPEN_API))
            auth = ApiAuthMode(values.get("auth_mode", ApiAuthMode.NONE))
            pagination = ApiPaginationMode(values.get("pagination_mode", ApiPaginationMode.NONE))
        except ValueError as error:
            raise MethodConfigError("API/RSS 설정값이 올바르지 않습니다.") from error
        if kind in {ApiSourceKind.RSS, ApiSourceKind.ATOM}:
            response_format = DataFormat.XML
            mapping: dict[str, str] = {}
            record_path = None
            discovery_only = True
            pagination = ApiPaginationMode.NONE
            auth = ApiAuthMode.NONE
        else:
            try:
                response_format = DataFormat(values.get("response_format", DataFormat.JSON))
            except ValueError as error:
                raise MethodConfigError("OpenAPI 응답 형식이 올바르지 않습니다.") from error
            if response_format not in {DataFormat.JSON, DataFormat.XML, DataFormat.CSV}:
                raise MethodConfigError("OpenAPI 응답 형식은 JSON, XML, CSV만 지원합니다.")
            mapping = {
                str(key): str(value).strip()
                for key, value in dict(values.get("field_mapping") or {}).items()
                if value is not None and str(value).strip()
            }
            unknown = set(mapping) - SEMANTIC_FIELDS
            if unknown:
                raise MethodConfigError("지원하지 않는 필드 매핑이 포함되어 있습니다.")
            record_path = str(values.get("record_path") or "").strip() or None
            discovery_only = bool(values.get("discovery_only", False))
            if not mapping and not discovery_only:
                raise MethodConfigError("필드 매핑 또는 명시적인 discovery-only 설정이 필요합니다.")
        static_params = {
            str(key): str(value)
            for key, value in dict(values.get("static_params") or {}).items()
            if value is not None
        }
        if any(not SAFE_NAME.fullmatch(key) for key in static_params):
            raise MethodConfigError("요청 parameter 이름이 올바르지 않습니다.")
        if {key.casefold() for key in static_params} & SECRET_PARAMETER_NAMES:
            raise MethodConfigError("비밀 parameter는 static parameters가 아니라 credential store를 사용하세요.")
        try:
            page_size = int(values.get("page_size", 100))
            start_page = int(values.get("start_page", 1))
            max_pages = int(values.get("max_pages", 1))
            delay = int(values.get("request_delay_ms", 1000))
        except (TypeError, ValueError) as error:
            raise MethodConfigError("API pagination 제한값이 올바르지 않습니다.") from error
        page_parameter = str(values.get("page_parameter") or "").strip() or None
        page_size_parameter = str(values.get("page_size_parameter") or "").strip() or None
        if pagination is ApiPaginationMode.PAGE_NUMBER:
            if not page_parameter or not SAFE_NAME.fullmatch(page_parameter):
                raise MethodConfigError("페이지 parameter 이름이 필요합니다.")
            if page_size_parameter and not SAFE_NAME.fullmatch(page_size_parameter):
                raise MethodConfigError("페이지 크기 parameter 이름이 올바르지 않습니다.")
        if not 1 <= page_size <= MAX_API_PAGE_SIZE or start_page < 1 or not 1 <= max_pages <= MAX_API_PAGES:
            raise MethodConfigError("API pagination 범위가 안전 한도를 벗어났습니다.")
        if delay < MIN_REQUEST_DELAY_MS:
            raise MethodConfigError(f"요청 간격은 최소 {MIN_REQUEST_DELAY_MS}ms입니다.")
        credential_ref = str(values.get("credential_ref") or "").strip() or None
        credential_name = str(values.get("credential_name") or "").strip() or None
        if auth is not ApiAuthMode.NONE and (not credential_ref or not credential_name or not SAFE_NAME.fullmatch(credential_name)):
            raise MethodConfigError("API Key 인증에는 자격증명 참조와 parameter/header 이름이 필요합니다.")
        expiry = values.get("credential_expires_on")
        if isinstance(expiry, str) and expiry:
            try:
                expiry = date.fromisoformat(expiry)
            except ValueError as error:
                raise MethodConfigError("자격증명 만료일이 올바르지 않습니다.") from error
        if expiry is not None and not isinstance(expiry, date):
            raise MethodConfigError("자격증명 만료일이 올바르지 않습니다.")
        return SourceApiConfig(
            kind=kind, response_format=response_format, record_path=record_path,
            field_mapping=mapping, static_params=static_params, discovery_only=discovery_only,
            pagination_mode=pagination, page_parameter=page_parameter,
            page_size_parameter=page_size_parameter, page_size=page_size,
            start_page=start_page, max_pages=max_pages, request_delay_ms=delay,
            auth_mode=auth, credential_ref=credential_ref, credential_name=credential_name,
            credential_expires_on=expiry or None,
            catalog_id=str(values.get("catalog_id") or "").strip() or None,
            catalog_version=str(values.get("catalog_version") or "").strip() or None,
        )

    @staticmethod
    def snapshot(source: Source) -> tuple[str | None, dict]:
        if source.collection_method is CollectionMethod.WEB_PAGE:
            config = source.scrape_config or SourceScrapeConfig(extract_contacts=True, extract_directory=True)
            return None, {
                "target_url": source.url, "extract_contacts": config.extract_contacts,
                "extract_directory": config.extract_directory,
            }
        if source.collection_method is CollectionMethod.WEB_CRAWL:
            config = source.crawl_config
            if config is None:
                raise MethodConfigError("크롤링 설정이 없습니다.")
            return None, {
                "seed_url": source.url, "scope": config.scope.value, "allowed_path": config.allowed_path,
                "max_depth": config.max_depth, "max_pages": config.max_pages,
                "request_delay_ms": config.request_delay_ms, "robots_txt": True,
                "extract_contacts": config.extract_contacts, "extract_directory": config.extract_directory,
            }
        config = source.api_config
        if config is None:
            raise MethodConfigError("API/RSS 설정이 없습니다.")
        return config.kind.value, {
            "endpoint": source.url, "kind": config.kind.value, "response_format": config.response_format.value,
            "record_path": config.record_path, "field_mapping": config.field_mapping,
            "static_params": config.static_params, "discovery_only": config.discovery_only,
            "pagination_mode": config.pagination_mode.value, "page_parameter": config.page_parameter,
            "page_size_parameter": config.page_size_parameter, "page_size": config.page_size,
            "start_page": config.start_page, "max_pages": config.max_pages,
            "request_delay_ms": config.request_delay_ms, "auth_mode": config.auth_mode.value,
            "credential_ref": config.credential_ref, "credential_name": config.credential_name,
            "credential_expires_on": config.credential_expires_on.isoformat() if config.credential_expires_on else None,
            "catalog_id": config.catalog_id, "catalog_version": config.catalog_version,
        }
