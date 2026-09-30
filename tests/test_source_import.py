from __future__ import annotations

import csv
import hashlib
import io
import socket
import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select

from app.core.config import runtime_paths
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from tests.support import regression_app
from app.models import Agency, AgencyType, OrgUnit, OrgUnitType, Source, SourceBinding, SourceImportLog
from app.services.agency_service import AgencyService
from app.services.source_import_service import MAX_IMPORT_ROWS, PreviewStore, SourceImportError, SourceImportService, parse_import_file
from app.services.source_service import SourceService


@pytest.fixture()
def import_env(tmp_path, monkeypatch):
    db_path = tmp_path / "db" / "test.sqlite3"
    db_path.parent.mkdir()
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield tmp_path, url, factory
    engine.dispose()


@pytest.fixture()
def session(import_env):
    with import_env[2]() as value:
        yield value


def csv_bytes(rows, headers=("기관명", "부서명", "URL", "소스 설명")):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def xlsx_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["기관명", "부서명", "URL", "소스 설명"])
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def existing_context(session):
    agencies = AgencyService(session)
    first, _ = agencies.create_agency(official_name="가 기관", agency_type=AgencyType.OTHER)
    second, _ = agencies.create_agency(official_name="나 기관", agency_type=AgencyType.OTHER)
    first_id, second_id = uuid.UUID(first["id"]), uuid.UUID(second["id"])
    first_unit = agencies.create_org_unit(agency_id=first_id, name="공통부서", unit_type=OrgUnitType.DEPARTMENT)
    second_unit = agencies.create_org_unit(agency_id=second_id, name="공통부서", unit_type=OrgUnitType.DEPARTMENT)
    return first_id, uuid.UUID(first_unit["id"]), second_id, uuid.UUID(second_unit["id"])


def test_xlsx_and_csv_valid_preview_write_zero_rows(session):
    agency_id, _, _, _ = existing_context(session)
    before = {
        model: session.scalar(select(func.count()).select_from(model))
        for model in (Agency, OrgUnit, Source, SourceBinding, SourceImportLog)
    }
    service = SourceImportService(session)
    for name, content in [
        ("sources.xlsx", xlsx_bytes([["가 기관", "", "https://example.com/a", "설명"]])),
        ("sources.csv", csv_bytes([["가 기관", "", "https://example.com/b", "설명"]])),
    ]:
        preview = service.preview(name, content)
        assert preview["rows"][0]["classification"] == "READY"
        assert preview["rows"][0]["agency_id"] == str(agency_id)
    after = {model: session.scalar(select(func.count()).select_from(model)) for model in before}
    assert after == before


def test_input_validation_and_row_limit(session):
    service = SourceImportService(session)
    with pytest.raises(SourceImportError, match="필수 컬럼"):
        service.preview("bad.csv", csv_bytes([["기관", "https://example.com"]], headers=("부서명", "설명")))
    preview = service.preview("empty.csv", csv_bytes([["", "", "", "설명만 있음"]]))
    assert preview["rows"][0]["classification"] == "INVALID"
    assert service.preview("url.csv", csv_bytes([["기관", "", "http://127.0.0.1/a", ""]]))["rows"][0]["classification"] == "INVALID"
    too_many = csv_bytes([["기관", "", f"https://example.com/{number}", ""] for number in range(MAX_IMPORT_ROWS + 1)])
    with pytest.raises(SourceImportError, match="최대"):
        parse_import_file("large.csv", too_many)


def test_all_row_classifications_and_department_scope(session):
    first_id, first_unit, second_id, second_unit = existing_context(session)
    source_service = SourceService(session)
    source_service.register_binding(url="https://example.com/existing", agency_id=first_id, org_unit_id=first_unit)
    rows = [
        ["가 기관", "", "https://example.com/ready", ""],
        ["새 기관", "", "https://example.com/new-agency", ""],
        ["가 기관", "새 부서", "https://example.com/new-unit", ""],
        ["가 기관", "공통부서", "https://example.com/existing", ""],
        ["나 기관", "공통부서", "https://example.com/existing", ""],
        ["가 기관", "", "https://example.com/ready#duplicate", ""],
        ["가 기관", "", "ftp://example.com/bad", ""],
    ]
    preview = SourceImportService(session).preview("all.csv", csv_bytes(rows))
    categories = [row["classification"] for row in preview["rows"]]
    assert categories == ["READY", "NEW_AGENCY", "NEW_ORG_UNIT", "EXACT_DUPLICATE", "EXISTING_SOURCE_NEW_BINDING", "EXACT_DUPLICATE", "INVALID"]
    assert preview["rows"][4]["org_unit_id"] == str(second_unit)
    assert preview["rows"][4]["org_unit_id"] != str(first_unit)


def test_conflict_classification_is_not_importable(session):
    session.add_all([
        Agency(official_name="충돌 기관 A", normalized_name="충돌 기관", agency_type=AgencyType.OTHER),
        Agency(official_name="충돌 기관 B", normalized_name="충돌 기관", agency_type=AgencyType.PUBLIC_INSTITUTION),
    ])
    session.commit()
    row = SourceImportService(session).preview("conflict.csv", csv_bytes([["충돌 기관", "", "https://example.com/c", ""]]))["rows"][0]
    assert row["classification"] == "CONFLICT"
    assert row["importable"] is False


