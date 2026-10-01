import uuid

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.db.base import Base
from app.db.engine import create_db_engine
from app.main import WORKSPACE_PAGES
from app.models import AgencyType, OrgUnitType
from app.services.agency_service import AgencyService
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
        'sources': ('수집 소스', '엑셀 업로드', '수집 소스 추가'),
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


def test_organization_workflow_modals_and_detail_actions(tmp_path):
    app = make_app(tmp_path)
    with app.state.session_factory() as session:
        agencies = AgencyService(session)
        empty_agency, _ = agencies.create_agency(
            official_name='가 부서없는기관', agency_type=AgencyType.OTHER,
        )
        populated_agency, _ = agencies.create_agency(
            official_name='나 부서있는기관', agency_type=AgencyType.PUBLIC_INSTITUTION,
        )
        unit = agencies.create_org_unit(
            agency_id=uuid.UUID(populated_agency['id']),
            name='민원부서',
            unit_type=OrgUnitType.DEPARTMENT,
        )

    with TestClient(app) as client:
        response = client.get('/agencies')
        workspace_css = client.get('/static/css/workspace.css')
    assert response.status_code == 200
    page = BeautifulSoup(response.text, 'html.parser')

    empty_id = empty_agency['id']
    populated_id = populated_agency['id']
    empty_detail = page.select_one(f'[data-detail-id="{empty_id}"]')
    populated_detail = page.select_one(f'[data-detail-id="{populated_id}"]')
    assert empty_detail is not None and populated_detail is not None

    department_action = empty_detail.select_one(f'button.button.button--secondary[data-modal-open="org-{empty_id}"]')
    empty_duty_action = empty_detail.select_one(f'button.button.button--secondary[data-modal-open="duty-{empty_id}"]')
    populated_duty_action = populated_detail.select_one(f'button.button.button--secondary[data-modal-open="duty-{populated_id}"]')
    assert department_action is not None
    assert empty_duty_action is not None and empty_duty_action.has_attr('disabled')
    assert populated_duty_action is not None and not populated_duty_action.has_attr('disabled')
    assert '먼저 부서를 등록하세요.' in empty_detail.get_text(' ', strip=True)
    empty_notes = [node.get_text(strip=True) for node in empty_detail.select('.detail-note--empty')]
    assert '등록된 부서가 없습니다.' in empty_notes
    assert '등록된 업무가 없습니다.' in empty_notes
    assert '연결된 소스가 없습니다.' in empty_notes
    assert not {
        node.get_text(strip=True) for node in empty_detail.select('li')
    } & {
        '등록된 부서가 없습니다.', '등록된 업무가 없습니다.', '연결된 소스가 없습니다.',
    }

    department_modal = page.select_one(f'[data-modal="org-{empty_id}"]')
    assert department_modal is not None
    assert department_modal.select_one('.modal-shell.modal-shell--compact') is not None
    assert department_modal.select_one('button.modal-close[type="button"][data-modal-close][aria-label="닫기"]') is not None
    department_form = department_modal.select_one('form.organization-create-form[data-api-form]')
    assert department_form.get('action') == f'/api/agencies/{empty_id}/org-units'
    assert department_form.get('data-method') == 'POST'
    department_name = department_form.select_one('input.control[name="name"]')
    unit_type = department_form.select_one('input[type="hidden"][name="unit_type"]')
    assert department_name is not None and department_name.has_attr('required')
    assert unit_type is not None and unit_type.get('value') == 'DEPARTMENT'
    assert department_form.select_one('.modal-actions button[type="button"][data-modal-close]') is not None
    assert department_form.select_one('.modal-actions button[type="submit"]') is not None

    duty_modal = page.select_one(f'[data-modal="duty-{populated_id}"]')
    assert duty_modal is not None
    assert duty_modal.select_one('.modal-shell.modal-shell--compact') is not None
    assert duty_modal.select_one('button.modal-close[type="button"][data-modal-close][aria-label="닫기"]') is not None
    duty_form = duty_modal.select_one('form.organization-create-form[data-api-form]')
    assert duty_form.get('action') == f'/api/agencies/{populated_id}/duties'
    assert duty_form.get('data-method') == 'POST'
    department_select = duty_form.select_one('select.control[name="org_unit_id"]')
    duty_title = duty_form.select_one('input.control[name="title"]')
    assert department_select is not None and department_select.has_attr('required')
    assert duty_title is not None and duty_title.has_attr('required')
    options = department_select.select('option')
    assert options[0].get('value') == '' and options[0].get_text(strip=True) == '부서 선택'
    assert options[0].has_attr('selected') and options[0].has_attr('disabled')
    assert options[1].get('value') == unit['id']
    assert duty_form.select_one('.modal-actions button[type="button"][data-modal-close]') is not None
    assert duty_form.select_one('.modal-actions button[type="submit"]') is not None
    assert 'modal-shell--compact { width: min(560px, calc(100vw - 48px)); max-width: 100%; }' in workspace_css.text
