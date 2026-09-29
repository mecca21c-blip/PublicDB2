"""FastAPI entrypoint for the PublicDB2 UI."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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


def create_app() -> FastAPI:
    """Create the UI-only application without DB or collection side effects."""

    application = FastAPI(
        title="PublicDB2",
        description="PublicDB2 local administration workspace UI",
    )
    templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
    application.mount(
        "/static",
        StaticFiles(directory=str(WEB_ROOT / "static")),
        name="static",
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "app": "PublicDB2", "mode": "ui-foundation"}

    @application.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=_page_context("dashboard", dashboard=DASHBOARD_FIXTURE),
        )

    for route_key, template_name in WORKSPACE_PAGES.items():
        application.add_api_route(
            f"/{route_key}",
            _workspace_endpoint(templates, route_key, template_name),
            methods=["GET"],
            response_class=HTMLResponse,
            name=route_key,
        )

    return application


app = create_app()
