from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.db.base import Base
from app.db.engine import create_db_engine
from app.main import WORKSPACE_PAGES
from app.models import AgencyType
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


def test_agency_create_modal_presentation_and_contract(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        response = client.get('/agencies')
        workspace_css = client.get('/static/css/workspace.css')

    assert response.status_code == 200
    modal = BeautifulSoup(response.text, 'html.parser').select_one('[data-modal="agency-create"]')
    assert modal is not None
    shell = modal.select_one('.modal-shell.modal-shell--agency-create')
    assert shell is not None
    form = modal.select_one('form.agency-create-form[data-api-form]')
    assert form is not None
    assert form.get('action') == '/api/agencies'
    assert form.get('data-method') == 'POST'

    official_name = form.select_one('[name="official_name"]')
    agency_type = form.select_one('select.control[name="agency_type"]')
    external_identifier = form.select_one('[name="external_identifier"]')
    address = form.select_one('[name="address"]')
    assert official_name is not None and official_name.has_attr('required')
    assert agency_type is not None and agency_type.has_attr('required')
    assert external_identifier is not None and not external_identifier.has_attr('required')
    assert address is not None and not address.has_attr('required')

    options = agency_type.select('option')
    assert options[0].get('value') == ''
    assert options[0].get_text(strip=True) == '기관 유형 선택'
    assert options[0].has_attr('selected') and options[0].has_attr('disabled')
    assert {option.get('value') for option in options[1:]} == {item.value for item in AgencyType}
    assert {option.get('value'): option.get_text(strip=True) for option in options[1:]} == {
        'CENTRAL_GOVERNMENT': '중앙행정기관',
        'AGENCY': '외청/청',
        'COMMISSION': '위원회',
        'METROPOLITAN_GOVERNMENT': '광역지방자치단체',
        'BASIC_LOCAL_GOVERNMENT': '기초지방자치단체',
        'PUBLIC_INSTITUTION': '공공기관',
        'OTHER': '기타',
    }
    assert options[1].get('value') != AgencyType.OTHER.value

    close = modal.select_one('button.modal-close[data-modal-close][aria-label="닫기"]')
    cancel = form.select_one('.modal-actions button[type="button"][data-modal-close]')
    submit = form.select_one('.modal-actions button[type="submit"]')
    assert close is not None and cancel is not None and submit is not None
    assert '공식 관리코드가 있는 경우 입력합니다.' in form.get_text(' ', strip=True)
    assert 'modal-shell--agency-create { width: min(700px, calc(100vw - 48px)); max-width: 100%; }' in workspace_css.text
    assert '.modal-shell { width: min(960px, calc(100vw - 56px));' in workspace_css.text
