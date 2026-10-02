"""Read-only, bounded Agency suggestions from one explicitly requested URL."""

from __future__ import annotations

import html
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.http_fetcher import HTTPFetchError, HTTPFetcher
from app.models import Agency, AgencyType, CollectionMethod
from app.services.normalization import collapse_whitespace, normalize_agency_name, normalize_source_url


logger = logging.getLogger("publicdb2")

MAX_DISCOVERY_EVIDENCE = 8
MAX_DISCOVERY_SNIPPET = 160

_SEPARATORS = re.compile(r"\s*(?:\||｜|·|•|::|»|>|―|–|—)\s*")
_NOISE = re.compile(
    r"(?:직원\s*안내|부서\s*안내|조직\s*안내|조직도|담당자\s*안내|"
    r"홈페이지|메인\s*페이지|민원\s*안내|공지\s*사항|누리집)",
    re.IGNORECASE,
)
_COPYRIGHT = re.compile(r"(?i)(?:copyright|all rights reserved|©|ⓒ)|\b(?:19|20)\d{2}\b")
_AGENCY_PATTERN = re.compile(
    r"[가-힣A-Za-z0-9·]{2,40}(?:특별자치시|특별자치도|특별시|광역시|"
    r"교육청|소방본부|구청|군청|시청|도청|위원회|공단|공사|재단|연구원|대학교|부|처|청)"
)
_GENERIC_NAMES = {"직원", "직원 안내", "부서", "부서 안내", "조직", "조직도", "홈", "메인"}

_REGION_MARKERS = (
    ("SEOUL", ("서울특별시", "서울시")),
    ("BUSAN", ("부산광역시", "부산시")),
    ("DAEGU", ("대구광역시", "대구시")),
    ("INCHEON", ("인천광역시", "인천시")),
    ("GWANGJU", ("광주광역시", "광주시")),
    ("DAEJEON", ("대전광역시", "대전시")),
    ("ULSAN", ("울산광역시", "울산시")),
    ("SEJONG", ("세종특별자치시", "세종시")),
    ("GYEONGGI", ("경기도",)),
    ("GANGWON", ("강원특별자치도", "강원도")),
    ("CHUNGBUK", ("충청북도", "충북")),
    ("CHUNGNAM", ("충청남도", "충남")),
    ("JEONBUK", ("전북특별자치도", "전라북도", "전북")),
    ("JEONNAM", ("전라남도", "전남")),
    ("GYEONGBUK", ("경상북도", "경북")),
    ("GYEONGNAM", ("경상남도", "경남")),
    ("JEJU", ("제주특별자치도", "제주도")),
)


class AgencyDiscoveryError(ValueError):
    pass


@dataclass(frozen=True)
class _Evidence:
    candidate: str
    kind: str
    label: str
    snippet: str


def _decode(content: bytes, declared_charset: str | None) -> str:
    for encoding in (declared_charset, "utf-8-sig", "cp949"):
        if not encoding:
            continue
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def _bounded_text(value: object) -> str:
    text = collapse_whitespace(html.unescape(str(value or "")))
    return text[:MAX_DISCOVERY_SNIPPET]


def _candidate_from_text(value: object, *, strong: bool = False) -> str | None:
    original = _bounded_text(value)
    if not original:
        return None
    cleaned = collapse_whitespace(_COPYRIGHT.sub(" ", _NOISE.sub(" ", original))).strip(" -_:;,.()[]{}")
    if not cleaned or cleaned in _GENERIC_NAMES:
        return None
    pieces = [piece.strip(" -_:;,.()[]{}") for piece in _SEPARATORS.split(cleaned) if piece.strip()]
    matches: list[str] = []
    for piece in pieces or [cleaned]:
        matches.extend(_AGENCY_PATTERN.findall(piece.replace(" ", "")))
    if matches:
        return matches[-1][:300]
    if strong and 2 <= len(cleaned) <= 80 and not re.fullmatch(r"[\W\d_]+", cleaned):
        return cleaned
    return None


def _evidence(value: object, kind: str, label: str, *, strong: bool = False) -> _Evidence | None:
    snippet = _bounded_text(value)
    candidate = _candidate_from_text(snippet, strong=strong)
    return _Evidence(candidate, kind, label, snippet) if candidate else None


def _json_ld_objects(value: object):
    queue = [value]
    visited = 0
    while queue and visited < 100:
        item = queue.pop(0)
        visited += 1
        if isinstance(item, dict):
            yield item
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item[:30])


