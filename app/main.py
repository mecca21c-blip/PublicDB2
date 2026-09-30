"""FastAPI entrypoint for the PublicDB2 UI."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError

from app.api.agencies import router as agencies_api
from app.api.collection import router as collection_api
from app.api.sources import router as sources_api
from app.core.config import runtime_paths
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import AgencyType, OrgUnit, RunStatus
from app.services.agency_service import AgencyService
from app.services.collection_service import CollectionCoordinator, CollectionService
from app.services.run_service import RunService
from app.services.source_service import SourceService
from app.services.source_import_service import PreviewStore

from app.web.dashboard_fixture import DASHBOARD_FIXTURE
from app.web.workspace_fixtures import WORKSPACE_FIXTURES


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


def _page_context(active_page: str, **values: object) -> dict[str, object]:
    return {"active_page": active_page, "navigation": NAVIGATION, **values}


def _workspace_endpoint(
    templates: Jinja2Templates,
    route_key: str,
    template_name: str,
) -> Callable[[Request], HTMLResponse]:
    def endpoint(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=_page_context(
                route_key,
                workspace=WORKSPACE_FIXTURES[route_key],
            ),
        )

    return endpoint


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def create_app(database_url: str | None = None, project_root: Path | None = None) -> FastAPI:
    """Create the application without schema creation or collection side effects."""

    application = FastAPI(
        title="PublicDB2",
        description="PublicDB2 local administration workspace UI",
    )
    templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
    engine = create_db_engine(database_url)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    application.state.runtime_paths = runtime_paths(project_root)
    application.state.source_import_previews = PreviewStore(application.state.runtime_paths)
    application.state.collection_coordinator = CollectionCoordinator()
    application.state.collection_service_factory = lambda session: CollectionService(
        session,
        project_root=application.state.runtime_paths.project_root,
        raw_root=application.state.runtime_paths.raw_root,
        coordinator=application.state.collection_coordinator,
    )
    application.include_router(agencies_api)
    application.include_router(sources_api)
    application.include_router(collection_api)
    application.mount(
        "/static",
        StaticFiles(directory=str(WEB_ROOT / "static")),
        name="static",
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "app": "PublicDB2", "mode": "agency-source-live"}

    @application.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=_page_context("dashboard", dashboard=DASHBOARD_FIXTURE),
        )

    @application.get('/agencies', response_class=HTMLResponse, name='agencies')
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
        return templates.TemplateResponse(request=request, name='agencies.html', context=_page_context('agencies', workspace=workspace, db_error=db_error, filters={'search': search or '', 'agency_type': agency_type.value if agency_type else ''}, agency_types=AgencyType))

    @application.get('/sources', response_class=HTMLResponse, name='sources')
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
        return templates.TemplateResponse(request=request, name='sources.html', context=_page_context('sources', workspace=workspace, db_error=db_error, agencies=options['items'], org_units=units, filters={'search': search or '', 'agency_id': agency_id or '', 'org_unit_id': org_unit_id or '', 'source_status': source_status or ''}))

    @application.get('/runs', response_class=HTMLResponse, name='runs')
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
            context=_page_context(
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

    for route_key, template_name in WORKSPACE_PAGES.items():
        if route_key in {'agencies', 'sources', 'runs'}:
            continue
        application.add_api_route(
            f"/{route_key}",
            _workspace_endpoint(templates, route_key, template_name),
            methods=["GET"],
            response_class=HTMLResponse,
            name=route_key,
        )

    return application


app = create_app()
