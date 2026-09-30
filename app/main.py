"""FastAPI entrypoint for the PublicDB2 UI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
import logging
import uuid

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.agencies import router as agencies_api
from app.api.collection import router as collection_api
from app.api.master_review import router as master_review_api
from app.api.operations import router as operations_api
from app.api.sources import router as sources_api
from app.api.dependencies import ensure_csrf_token, get_current_user_optional, get_session, require_admin, require_viewer
from app.collectors.http_fetcher import HTTPFetcher
from app.core.config import allowed_hosts, runtime_paths
from app.core.logging import close_file_logging, configure_file_logging
from app.core.security import SecretStore, SecurityHeadersMiddleware, SignedSessionMiddleware
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    AgencyType, ChangeEventType, ContactType, OrgUnit, ReviewStatus, RunStatus, User,
)
from app.services.agency_service import AgencyService
from app.services.collection_service import CollectionCoordinator, CollectionService
from app.services.contact_service import ContactService
from app.services.dashboard_service import DashboardService
from app.services.review_read_service import ReviewReadService
from app.services.run_service import RunService
from app.services.settings_service import SettingsService
from app.services.source_service import SourceService
from app.services.source_import_service import PreviewStore
from app.services.user_service import UserService


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
    try:
        yield
    finally:
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
    engine = create_db_engine(database_url)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    application.state.runtime_paths = runtime_paths(project_root)
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
    application.include_router(agencies_api)
    application.include_router(sources_api)
    application.include_router(collection_api)
    application.include_router(master_review_api)
    application.include_router(operations_api)
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
            if revision != 'a5c105a05a01':
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
    def agencies(request: Request, search: str | None = None, agency_type: AgencyType | None = None) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            workspace = AgencyService(session).list_page(search=search, agency_type=agency_type)
            db_error = None
        except SQLAlchemyError:
            session.rollback()
            workspace = {'items': (), 'details': ()}
            db_error = '기관 데이터를 불러오지 못했습니다. 데이터베이스 마이그레이션과 연결 상태를 확인하세요.'
        finally:
            session.close()
        return templates.TemplateResponse(request=request, name='agencies.html', context=_page_context(request, 'agencies', workspace=workspace, db_error=db_error, filters={'search': search or '', 'agency_type': agency_type.value if agency_type else ''}, agency_types=AgencyType))

    @application.get('/sources', response_class=HTMLResponse, name='sources', dependencies=[Depends(require_viewer)])
    def sources(request: Request, search: str | None = None, agency_id: str | None = None, org_unit_id: str | None = None, source_status: str | None = None) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            options = AgencyService(session).list_page()
            units = list(session.query(OrgUnit).filter(OrgUnit.active.is_(True)).order_by(OrgUnit.name))
            workspace = SourceService(session).list_page(search=search, agency_id=_uuid_or_none(agency_id), org_unit_id=_uuid_or_none(org_unit_id), status=source_status)
            db_error = None
        except SQLAlchemyError:
            session.rollback()
            options, units = {'items': (), 'details': ()}, []
            workspace = {'items': (), 'details': ()}
            db_error = '수집 소스 데이터를 불러오지 못했습니다. 데이터베이스 마이그레이션과 연결 상태를 확인하세요.'
        finally:
            session.close()
        return templates.TemplateResponse(request=request, name='sources.html', context=_page_context(request, 'sources', workspace=workspace, db_error=db_error, agencies=options['items'], org_units=units, filters={'search': search or '', 'agency_id': agency_id or '', 'org_unit_id': org_unit_id or '', 'source_status': source_status or ''}))

    @application.get('/runs', response_class=HTMLResponse, name='runs', dependencies=[Depends(require_viewer)])
    def runs(
        request: Request,
        date_from: date | None = None,
        date_to: date | None = None,
        agency_id: str | None = None,
        status: RunStatus | None = None,
        search: str | None = None,
    ) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            agencies_page = AgencyService(session).list_page()
            workspace = RunService(session).list_page(
                date_from=date_from,
                date_to=date_to,
                agency_id=_uuid_or_none(agency_id),
                status=status,
                search=search,
            )
            db_error = None
        except SQLAlchemyError:
            session.rollback()
            agencies_page = {'items': ()}
            workspace = {'items': (), 'details': ()}
            db_error = '수집 이력을 불러오지 못했습니다. 데이터베이스 마이그레이션과 연결 상태를 확인하세요.'
        finally:
            session.close()
        return templates.TemplateResponse(
            request=request,
            name='runs.html',
            context=_page_context(request,
                'runs',
                workspace=workspace,
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
    ) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            agencies_page = AgencyService(session).list_page()
            units = list(session.query(OrgUnit).filter(OrgUnit.active.is_(True)).order_by(OrgUnit.name))
            workspace = ContactService(session).list_page(
                search=search,
                agency_id=_uuid_or_none(agency_id),
                org_unit_id=_uuid_or_none(org_unit_id),
                contact_type=contact_type,
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
    ) -> HTMLResponse:
        session = application.state.session_factory()
        try:
            agencies_page = AgencyService(session).list_page()
            workspace = ReviewReadService(session).list_page(
                search=search, agency_id=_uuid_or_none(agency_id),
                review_status=review_status, change_type=change_type,
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
            users = UserService(session).list_users()
            db_error = None
        except SQLAlchemyError:
            session.rollback()
            snapshot, users = None, ()
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
                                  portable_paths=portable_paths, db_error=db_error),
        )

    return application


app = create_app()