class SourceAgencyDiscoveryService:
    def __init__(self, session: Session, fetcher: HTTPFetcher) -> None:
        self.session = session
        self.fetcher = fetcher

    def discover(
        self, *, representative_url: str, collection_method: CollectionMethod,
        api_kind: str | None = None, auth_mode: str | None = None,
    ) -> dict:
        normalized_url = normalize_source_url(representative_url)
        host = (urlsplit(normalized_url).hostname or "").lower()
        logger.info("agency_discovery_started host=%s method=%s", host, collection_method.value)

        if (
            collection_method is CollectionMethod.API
            and (api_kind or "OPEN_API").upper() == "OPEN_API"
            and (auth_mode or "NONE").upper() != "NONE"
        ):
            result = self._result(
                normalized_url, [], message=(
                    "인증이 필요한 API는 기관 확인을 위해 자동 호출하지 않습니다. "
                    "기존 기관을 검색하거나 새 기관을 등록하세요."
                ),
            )
            logger.info("agency_discovery_result host=%s evidence=0 candidate=false", host)
            return result

        try:
            fetched = self.fetcher.fetch(normalized_url)
            text = _decode(fetched.content, fetched.declared_charset)
            kind = (api_kind or "").upper()
            if kind in {"RSS", "ATOM"} or "rss" in fetched.content_type or "atom" in fetched.content_type:
                evidence = self._feed_evidence(text)
            elif collection_method is CollectionMethod.API and (
                "json" in fetched.content_type or text.lstrip().startswith(("{", "["))
            ):
                evidence = self._json_evidence(text)
            elif not fetched.content_type or "html" in fetched.content_type or "xml" in fetched.content_type:
                evidence = self._html_evidence(text)
            else:
                evidence = []
            result = self._result(fetched.final_url, evidence)
            logger.info(
                "agency_discovery_result host=%s evidence=%d candidate=%s",
                host, len(result["evidence"]), bool(result["candidate_name"]),
            )
            return result
        except HTTPFetchError as error:
            logger.warning("agency_discovery_failed host=%s error=%s", host, error.__class__.__name__)
            raise AgencyDiscoveryError(
                "기관을 자동 확인하지 못했습니다. 기존 기관을 검색하거나 새 기관을 등록하세요."
            ) from error

    @staticmethod
    def _html_evidence(document: str) -> list[_Evidence]:
        soup = BeautifulSoup(document, "html.parser")
        found: list[_Evidence] = []

        def add(value: object, kind: str, label: str, *, strong: bool = False) -> None:
            item = _evidence(value, kind, label, strong=strong)
            if item:
                found.append(item)

        site = soup.find("meta", attrs={"property": re.compile(r"^og:site_name$", re.I)})
        add(site.get("content") if site else "", "site_name", "사이트명", strong=True)
        for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}, limit=10):
            try:
                payload = json.loads(script.string or script.get_text() or "null")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for item in _json_ld_objects(payload):
                raw_type = item.get("@type")
                types = raw_type if isinstance(raw_type, list) else [raw_type]
                if any(str(value).lower() in {"organization", "governmentorganization", "governmentoffice"} for value in types):
                    add(item.get("name"), "json_ld_organization", "구조화된 기관명", strong=True)
        add(soup.title.get_text(" ", strip=True) if soup.title else "", "title", "페이지 제목")
        for node in soup.find_all("h1", limit=3):
            add(node.get_text(" ", strip=True), "heading", "대표 제목")
        breadcrumb = soup.select_one('[aria-label*="breadcrumb" i], .breadcrumb, .breadcrumbs, nav.breadcrumb')
        add(breadcrumb.get_text(" ", strip=True) if breadcrumb else "", "breadcrumb", "현재 위치")
        for footer in soup.find_all("footer", limit=2):
            add(footer.get_text(" ", strip=True), "footer", "페이지 하단")
        description = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        add(description.get("content") if description else "", "description", "페이지 설명")
        return found

    @staticmethod
    def _feed_evidence(document: str) -> list[_Evidence]:
        soup = BeautifulSoup(document, "html.parser")
        root = soup.find("channel") or soup.find("feed")
        if root is None:
            return []
        found: list[_Evidence] = []
        for tag, kind, label, strong in (
            ("title", "feed_title", "피드 제목", True),
            ("subtitle", "feed_subtitle", "피드 설명", False),
            ("description", "feed_description", "피드 설명", False),
        ):
            node = root.find(tag, recursive=False)
            item = _evidence(node.get_text(" ", strip=True) if node else "", kind, label, strong=strong)
            if item:
                found.append(item)
        return found

    @staticmethod
    def _json_evidence(document: str) -> list[_Evidence]:
        try:
            payload = json.loads(document)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        values: list[tuple[object, str, str, bool]] = []
        for key, label in (("agency", "기관 필드"), ("organization", "기관 필드"), ("publisher", "제공 기관")):
            value = payload.get(key)
            if isinstance(value, dict):
                value = value.get("name")
            values.append((value, f"json_{key}", label, True))
        info = payload.get("info")
        if isinstance(info, dict):
            contact = info.get("contact")
            values.append((contact.get("name") if isinstance(contact, dict) else None, "api_contact", "API 제공 기관", True))
            values.append((info.get("title"), "api_title", "API 제목", False))
        return [item for value, kind, label, strong in values if (item := _evidence(value, kind, label, strong=strong))]

    def _result(self, final_url: str, evidence: list[_Evidence], *, message: str | None = None) -> dict:
        grouped: dict[str, list[_Evidence]] = defaultdict(list)
        display: dict[str, str] = {}
        for item in evidence:
            normalized = normalize_agency_name(item.candidate)
            key = normalized.casefold()
            if not key or any(existing.kind == item.kind for existing in grouped[key]):
                continue
            grouped[key].append(item)
            display.setdefault(key, item.candidate)
        ranked = sorted(grouped, key=lambda key: (-len(grouped[key]), len(display[key]), display[key]))
        winner = ranked[0] if ranked else None
        candidate = display[winner] if winner else None
        selected_evidence = grouped[winner][:MAX_DISCOVERY_EVIDENCE] if winner else []
        existing, match_type = self._exact_match(candidate, final_url)
        if existing and not candidate:
            candidate = existing.official_name
        evidence_payload = [
            {"type": item.kind, "label": item.label, "snippet": item.snippet}
            for item in selected_evidence
        ]
        if existing and match_type == "HOMEPAGE_HOST":
            evidence_payload.append({
                "type": "homepage_host", "label": "등록된 기관 홈페이지 주소",
                "snippet": urlsplit(final_url).hostname or "",
            })
        combined = " ".join([candidate or "", *(item.snippet for item in evidence)])
        return {
            "candidate_name": candidate,
            "existing_agency": self._agency_projection(existing) if existing else None,
            "match_type": match_type,
            "evidence": evidence_payload[:MAX_DISCOVERY_EVIDENCE],
            "evidence_summary": {
                "count": len(evidence_payload[:MAX_DISCOVERY_EVIDENCE]),
                "types": [item["type"] for item in evidence_payload[:MAX_DISCOVERY_EVIDENCE]],
            },
            "suggested_agency_type": self._suggest_agency_type(candidate),
            "suggested_region_code": self._suggest_region(combined),
            "message": message or (None if candidate or existing else (
                "페이지에서 기관을 확실히 확인하지 못했습니다. "
                "기존 기관을 검색하거나 새 기관을 등록하세요."
            )),
        }

    def _exact_match(self, candidate: str | None, url: str) -> tuple[Agency | None, str | None]:
        if candidate:
            matches = list(self.session.scalars(select(Agency).where(
                Agency.normalized_name == normalize_agency_name(candidate),
                Agency.active.is_(True),
            )))
            if len(matches) == 1:
                return matches[0], "EXACT_NAME"
        host = (urlsplit(url).hostname or "").lower()
        if host:
            homepage_matches = []
            for agency in self.session.scalars(select(Agency).where(
                Agency.homepage_url.is_not(None), Agency.active.is_(True),
            )):
                if (urlsplit(agency.homepage_url or "").hostname or "").lower() == host:
                    homepage_matches.append(agency)
            if len(homepage_matches) == 1:
                return homepage_matches[0], "HOMEPAGE_HOST"
        return None, None

    @staticmethod
    def _agency_projection(agency: Agency | None) -> dict | None:
        if agency is None:
            return None
        return {
            "id": str(agency.id), "name": agency.official_name,
            "agency_type": agency.agency_type.value, "region_code": agency.region_code,
        }

    @staticmethod
    def _suggest_agency_type(candidate: str | None) -> str | None:
        name = candidate or ""
        if any(marker in name for marker in ("특별시", "광역시", "특별자치시", "특별자치도")):
            return AgencyType.METROPOLITAN_GOVERNMENT.value
        if name.endswith(("구청", "군청", "시청")):
            return AgencyType.BASIC_LOCAL_GOVERNMENT.value
        return None

    @staticmethod
    def _suggest_region(text: str) -> str | None:
        for code, markers in _REGION_MARKERS:
            if any(marker in text for marker in markers):
                return code
        return None
