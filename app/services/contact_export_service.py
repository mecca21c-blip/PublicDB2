'''Filtered confirmed-contact XLSX export.'''
from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import CollectionMethod, ContactPoint, ContactType, CrawlRun, EntityType, Observation, PersonAssignment, Source, SourceOccurrence
from app.services.source_method_service import user_method_label

HEADERS = ('기관', '부서', '업무', '담당자', '연락처 유형', '연락처 값', '확인일', '공식 출처 URL', '수집 방식', '실제 발견 URL')


def safe_cell(value):
    if value is None:
        return ''
    text = str(value)
    return "'" + text if text.startswith(('=', '+', '-', '@')) else text


class ContactExportService:
    def __init__(self, session: Session, *, export_root: Path):
        self.session = session
        self.export_root = export_root.resolve()

    def export(self, *, search=None, agency_id=None, org_unit_id=None, contact_type=None):
        statement = select(ContactPoint).options(
            selectinload(ContactPoint.agency), selectinload(ContactPoint.org_unit),
            selectinload(ContactPoint.duty),
            selectinload(ContactPoint.person_assignment).selectinload(PersonAssignment.person),
        ).where(ContactPoint.active.is_(True)).order_by(ContactPoint.created_at, ContactPoint.id)
        if agency_id:
            statement = statement.where(ContactPoint.agency_id == agency_id)
        if org_unit_id:
            statement = statement.where(ContactPoint.org_unit_id == org_unit_id)
        if contact_type:
            statement = statement.where(ContactPoint.contact_type == contact_type)
        contacts = list(self.session.scalars(statement))
        term = (search or '').strip().casefold()
        if term:
            contacts = [item for item in contacts if term in ' '.join((
                item.agency.official_name, item.org_unit.name if item.org_unit else '',
                item.duty.title if item.duty else '',
                item.person_assignment.person.name if item.person_assignment else '', item.value,
            )).casefold()]
        sources = self._sources([item.id for item in contacts])
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '확정 연락처'
        sheet.append(HEADERS)
        for item in contacts:
            values = (
                item.agency.official_name, item.org_unit.name if item.org_unit else '',
                item.duty.title if item.duty else '',
                item.person_assignment.person.name if item.person_assignment else '',
                item.contact_type.value, item.value,
                item.verified_at.isoformat() if item.verified_at else '',
                ', '.join(sorted(sources.get(item.id, {}).get('official', set()))),
                ', '.join(sorted(sources.get(item.id, {}).get('methods', set()))),
                ', '.join(sorted(sources.get(item.id, {}).get('actual', set()))),
            )
            sheet.append(tuple(safe_cell(value) for value in values))
        now = datetime.now()
        directory = (self.export_root / now.strftime('%Y') / now.strftime('%m') / now.strftime('%d')).resolve()
        if self.export_root not in directory.parents:
            raise RuntimeError('내보내기 경로가 안전하지 않습니다.')
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"publicdb2_contacts_{now:%Y%m%d_%H%M%S}_{secrets.token_hex(4)}.xlsx"
        final_path = (directory / filename).resolve()
        temporary = final_path.with_suffix('.tmp')
        try:
            workbook.save(temporary)
            os.replace(temporary, final_path)
        finally:
            temporary.unlink(missing_ok=True)
        return final_path

    def _sources(self, contact_ids: list[uuid.UUID]):
        result = {}
        if not contact_ids:
            return result
        rows = self.session.execute(
            select(
                SourceOccurrence.entity_id, Source.url, Observation.final_url,
                Observation.page_url, CrawlRun.collection_method_snapshot,
                CrawlRun.collection_kind_snapshot,
            )
            .join(Observation, Observation.id == SourceOccurrence.observation_id)
            .join(CrawlRun, CrawlRun.id == Observation.crawl_run_id)
            .join(Source, Source.id == Observation.source_id)
            .where(SourceOccurrence.entity_type == EntityType.CONTACT_POINT, SourceOccurrence.entity_id.in_(contact_ids))
        )
        for entity_id, url, final_url, page_url, method, kind in rows:
            values = result.setdefault(entity_id, {'official': set(), 'methods': set(), 'actual': set()})
            values['official'].add(url)
            values['methods'].add(user_method_label(method or CollectionMethod.WEB_PAGE, kind))
            values['actual'].add(final_url or page_url)
        return result
