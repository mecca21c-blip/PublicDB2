from fastapi.testclient import TestClient

from app.main import WORKSPACE_PAGES, create_app
from app.web.dashboard_fixture import _chart_points
from app.web.workspace_fixtures import WORKSPACE_FIXTURES


def test_dashboard_and_static_assets_render() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/")
        css = client.get("/static/css/dashboard.css")
        script = client.get("/static/js/app.js")

    assert response.status_code == 200
    assert "운영 대시보드" in response.text
    assert "샘플 데이터" in response.text
    assert "최근 수집 현황" in response.text
    assert "최근 검토 대상" in response.text
    assert "수집 소스" in response.text
    assert "소스 등록" in response.text
    assert "Source" not in response.text
    assert "Master" not in response.text
    assert "UI Foundation" not in response.text
    assert "OPERATIONS OVERVIEW" not in response.text
    assert "RECENT COLLECTION" not in response.text
    assert "7-DAY TREND" not in response.text
    assert "REVIEW PENDING" not in response.text
    assert "SHORTCUTS" not in response.text
    assert "알림" not in response.text
    assert "관리자" not in response.text
    assert "status-badge--warning" in response.text
    assert "status-badge--info" in response.text
    assert "status-badge--neutral" in response.text
    assert css.status_code == 200
    assert script.status_code == 200


def test_chart_points_share_scale_and_safe_zero_baseline() -> None:
    success_points = _chart_points((0, 10), 10).split()
    error_points = _chart_points((0, 2), 10).split()
    zero_points = _chart_points((0, 0), 0).split()

    assert success_points[0].split(",")[1] == error_points[0].split(",")[1]
    assert success_points[0].split(",")[1] == "134.0"
    assert success_points[1].split(",")[1] == "16.0"
    assert error_points[1].split(",")[1] == "110.4"
    assert {point.split(",")[1] for point in zero_points} == {"134.0"}


def test_health_has_no_database_claim() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app": "PublicDB2",
        "mode": "ui-foundation",
    }


def test_all_workspace_routes_render_without_placeholders() -> None:
    required_content = {
        "agencies": ("기관/조직", "기관 추가", "조직/부서", "연결된 수집 소스"),
        "sources": ("수집 소스", "엑셀 업로드", "URL 추가", "자료없음", "제외"),
        "runs": ("수집 이력", "RAW 저장", "확정 DB 반영", "추출 오류"),
        "contacts": ("연락처 DB", "엑셀 내보내기", "공식 출처", "최근 변경 이력"),
        "review": ("변경/검토", "CURRENT VALUE", "DISCOVERED VALUE", "반영", "보류"),
        "settings": ("설정", "데이터 저장", "웹 수집 정책", "저장"),
    }

    with TestClient(create_app()) as client:
        for route_key, markers in required_content.items():
            response = client.get(f"/{route_key}")
            assert response.status_code == 200
            assert "구현 예정" not in response.text
            for marker in markers:
                assert marker in response.text


def test_workspace_templates_and_static_assets_are_registered() -> None:
    assert set(WORKSPACE_PAGES) == {
        "agencies",
        "sources",
        "runs",
        "contacts",
        "review",
        "settings",
    }
    assert set(WORKSPACE_FIXTURES) == set(WORKSPACE_PAGES)

    with TestClient(create_app()) as client:
        workspace_css = client.get("/static/css/workspace.css")
        script = client.get("/static/js/app.js")
        dashboard = client.get("/")
        sources = client.get("/sources")

    assert workspace_css.status_code == 200
    assert script.status_code == 200
    assert "data-detail-target" in sources.text
    assert 'data-modal="excel-import"' in sources.text
    assert "workspace.css" not in dashboard.text
    assert "kpi-grid" in dashboard.text
