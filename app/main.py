"""FastAPI entrypoint for the PublicDB2 UI foundation."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.web.dashboard_fixture import DASHBOARD_FIXTURE


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

PLACEHOLDERS = {
    "agencies": ("기관/조직", "기관과 조직 정보를 관리하는 화면입니다."),
    "sources": ("수집 소스", "공식 수집 URL과 연결 정보를 관리하는 화면입니다."),
    "runs": ("수집 이력", "수집 실행별 상태와 결과를 확인하는 화면입니다."),
    "contacts": ("연락처 DB", "확정 연락처와 공식 출처를 확인하는 화면입니다."),
    "review": ("변경/검토", "발견된 변경 후보를 비교하고 검토하는 화면입니다."),
    "settings": ("설정", "PublicDB2 운영에 필요한 설정 화면입니다."),
}


def _page_context(active_page: str, **values: object) -> dict[str, object]:
    return {"active_page": active_page, "navigation": NAVIGATION, **values}


def _placeholder_endpoint(
    templates: Jinja2Templates,
    route_key: str,
    title: str,
    description: str,
) -> Callable[[Request], HTMLResponse]:
    def endpoint(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="placeholder.html",
            context=_page_context(
                route_key,
                page_title=title,
                page_description=description,
            ),
        )

    return endpoint


def create_app() -> FastAPI:
    """Create the UI-only application without DB or collection side effects."""

    application = FastAPI(
        title="PublicDB2",
        description="PublicDB2 local administration UI foundation",
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

    for route_key, (title, description) in PLACEHOLDERS.items():
        application.add_api_route(
            f"/{route_key}",
            _placeholder_endpoint(templates, route_key, title, description),
            methods=["GET"],
            response_class=HTMLResponse,
            name=route_key,
        )

    return application


app = create_app()
