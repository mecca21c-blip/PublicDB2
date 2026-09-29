from fastapi.testclient import TestClient

from app.main import PLACEHOLDERS, create_app
from app.web.dashboard_fixture import _chart_points


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


def test_required_placeholders_are_minimal_and_routable() -> None:
    with TestClient(create_app()) as client:
        for route_key, (title, _description) in PLACEHOLDERS.items():
            response = client.get(f"/{route_key}")
            assert response.status_code == 200
            assert title in response.text
            assert "구현 예정" in response.text
            assert "data-table" not in response.text
