"""Deterministic JSON/XML/CSV and RSS/Atom discovery extraction."""

from __future__ import annotations

import csv
import io
import json
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.collectors.html_contact_extractor import normalize_email, normalize_phone
from app.models import (
    ApiSourceKind, CandidateType, DataFormat, DetectionMethod,
    DirectoryRecordType, ExtractedContactCandidate, ExtractedDirectoryRecord,
    ExtractedFeedItem, ExtractionRun, ExtractionStatus,
)
from app.models.common import utc_now


class StructuredExtractionError(ValueError):
    pass


def _decode(content: bytes, declared_charset: str | None = None) -> str:
    for encoding in (declared_charset, "utf-8-sig", "cp949"):
        if not encoding:
            continue
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise StructuredExtractionError("응답 문자 인코딩을 해석할 수 없습니다.")


def _dot(value: Any, path: str | None) -> Any:
    current = value
    for part in (path or "").strip(".").split("."):
        if not part:
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise StructuredExtractionError(f"record path를 찾을 수 없습니다: {path}")
    return current


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_path(nodes: list[ET.Element], path: str | None) -> list[ET.Element]:
    parts = [part for part in (path or "").replace(".", "/").split("/") if part]
    current = nodes
    if parts and current and _local(current[0].tag) == parts[0]:
        parts = parts[1:]
    for part in parts:
        next_nodes = []
        for node in current:
            next_nodes.extend(child for child in list(node) if _local(child.tag) == part)
        current = next_nodes
    return current


def _xml_value(node: ET.Element, path: str) -> str | None:
    selected = _xml_path([node], path)
    if not selected:
        return None
    return " ".join((selected[0].text or "").split()) or None


def parse_structured_records(
    content: bytes,
    response_format: DataFormat,
    record_path: str | None,
    *,
    declared_charset: str | None = None,
) -> list[Any]:
    try:
        if response_format is DataFormat.JSON:
            payload = json.loads(_decode(content, declared_charset))
            selected = _dot(payload, record_path)
            if selected is None:
                return []
            if isinstance(selected, list):
                return selected
            if isinstance(selected, dict):
                return [selected]
            raise StructuredExtractionError("record path 결과는 object 또는 array여야 합니다.")
        if response_format is DataFormat.XML:
            root = ET.fromstring(content)
            return _xml_path([root], record_path) if record_path else list(root)
        if response_format is DataFormat.CSV:
            return list(csv.DictReader(io.StringIO(_decode(content, declared_charset))))
    except (json.JSONDecodeError, ET.ParseError, csv.Error) as error:
        raise StructuredExtractionError("구조화 응답을 해석할 수 없습니다.") from error
    raise StructuredExtractionError("지원하지 않는 구조화 응답 형식입니다.")


def mapped_record(record: Any, mapping: dict[str, str], response_format: DataFormat) -> dict[str, str | None]:
    projected: dict[str, str | None] = {}
    for semantic, path in mapping.items():
        if response_format is DataFormat.XML:
            value = _xml_value(record, path)
        elif response_format is DataFormat.JSON:
            try:
                value = _dot(record, path)
            except StructuredExtractionError:
                value = None
        else:
            value = record.get(path) if isinstance(record, dict) else None
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        projected[semantic] = " ".join(str(value).split())[:4000] if value is not None and str(value).strip() else None
    return projected


@dataclass(frozen=True)
class StructuredResult:
    extraction_run: ExtractionRun
    raw_records: int
    mapped_records: int


class StructuredExtractionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def extract_open_api(
        self, observation_id: uuid.UUID, content: bytes, response_format: DataFormat,
        record_path: str | None, mapping: dict[str, str], discovery_only: bool,
        declared_charset: str | None = None,
    ) -> StructuredResult:
        run = ExtractionRun(
            observation_id=observation_id, extractor_name="staff_directory",
            extractor_version="structured-api-1", status=ExtractionStatus.RUNNING,
            started_at=utc_now(), candidates_found=0,
        )
        self.session.add(run)
        self.session.commit()
        try:
            records = parse_structured_records(content, response_format, record_path, declared_charset=declared_charset)
            mapped_count = 0
            for index, record in enumerate(records):
                values = mapped_record(record, mapping, response_format)
                usable = {key: value for key, value in values.items() if value}
                if not usable:
                    continue
                if any(usable.get(key) for key in ("org_unit", "duty", "position", "person_name")):
                    self.session.add(ExtractedDirectoryRecord(
                        extraction_run_id=run.id, observation_id=observation_id,
                        record_type=DirectoryRecordType.STAFF_DIRECTORY_ROW,
                        org_unit_text=usable.get("org_unit"), duty_text=usable.get("duty"),
                        position_text=usable.get("position"), person_name_text=usable.get("person_name"),
                        phone_text=usable.get("phone"), email_text=usable.get("email"),
                        fax_text=usable.get("fax"), row_text=" | ".join(usable.values())[:4000],
                        source_locator=usable.get("record_url") or f"record[{index}]",
                        structured_payload=usable,
                    ))
                    mapped_count += 1
                else:
                    for semantic, candidate_type in (
                        ("phone", CandidateType.PHONE), ("email", CandidateType.EMAIL), ("fax", CandidateType.FAX)
                    ):
                        raw = usable.get(semantic)
                        if not raw:
                            continue
                        try:
                            normalized = normalize_email(raw) if candidate_type is CandidateType.EMAIL else normalize_phone(raw)
                        except ValueError:
                            continue
                        if not normalized:
                            continue
                        self.session.add(ExtractedContactCandidate(
                            extraction_run_id=run.id, observation_id=observation_id,
                            candidate_type=candidate_type, raw_value=raw[:500],
                            normalized_value=normalized[:500], context_text=None,
                            source_locator=usable.get("record_url") or f"record[{index}]",
                            detection_method=DetectionMethod.TEXT_PATTERN,
                        ))
                        mapped_count += 1
            run.status = ExtractionStatus.SUCCESS
            run.finished_at = utc_now()
            run.candidates_found = mapped_count
            self.session.commit()
            return StructuredResult(run, len(records), mapped_count)
        except Exception as error:
            self.session.rollback()
            failed = self.session.get(ExtractionRun, run.id)
            if failed is not None:
                failed.status = ExtractionStatus.FAILED
                failed.finished_at = utc_now()
                failed.error_summary = (str(error) or error.__class__.__name__)[:1000]
                self.session.commit()
            raise StructuredExtractionError(str(error) or "구조화 추출 실패") from error

    def extract_feed(
        self, observation_id: uuid.UUID, content: bytes, kind: ApiSourceKind
    ) -> StructuredResult:
        run = ExtractionRun(
            observation_id=observation_id, extractor_name="feed",
            extractor_version="1", status=ExtractionStatus.RUNNING,
            started_at=utc_now(), candidates_found=0,
        )
        self.session.add(run)
        self.session.commit()
        try:
            root = ET.fromstring(content)
            if kind is ApiSourceKind.RSS:
                nodes = [node for node in root.iter() if _local(node.tag) == "item"]
            else:
                nodes = [node for node in root.iter() if _local(node.tag) == "entry"]
            for index, node in enumerate(nodes):
                values: dict[str, str | None] = {}
                for name in ("title", "guid", "id", "published", "pubDate", "updated", "author", "summary", "description"):
                    child = next((item for item in list(node) if _local(item.tag) == name), None)
                    if child is not None:
                        values[name] = " ".join("".join(child.itertext()).split())[:4000] or None
                link_node = next((item for item in list(node) if _local(item.tag) == "link"), None)
                link = None
                if link_node is not None:
                    link = link_node.attrib.get("href") or (link_node.text or "").strip() or None
                self.session.add(ExtractedFeedItem(
                    extraction_run_id=run.id, observation_id=observation_id,
                    item_identity=values.get("guid") or values.get("id") or link,
                    title=values.get("title"), link=link,
                    published_at=values.get("published") or values.get("pubDate"),
                    updated_at_text=values.get("updated"),
                    author=values.get("author"),
                    summary=values.get("summary") or values.get("description"),
                    source_locator=f"item[{index}]", structured_payload=values,
                ))
            run.status = ExtractionStatus.SUCCESS
            run.finished_at = utc_now()
            run.candidates_found = len(nodes)
            self.session.commit()
            return StructuredResult(run, len(nodes), len(nodes))
        except ET.ParseError as error:
            self.session.rollback()
            failed = self.session.get(ExtractionRun, run.id)
            if failed is not None:
                failed.status = ExtractionStatus.FAILED
                failed.finished_at = utc_now()
                failed.error_summary = "feed XML을 해석할 수 없습니다."
                self.session.commit()
            raise StructuredExtractionError("feed XML을 해석할 수 없습니다.") from error
