from fastapi.testclient import TestClient

from app.db.base import Base
from app.db.engine import create_db_engine
from app.main import WORKSPACE_PAGES
from app.services.dashboard_service import _points
from tests.support import regression_app


def make_app(tmp_path):
    database = tmp_path / 'app.sqlite3'
    url = f'sqlite:///{database.as_posix()}'
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return regression_app(url, project_root=tmp_path)


def test_dashboard_and_static_assets_render(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        response = client.get('/')
        css = client.get('/static/css/dashboard.css')
        script = client.get('/static/js/app.js')
    assert response.status_code == 200
    assert '운영 대시보드' in response.text
    assert '샘플 데이터' not in response.text
    assert '최근 수집 현황' in response.text
    assert '최근 검토 대상' in response.text
    assert '수집 이력이 없습니다.' in response.text
    assert css.status_code == 200 and script.status_code == 200


def test_chart_points_share_safe_zero_baseline():
    assert len(_points([0, 10]).split()) == 2
    assert {point.split(',')[1] for point in _points([0, 0]).split()} == {'140'}


def test_health_has_no_database_claim(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        response = client.get('/health')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'app': 'PublicDB2'}
    assert 'path' not in response.text.casefold()


def test_all_workspace_routes_render_without_placeholders(tmp_path):
    required = {
        'agencies': ('기관/조직', '기관 추가'),
        'sources': ('수집 소스', '엑셀 업로드', 'URL 추가'),
        'runs': ('수집 이력', '조건에 맞는 수집 이력이 없습니다'),
        'contacts': ('연락처 DB', '엑셀 내보내기', '공식 출처'),
        'review': ('변경/검토', 'CURRENT VALUE', 'DISCOVERED VALUE'),
        'settings': ('설정', '수집 기본값', '사용자 관리', '프로젝트 소유 경로'),
    }
    with TestClient(make_app(tmp_path)) as client:
        for route, markers in required.items():
            response = client.get('/' + route)
            assert response.status_code == 200
            for marker in markers:
                assert marker in response.text


def test_workspace_templates_and_static_assets_are_registered(tmp_path):
    assert set(WORKSPACE_PAGES) == {'agencies', 'sources', 'runs', 'contacts', 'review', 'settings'}
    with TestClient(make_app(tmp_path)) as client:
        workspace_css = client.get('/static/css/workspace.css')
        script = client.get('/static/js/app.js')
        dashboard = client.get('/')
        sources = client.get('/sources')
    assert workspace_css.status_code == 200 and script.status_code == 200
    assert 'data-modal="source-create"' in sources.text
    assert 'data-modal="excel-import"' in sources.text
    assert 'workspace.css' not in dashboard.text
    assert 'kpi-grid' in dashboard.text
