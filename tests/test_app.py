from fastapi.testclient import TestClient

from app.main import PLACEHOLDERS, create_app


def test_dashboard_and_static_assets_render() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/")
        css = client.get("/static/css/dashboard.css")
        script = client.get("/static/js/app.js")

    assert response.status_code == 200
    assert "운영 대시보드" in response.text
    assert "샘플 데이터 · UI 검증용" in response.text
    assert "최근 수집 현황" in response.text
    assert "최근 검토 대상" in response.text
    assert css.status_code == 200
    assert script.status_code == 200


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
