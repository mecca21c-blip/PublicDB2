"""FastAPI entrypoint for the PublicDB2 UI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
import logging
import uuid
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.agencies import router as agencies_api
from app.api.agency_discovery import router as agency_discovery_api
from app.api.collection import router as collection_api
from app.api.collection_jobs import router as collection_jobs_api
from app.api.lookups import router as lookups_api
from app.api.master_review import router as master_review_api
from app.api.operations import router as operations_api
from app.api.run_evidence import router as run_evidence_api
from app.api.sources import router as sources_api
from app.api.source_index import router as source_index_api
from app.api.three_way import router as three_way_api
from app.api.dependencies import ensure_csrf_token, get_current_user_optional, get_session, require_admin, require_viewer
from app.collectors.http_fetcher import HTTPFetcher
from app.core.config import allowed_hosts, get_database_url, runtime_paths
from app.core.logging import close_file_logging, configure_file_logging
from app.core.schema import MIGRATION_HEAD
from app.core.security import SecretStore, SecurityHeadersMiddleware, SignedSessionMiddleware
from app.core.time_presentation import format_kst_datetime
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    AgencyType, ChangeEventType, ContactType, OrgUnit, ReviewStatus, RunStatus, User,
)
from app.services.agency_service import AGENCY_TYPE_LABELS, AgencyService
from app.services.collection_service import CollectionCoordinator, CollectionService
from app.services.collection_recovery_service import reconcile_stale_collections
from app.services.collection_job_service import CollectionJobService
from app.services.collection_job_worker import CollectionJobWorker, recover_interrupted_jobs
from app.services.collection_scheduler import CollectionScheduler
from app.services.collection_background_runtime import CollectionBackgroundRuntime
from app.services.catalog_service import CatalogService
from app.services.contact_service import ContactService
from app.services.dashboard_service import DashboardService
from app.services.review_read_service import ReviewReadService
from app.services.run_service import RunService
from app.services.settings_service import SettingsService
from app.services.pagination import page_metadata
from app.services.source_service import SourceService
from app.services.source_query_service import FILTER_METHODS, SourceFilterError, SourceFilterSpec, SourceQueryService
from app.services.source_import_service import PreviewStore
from app.services.user_service import UserService
from app.services.regions import REGIONS


logger = logging.getLogger('publicdb2')


WEB_ROOT = Path(__file__).resolve().parent / "web"

NAVIGATION = (
    {"key": "dashboard", "label": "대시보드", "href": "/", "icon": "dashboard"},
    {"key": "agencies", "label": "기관/조직", "href": "/agencies", "icon": "building"},
    {"key": "sources", "label": "수집 소스", "href": "/sources", "icon": "link"},
    {"key": "runs", "label": "수집 이력", "href": "/runs", "icon": "clock"},
    {"key": "contacts", "label": "연락처 DB", "href": "/contacts", "icon": "contacts"},
    {"key": "review", "label": "변경/검토", "href": "/review", "icon": "review"},
    {"key": "settings", "label": "설정", "href": "/settings", "icon": "settings"},
)

WORKSPACE_PAGES = {
    "agencies": "agencies.html",
    "sources": "sources.html",
    "runs": "runs.html",
    "contacts": "contacts.html",
    "review": "review.html",
    "settings": "settings.html",
}


def _page_context(request: Request, active_page: str, **values: object) -> dict[str, object]:
    user = getattr(request.state, 'current_user', None)
    workspace = values.get('workspace')
    if isinstance(workspace, dict) and workspace.get('pagination'):
        pagination = dict(workspace['pagination'])
        params = dict(request.query_params)
        params['page_size'] = str(pagination['page_size'])
        for key, target in (
            ('previous_url', pagination['page'] - 1 if pagination['has_previous'] else None),
            ('next_url', pagination['page'] + 1 if pagination['has_next'] else None),
        ):
            if target is None:
                pagination[key] = None
            else:
                params['page'] = str(target)
                pagination[key] = request.url.path + '?' + urlencode(params)
        values['workspace'] = {**workspace, 'pagination': pagination}
    return {
        'active_page': active_page, 'navigation': NAVIGATION,
        'current_user': user, 'csrf_token': ensure_csrf_token(request), **values,
    }


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


@asynccontextmanager
async def _lifespan(application: FastAPI):
    handler = configure_file_logging(application.state.runtime_paths)
    logger.info('application startup')
    with application.state.session_factory() as recovery_session:
        try:
            recovered = reconcile_stale_collections(recovery_session)
            interrupted = recover_interrupted_jobs(recovery_session)
            if recovered:
                logger.warning('recovered interrupted collection runs count=%s', recovered)
            if interrupted:
                logger.warning('recovered interrupted collection job items count=%s', interrupted)
        except SQLAlchemyError:
            recovery_session.rollback()
            logger.warning('collection recovery deferred until database is ready')
    application.state.collection_background_runtime.start()
    try:
        yield
    finally:
        application.state.collection_background_runtime.stop()
        logger.info('application shutdown')
        application.state.engine.dispose()
        close_file_logging(handler)


def create_app(database_url: str | None = None, project_root: Path | None = None) -> FastAPI:
    """Create the application without schema creation or collection side effects."""

    application = FastAPI(
        title="PublicDB2",
        description="PublicDB2 local administration workspace UI",
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
    paths = runtime_paths(project_root)
    engine = create_db_engine(database_url or get_database_url(paths.project_root))
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    application.state.runtime_paths = paths
    application.state.secret_store = SecretStore(application.state.runtime_paths)
    application.state.source_import_previews = PreviewStore(application.state.runtime_paths)
    application.state.collection_coordinator = CollectionCoordinator()
    def collection_service_factory(session):
        settings = SettingsService(session).snapshot()
        return CollectionService(
            session, project_root=application.state.runtime_paths.project_root,
            raw_root=application.state.runtime_paths.raw_root,
            coordinator=application.state.collection_coordinator,
            fetcher=HTTPFetcher(
                timeout_seconds=settings.http_timeout_seconds,
                max_response_bytes=settings.max_response_bytes,
                user_agent=settings.user_agent,
            ),
        )
    application.state.collection_service_factory = collection_service_factory
    application.state.collection_job_worker = CollectionJobWorker(
        application.state.session_factory,
        lambda session: application.state.collection_service_factory(session),
    )
    application.state.collection_scheduler = CollectionScheduler(application.state.session_factory)
    application.state.collection_background_runtime = CollectionBackgroundRuntime(
        application.state.collection_job_worker, application.state.collection_scheduler
    )
    def preview_fetcher_factory():
        with application.state.session_factory() as preview_session:
            settings = SettingsService(preview_session).snapshot()
        return HTTPFetcher(
            timeout_seconds=settings.http_timeout_seconds,
            max_response_bytes=settings.max_response_bytes,
            user_agent=settings.user_agent,
        )
    application.state.preview_fetcher_factory = preview_fetcher_factory

    def agency_discovery_fetcher_factory():
        with application.state.session_factory() as discovery_session:
            settings = SettingsService(discovery_session).snapshot()
        return HTTPFetcher(
            timeout_seconds=min(settings.http_timeout_seconds, 8.0),
            max_response_bytes=min(settings.max_response_bytes, 2 * 1024 * 1024),
            user_agent=settings.user_agent,
        )
    application.state.agency_discovery_fetcher_factory = agency_discovery_fetcher_factory

    application.include_router(agencies_api)
    application.include_router(agency_discovery_api)
    application.include_router(sources_api)
    application.include_router(collection_api)
    application.include_router(collection_jobs_api)
    application.include_router(lookups_api)
    application.include_router(source_index_api)
    application.include_router(master_review_api)
    application.include_router(operations_api)
    application.include_router(run_evidence_api)
    application.include_router(three_way_api)
    application.mount(
        "/static",
        StaticFiles(directory=str(WEB_ROOT / "static")),
        name="static",
    )
    application.add_middleware(SignedSessionMiddleware, store=application.state.secret_store)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())
    application.add_middleware(SecurityHeadersMiddleware)

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        if error.status_code == status.HTTP_401_UNAUTHORIZED and not request.url.path.startswith('/api/'):
            return RedirectResponse('/login', status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({'detail': error.detail}, status_code=error.status_code, headers=error.headers)

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        logger.error('database operation failed: %s', error.__class__.__name__)
        message = '데이터베이스 연결 상태를 확인하세요.'
        if request.url.path.startswith('/api/'):
            return JSONResponse({'detail': message}, status_code=503)
        return HTMLResponse(f'<h1>데이터베이스 오류</h1><p>{message}</p>', status_code=503)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "app": "PublicDB2"}

    @application.get('/ready')
    def ready():
        try:
            with engine.connect() as connection:
                connection.execute(text('SELECT 1'))
                revision = connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
            if revision != MIGRATION_HEAD:
                raise RuntimeError('migration mismatch')
            return {'status': 'ready'}
        except Exception:
            return JSONResponse({'status': 'not_ready'}, status_code=503)

    @application.get('/login', response_class=HTMLResponse)
    def login_page(request: Request, user: User | None = Depends(get_current_user_optional)):
        if user is not None:
            return RedirectResponse('/', status_code=303)
        return templates.TemplateResponse(request=request, name='login.html', context={'csrf_token': ensure_csrf_token(request), 'error': None})

    @application.post('/login', response_class=HTMLResponse)
    def login(request: Request, username: str = Form(), password: str = Form(), csrf_token: str = Form(), session=Depends(get_session)):
        try:
            expected = request.state.session.get('csrf_token', '')
            import secrets
            if not expected or not secrets.compare_digest(expected, csrf_token):
                raise HTTPException(status_code=403, detail='요청 보안 토큰이 올바르지 않습니다.')
            user = UserService(session).authenticate(username, password)
            if user is None:
                logger.warning('login failed')
                return templates.TemplateResponse(request=request, name='login.html', context={'csrf_token': expected, 'error': '아이디 또는 비밀번호가 올바르지 않습니다.'}, status_code=401)
            request.state.session.clear()
            request.state.session.update({'user_id': str(user.id), 'csrf_token': secrets.token_urlsafe(32)})
            logger.info('login success user_id=%s', user.id)
            return RedirectResponse('/', status_code=303)
        finally:
            pass

    @application.post('/logout', dependencies=[Depends(require_viewer)])
    def logout(request: Request):
        request.state.session.clear()
        request.state.clear_session = True
        return RedirectResponse('/login', status_code=303)

    @application.get("/", response_class=HTMLResponse, dependencies=[Depends(require_viewer)])
    def dashboard(request: Request) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            live_dashboard = DashboardService(session).read()
            db_error = None
        except SQLAlchemyError:
            session.rollback()
            live_dashboard = None
            db_error = '대시보드 데이터를 불러오지 못했습니다. 데이터베이스 연결 상태를 확인하세요.'
        finally:
            session.close()
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=_page_context(request, "dashboard", dashboard=live_dashboard, db_error=db_error),
        )

    @application.get('/agencies', response_class=HTMLResponse, name='agencies', dependencies=[Depends(require_viewer)])
    def agencies(request: Request, search: str | None = None, agency_type: AgencyType | None = None, region_code: str | None = None, page: int = 1, page_size: int = 100) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            workspace = AgencyService(session).list_page(search=search, agency_type=agency_type, region_code=region_code, page=page, page_size=page_size)
            db_error = None
        except SQLAlchemyError:
            session.rollback()
            workspace = {'items': (), 'details': ()}
            db_error = '기관 데이터를 불러오지 못했습니다. 데이터베이스 마이그레이션과 연결 상태를 확인하세요.'
        finally:
            session.close()
        return templates.TemplateResponse(request=request, name='agencies.html', context=_page_context(request, 'agencies', workspace=workspace, db_error=db_error, filters={'search': search or '', 'agency_type': agency_type.value if agency_type else '', 'region_code': region_code or ''}, agency_types=AgencyType, agency_type_labels=AGENCY_TYPE_LABELS, regions=REGIONS))

    @application.get('/sources', response_class=HTMLResponse, name='sources', dependencies=[Depends(require_viewer)])
    def sources(
        request: Request, search: str | None = None, region_code: str | None = None,
        agency_id: str | None = None, org_unit_id: str | None = None,
        methods: str | None = None, source_status: str | None = None,
        scheduled: str = "all", page: int = 1, page_size: int = 100,
    ) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            method_values = (
                list(FILTER_METHODS) if methods is None
                else [value for value in methods.split(",") if value]
            )
            spec = SourceFilterSpec.build(
                search=search, region_codes=[region_code] if region_code else [],
                agency_id=_uuid_or_none(agency_id), org_unit_id=_uuid_or_none(org_unit_id),
                methods=method_values, status=source_status, scheduled=scheduled,
            )
            query = SourceQueryService(session)
            workspace = query.list_page(spec, page=page, page_size=page_size)
            workspace['registered_total'] = query.registered_count()
            workspace['all_eligible_total'] = query.count(
                SourceFilterSpec.build(methods=None), eligible_only=True
            )
            filter_snapshot = query.snapshot(spec, resolved_count=workspace['eligible_total'])
            catalog = CatalogService(session).list()
            db_error = None
        except (SQLAlchemyError, SourceFilterError, ValueError):
            session.rollback()
            catalog = ()
            workspace = {
                'items': (), 'details': (), 'total': 0, 'eligible_total': 0,
                'registered_total': 0, 'all_eligible_total': 0,
                'method_counts': {}, 'pagination': page_metadata(0, 1, 100),
            }
            filter_snapshot = {}
            db_error = '수집 소스 필터를 확인하지 못했습니다. 선택한 기관·부서와 필터 값을 확인하세요.'
        finally:
            session.close()
        return templates.TemplateResponse(request=request, name='sources.html', context=_page_context(
            request, 'sources', workspace=workspace, db_error=db_error,
            catalog=catalog, regions=REGIONS, filter_snapshot=filter_snapshot,
            filters={
                'search': search or '', 'region_code': region_code or '',
                'agency_id': agency_id or '', 'agency_name': filter_snapshot.get('agency_name') or '',
                'org_unit_id': org_unit_id or '', 'org_unit_name': filter_snapshot.get('org_unit_name') or '',
                'methods': filter_snapshot.get('methods') or [],
                'source_status': source_status or '', 'scheduled': scheduled,
            },
        ))

    @application.get('/runs', response_class=HTMLResponse, name='runs', dependencies=[Depends(require_viewer)])
    def runs(
        request: Request,
        date_from: date | None = None,
        date_to: date | None = None,
        agency_id: str | None = None,
        status: RunStatus | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            agencies_page = AgencyService(session).list_page(page_size=500)
            workspace = RunService(session).list_page(
                date_from=date_from,
                date_to=date_to,
                agency_id=_uuid_or_none(agency_id),
                status=status,
                search=search,
                page=page,
                page_size=page_size,
            )
            collection_jobs = CollectionJobService(session).list_recent(
                failures_only=(status == RunStatus.FAILED), limit=50, include_items=True
            )
            db_error = None
        except SQLAlchemyError:
            session.rollback()
            agencies_page = {'items': ()}
            workspace = {'items': (), 'details': ()}
            collection_jobs = ()
            db_error = '수집 이력을 불러오지 못했습니다. 데이터베이스 마이그레이션과 연결 상태를 확인하세요.'
        finally:
            session.close()
        return templates.TemplateResponse(
            request=request,
            name='runs.html',
            context=_page_context(request,
                'runs',
                workspace=workspace,
                collection_jobs=collection_jobs,
                db_error=db_error,
                agencies=agencies_page['items'],
                run_statuses=RunStatus,
                filters={
                    'date_from': date_from.isoformat() if date_from else '',
                    'date_to': date_to.isoformat() if date_to else '',
                    'agency_id': agency_id or '',
                    'status': status.value if status else '',
                    'search': search or '',
                },
            ),
        )

    @application.get('/contacts', response_class=HTMLResponse, name='contacts', dependencies=[Depends(require_viewer)])
    def contacts(
        request: Request,
        search: str | None = None,
        agency_id: str | None = None,
        org_unit_id: str | None = None,
        contact_type: ContactType | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            agencies_page = AgencyService(session).list_page(page_size=500)
            units_query = session.query(OrgUnit).filter(OrgUnit.active.is_(True))
            if _uuid_or_none(agency_id):
                units_query = units_query.filter(OrgUnit.agency_id == _uuid_or_none(agency_id))
            units = list(units_query.order_by(OrgUnit.name).limit(500))
            workspace = ContactService(session).list_page(
                search=search,
                agency_id=_uuid_or_none(agency_id),
                org_unit_id=_uuid_or_none(org_unit_id),
                contact_type=contact_type,
                page=page,
                page_size=page_size,
            )
            db_error = None
        except SQLAlchemyError:
            session.rollback()
            agencies_page, units = {'items': ()}, []
            workspace = {'items': (), 'details': ()}
            db_error = '확정 연락처를 불러오지 못했습니다. 데이터베이스 연결 상태를 확인하세요.'
        finally:
            session.close()
        return templates.TemplateResponse(
            request=request, name='contacts.html',
            context=_page_context(request,
                'contacts', workspace=workspace, db_error=db_error,
                agencies=agencies_page['items'], org_units=units,
                contact_types=ContactType,
                filters={
                    'search': search or '', 'agency_id': agency_id or '',
                    'org_unit_id': org_unit_id or '',
                    'contact_type': contact_type.value if contact_type else '',
                },
            ),
        )

    @application.get('/review', response_class=HTMLResponse, name='review', dependencies=[Depends(require_viewer)])
    def review(
        request: Request,
        search: str | None = None,
        agency_id: str | None = None,
        review_status: ReviewStatus | None = None,
        change_type: ChangeEventType | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            agencies_page = AgencyService(session).list_page(page_size=500)
            workspace = ReviewReadService(session).list_page(
                search=search, agency_id=_uuid_or_none(agency_id),
                review_status=review_status, change_type=change_type,
                page=page, page_size=page_size,
            )
            db_error = None
        except SQLAlchemyError:
            session.rollback()
            agencies_page = {'items': ()}
            workspace = {'items': (), 'details': ()}
            db_error = '검토 대상을 불러오지 못했습니다. 데이터베이스 연결 상태를 확인하세요.'
        finally:
            session.close()
        return templates.TemplateResponse(
            request=request, name='review.html',
            context=_page_context(request,
                'review', workspace=workspace, db_error=db_error,
                agencies=agencies_page['items'],
                review_statuses=ReviewStatus, change_types=ChangeEventType,
                filters={
                    'search': search or '', 'agency_id': agency_id or '',
                    'review_status': review_status.value if review_status else '',
                    'change_type': change_type.value if change_type else '',
                },
            ),
        )

    @application.get('/settings', response_class=HTMLResponse, name='settings', dependencies=[Depends(require_admin)])
    def settings(request: Request):
        session = application.state.session_factory()
        try:
            snapshot = SettingsService(session).snapshot()
            next_refresh = application.state.collection_scheduler.next_run()
            users = UserService(session).list_users()
            db_error = None
        except SQLAlchemyError:
            session.rollback()
            snapshot, users, next_refresh = None, (), None
            db_error = '설정 데이터를 불러오지 못했습니다. 데이터베이스 연결 상태를 확인하세요.'
        finally:
            session.close()
        paths = application.state.runtime_paths
        portable_paths = (
            ('DB', paths.database_path), ('RAW', paths.raw_root), ('imports', paths.import_root),
            ('exports', paths.export_root), ('temp', paths.temp_root), ('logs', paths.log_root),
            ('backups', paths.backup_root), ('config', paths.config_root),
        )
        return templates.TemplateResponse(
            request=request, name='settings.html',
            context=_page_context(request, 'settings', settings=snapshot, users=users,
                                  portable_paths=portable_paths, regions=REGIONS,
                                  next_refresh=next_refresh,
                                  next_refresh_display=format_kst_datetime(
                                      next_refresh, fallback='자동 수집 사용 안 함'
                                  ),
                                  db_error=db_error),
        )

    return application


app = create_app()
