'''Live Dashboard read model.'''
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Agency, ContactPoint, CrawlRun, DetectedChangeCandidate, ReviewStatus, RunStatus, Source, SourceBinding
from app.services.review_read_service import ReviewReadService
from app.services.run_service import RunService

LOCAL_ZONE = ZoneInfo('Asia/Seoul')


def _points(values, maximum=None):
    maximum = maximum or max([1, *values])
    return ' '.join(f'{14+i*82},{140-round(value/maximum*100)}' for i, value in enumerate(values))


class DashboardService:
    def __init__(self, session: Session):
        self.session = session

    def read(self, *, now: datetime | None = None):
        local_now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_ZONE)
        agency_count = self.session.scalar(select(func.count()).select_from(Agency).where(Agency.active.is_(True))) or 0
        source_count = self.session.scalar(select(func.count(func.distinct(Source.id))).join(SourceBinding).where(Source.active.is_(True), SourceBinding.active.is_(True))) or 0
        contact_count = self.session.scalar(select(func.count()).select_from(ContactPoint).where(ContactPoint.active.is_(True))) or 0
        review_count = self.session.scalar(select(func.count()).select_from(DetectedChangeCandidate).where(DetectedChangeCandidate.review_status.in_((ReviewStatus.PENDING_REVIEW, ReviewStatus.DEFERRED)))) or 0
        runs = list(self.session.scalars(select(CrawlRun).order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())))
        latest = {}
        for run in runs:
            latest.setdefault(run.source_id, run)
        error_count = sum(run.status in (RunStatus.FAILED, RunStatus.PARTIAL) for run in latest.values())
        recent_runs = tuple({'time': row['time'], 'agency': row['agency'], 'source': row['source'], 'status': row['result'], 'tone': row['tone']} for row in RunService(self.session).list_page()['items'][:6])
        dates = [local_now.date()-timedelta(days=offset) for offset in range(6, -1, -1)]
        success, errors = Counter(), Counter()
        for run in runs:
            day = run.started_at.astimezone(LOCAL_ZONE).date()
            if day in dates and run.status is RunStatus.SUCCESS:
                success[day] += 1
            elif day in dates and run.status in (RunStatus.FAILED, RunStatus.PARTIAL):
                errors[day] += 1
        success_values = [success[day] for day in dates]
        error_values = [errors[day] for day in dates]
        pending = []
        for item in ReviewReadService(self.session).list_page()['items']:
            if item['state'] in {'검토 대기', '보류', '확인 필요'}:
                pending.append({'agency': item['agency'], 'field': f"{item['target']} · {item['field']}", 'before': item['current'], 'after': item['discovered'], 'detected': item['detected'], 'status': item['state'], 'tone': item['tone']})
            if len(pending) == 5:
                break
        return self._result(agency_count, source_count, contact_count, review_count, error_count, recent_runs, dates, success_values, error_values, pending)

    @staticmethod
    def _result(agency_count, source_count, contact_count, review_count, error_count, recent_runs, dates, success, errors, pending):
        shared_maximum = max([1, *success, *errors])
        return {
            'is_demo': False,
            'kpis': (
                {'label': '등록 기관', 'value': agency_count, 'note': '활성 기관', 'tone': 'primary', 'icon': 'building'},
                {'label': '수집 소스', 'value': source_count, 'note': '활성 연결 보유', 'tone': 'info', 'icon': 'link'},
                {'label': '연락처 DB', 'value': contact_count, 'note': '활성 확정 연락처', 'tone': 'success', 'icon': 'contacts'},
                {'label': '검토 대기', 'value': review_count, 'note': '대기 및 보류', 'tone': 'warning', 'icon': 'review'},
                {'label': '수집 오류', 'value': error_count, 'note': '소스별 최신 실행', 'tone': 'danger', 'icon': 'alert'},
            ),
            'recent_runs': recent_runs,
            'trend': {'labels': tuple(day.strftime('%m/%d') for day in dates), 'success_total': sum(success), 'error_total': sum(errors), 'success_points': _points(success, shared_maximum), 'error_points': _points(errors, shared_maximum)},
            'pending_reviews': tuple(pending),
            'quick_actions': (
                {'label': '기관/조직', 'description': '기관과 조직 정보를 확인합니다.', 'href': '/agencies', 'icon': 'building'},
                {'label': '수집 소스', 'description': '등록 소스와 상태를 확인합니다.', 'href': '/sources', 'icon': 'link'},
                {'label': '연락처 DB', 'description': '확정 연락처를 확인합니다.', 'href': '/contacts', 'icon': 'contacts'},
            ),
        }