def test_confirm_skips_conflict_rows(import_env):
    root, _, factory = import_env
    content = csv_bytes([["충돌 기관", "", "https://example.com/conflict", ""]])
    with factory() as session:
        session.add_all([
            Agency(official_name="충돌 기관 A", normalized_name="충돌 기관", agency_type=AgencyType.OTHER),
            Agency(official_name="충돌 기관 B", normalized_name="충돌 기관", agency_type=AgencyType.PUBLIC_INSTITUTION),
        ])
        session.commit()
        store = PreviewStore(runtime_paths(root))
        entry = store.save("conflict.csv", content)
        result = SourceImportService(session, runtime_paths(root)).confirm(entry)
        store.discard(entry.token)
        assert result["summary"]["conflicts"] == 1
        assert result["summary"]["created_sources"] == 0
        assert result["summary"]["created_bindings"] == 0


def test_confirm_partial_success_reuses_source_and_retains_file(import_env, monkeypatch):
    root, url, factory = import_env
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network call"))
    content = csv_bytes([
        ["새 기관", "새 부서", "https://example.com/shared#one", "첫 행"],
        ["다른 기관", "", "https://example.com/shared", "둘째 행"],
        ["오류 기관", "", "ftp://example.com/bad", ""],
    ])
    paths = runtime_paths(root)
    with factory() as session:
        store = PreviewStore(paths)
        entry = store.save("original.csv", content)
        preview = SourceImportService(session, paths).preview(entry.original_filename, content)
        assert preview["summary"]["importable"] == 2
        result = SourceImportService(session, paths).confirm(entry)
        store.discard(entry.token)
        assert result["summary"]["created_agencies"] == 2
        assert result["summary"]["created_org_units"] == 1
        assert result["summary"]["created_sources"] == 1
        assert result["summary"]["created_bindings"] == 2
        assert result["summary"]["invalid_rows"] == 1
        assert session.scalar(select(func.count()).select_from(Source)) == 1
        assert session.scalar(select(func.count()).select_from(SourceBinding)) == 2
        log = session.scalar(select(SourceImportLog))
        assert log.original_filename == "original.csv"
        assert not log.stored_path.startswith(("/", "\\"))
        assert log.sha256 == hashlib.sha256(content).hexdigest()
        assert (root / log.stored_path).read_bytes() == content
        assert not entry.temp_path.exists()


def test_stale_preview_revalidated_and_exact_binding_not_duplicated(import_env):
    root, _, factory = import_env
    content = csv_bytes([["가 기관", "", "https://example.com/stale", ""]])
    with factory() as session:
        agency, _ = AgencyService(session).create_agency(official_name="가 기관", agency_type=AgencyType.OTHER)
        agency_id = uuid.UUID(agency["id"])
        store = PreviewStore(runtime_paths(root))
        entry = store.save("stale.csv", content)
        assert SourceImportService(session, runtime_paths(root)).preview("stale.csv", content)["rows"][0]["classification"] == "READY"
        SourceService(session).register_binding(url="https://example.com/stale", agency_id=agency_id)
        result = SourceImportService(session, runtime_paths(root)).confirm(entry)
        store.discard(entry.token)
        assert result["summary"]["duplicates_skipped"] == 1
        assert result["summary"]["created_bindings"] == 0
        assert session.scalar(select(func.count()).select_from(SourceBinding)) == 1


def test_preview_store_cleanup_and_portable_paths(tmp_path):
    paths = runtime_paths(tmp_path)
    store = PreviewStore(paths)
    entry = store.save("x.csv", csv_bytes([["기관", "", "https://example.com", ""]]))
    assert entry.temp_path.is_relative_to(tmp_path)
    store.discard(entry.token)
    assert not entry.temp_path.exists()
    for value in vars(paths).values():
        assert str(value).startswith(str(tmp_path))
    defaults = runtime_paths()
    assert defaults.project_root.name == "PublicDB2"
    assert defaults.project_root != defaults.project_root.parent / "PublicDB"
    assert all(part not in str(defaults.project_root) for part in ("C:\\Users\\USER", "Desktop", "Documents", "AppData"))


def test_import_api_template_live_sources_and_routes(import_env):
    root, url, _ = import_env
    app = regression_app(url, project_root=root)
    with TestClient(app) as client:
        template = client.get("/api/source-bindings/imports/template.csv")
        preview = client.post("/api/source-bindings/imports/preview", files={"file": ("api.xlsx", xlsx_bytes([["API 기관", "", "https://example.com/api", ""]]), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        token = preview.json()["token"]
        confirmed = client.post(f"/api/source-bindings/imports/{token}/confirm")
        sources = client.get("/sources")
        routes = [client.get(path) for path in ["/", "/agencies", "/sources", "/runs", "/contacts", "/review", "/settings"]]
    assert template.status_code == 200
    assert template.content.startswith(b"\xef\xbb\xbf")
    assert preview.status_code == 200
    assert confirmed.status_code == 200
    assert "https://example.com/api" in sources.text
    assert all(response.status_code == 200 for response in routes)
    assert "샘플 데이터" not in routes[0].text
    assert "data-import-preview" in sources.text
    assert "수집</button>" in sources.text and "disabled" in sources.text

