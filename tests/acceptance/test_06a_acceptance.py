from __future__ import annotations

import shutil
import sys
import re
import threading
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import app.services.api_credential_store as credential_store_module
import app.services.contact_export_service as contact_export_module
import app.services.source_import_service as source_import_module
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from app.collectors.http_fetcher import HTTPFetcher, UnsafeRequestTarget
from app.core.schema import MIGRATION_HEAD
from app.core.config import runtime_paths
from app.core.security import SecretStore
from app.db.base import Base
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.main import create_app
from app.models import (
    Agency, AgencyType, ChangeEvent, ContactHistory, ContactPoint, ContactType, CrawlRun,
    DetectedChangeCandidate, Duty, EntityType, ExtractionRun, OperationClaim, OrgUnit, OrgUnitType,
    ReviewStatus, RunStatus, Source, SourceBinding, SourceImportLog, SourceOccurrence, StageStatus,
    User, UserRole,
)
from app.models.common import utc_now
from app.services.agency_service import AgencyService
from app.services.api_credential_store import ApiCredentialStore
from app.services.collection_recovery_service import (
    INTERRUPTION_REASON, reconcile_stale_collections,
)
from app.services.collection_service import (
    CollectionBusyError, CollectionCoordinator, CollectionService,
)
from app.services.contact_export_service import ContactExportService
from app.services.contact_service import ContactService
from app.services.dashboard_service import DashboardService
from app.services.master_promotion_apply_service import MasterPromotionApplyService
from app.services.review_service import ReviewConflict, ReviewService
from app.services.source_change_detection_service import SourceChangeDetectionService
from app.services.source_import_service import PreviewStore, SourceImportService
from app.services.source_method_service import MethodConfigError, SourceMethodService
from app.services.source_service import SourceService
from app.services.settings_service import SettingsService
from app.services.user_service import UserService
from scripts import manage_user
from tests.test_04b_master_review import add_context, add_extraction
from tests.test_collection_pipeline import HTML
from tests.test_source_import import csv_bytes


PUBLIC_DNS = lambda _host: {"93.184.216.34"}
DIRECTORY = (
    "<html><body><table><tr><th>부서</th><th>업무</th><th>전화</th></tr>"
    "<tr><td>데이터부</td><td>공개업무</td><td>02-1234-5678</td></tr>"
    "</table></body></html>"
).encode()


@pytest.fixture()
def acceptance_db(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    database = root / "data" / "db" / "publicdb2.sqlite3"
    database.parent.mkdir(parents=True)
    url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_PROJECT_ROOT", str(root))
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield root, url, factory
    engine.dispose()


def _source(factory, url="https://example.org/staff"):
    with factory() as session:
        agency, _ = AgencyService(session).create_agency(
            official_name=f"Acceptance-{uuid.uuid4()}", agency_type=AgencyType.OTHER
        )
        binding, _ = SourceService(session).register_binding(
            url=url, agency_id=uuid.UUID(agency["id"])
        )
        return uuid.UUID(binding["source_id"])


def _collector(session, root, handler):
    return CollectionService(
        session,
        project_root=root,
        raw_root=root / "data" / "raw",
        coordinator=CollectionCoordinator(),
        fetcher=HTTPFetcher(transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS),
    )


def test_stale_running_recovery_is_terminal_and_next_collects(acceptance_db):
    root, _url, factory = acceptance_db
    source_id = _source(factory)
    old = utc_now() - timedelta(minutes=20)
    with factory() as session:
        stale = CrawlRun(
            source_id=source_id, status=RunStatus.RUNNING,
            connection_status=StageStatus.PENDING, raw_status=StageStatus.PENDING,
            extraction_status=StageStatus.PENDING, started_at=old, heartbeat_at=old,
        )
        session.add(stale)
        session.commit()
        stale_id = stale.id
    with factory() as session:
        assert reconcile_stale_collections(session) == 1
        recovered = session.get(CrawlRun, stale_id)
        assert recovered.status is RunStatus.FAILED
        assert recovered.finished_at is not None
        assert recovered.error_summary == INTERRUPTION_REASON
        assert recovered.observations == []
        result = _collector(
            session, root,
            lambda _request: httpx.Response(
                200, headers={"Content-Type": "text/html"}, content=DIRECTORY
            ),
        ).collect(source_id)
        assert result.crawl_run.status is RunStatus.SUCCESS


def test_cross_instance_collection_claim_accepts_exactly_one(acceptance_db):
    root, _url, factory = acceptance_db
    source_id = _source(factory)
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    result = {}

    def blocking_handler(_request):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=DIRECTORY)

    def first():
        with factory() as session:
            result["first"] = _collector(session, root, blocking_handler).collect(source_id)

    worker = threading.Thread(target=first)
    worker.start()
    assert entered.wait(5)
    with factory() as session:
        with pytest.raises(CollectionBusyError):
            _collector(
                session, root,
                lambda _request: pytest.fail("rejected collection performed HTTP"),
            ).collect(source_id)
    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert calls == 1
    assert result["first"].crawl_run.status is RunStatus.SUCCESS
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CrawlRun)) == 1
        assert session.scalar(select(func.count()).select_from(OperationClaim).where(
            OperationClaim.status == "ACTIVE"
        )) == 0


def test_method_edit_rejected_while_run_active_and_snapshot_survives(acceptance_db):
    root, _url, factory = acceptance_db
    source_id = _source(factory)
    entered = threading.Event()
    release = threading.Event()

    def blocking_handler(_request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=DIRECTORY)

    errors = []

    def collect():
        try:
            with factory() as session:
                _collector(session, root, blocking_handler).collect(source_id)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=collect)
    worker.start()
    assert entered.wait(5)
    with factory() as session:
        source = session.get(Source, source_id)
        with pytest.raises(MethodConfigError):
            SourceMethodService(session).configure(
                source, "WEB_CRAWL",
                {"allowed_path": "/", "max_depth": 1, "max_pages": 2, "request_delay_ms": 500},
            )
    release.set()
    worker.join(10)
    assert not errors
    with factory() as session:
        run = session.scalar(select(CrawlRun).where(CrawlRun.source_id == source_id))
        assert run.collection_method_snapshot.value == "WEB_PAGE"
        assert run.collection_config_snapshot["target_url"] == "https://example.org/staff"


def test_baseline_and_review_duplicate_mutations_are_idempotent(acceptance_db):
    _root, _url, factory = acceptance_db
    with factory() as session:
        agency_id, source_id, _unit_id = add_context(session)
        extraction_id, _observation_id, _record_id = add_extraction(session, source_id)
    barrier = threading.Barrier(2)
    baseline_results = []
    baseline_errors = []

    def baseline():
        try:
            with factory() as session:
                barrier.wait()
                baseline_results.append(
                    MasterPromotionApplyService(session).apply(extraction_id, agency_id)
                )
        except Exception as error:
            baseline_errors.append(error)

    threads = [threading.Thread(target=baseline) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not baseline_errors
    assert sorted(result["reused"] for result in baseline_results) == [False, True]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(OrgUnit).where(OrgUnit.name == "Digital Office")) == 1
        assert session.scalar(select(func.count()).select_from(Duty).where(Duty.title == "Public Data")) == 1
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 2
        changed, _observation, _record = add_extraction(
            session, source_id, phone="02-9999-0000", email="team@example.org"
        )
        SourceChangeDetectionService(session).generate(changed, agency_id)
        candidate_id = session.scalar(select(DetectedChangeCandidate.id).where(
            DetectedChangeCandidate.review_status == ReviewStatus.PENDING_REVIEW,
            DetectedChangeCandidate.existing_entity_id.is_not(None),
        ))
        before_events = session.scalar(select(func.count()).select_from(ChangeEvent))
        before_history = session.scalar(select(func.count()).select_from(ContactHistory))
    barrier = threading.Barrier(2)
    review_results = []
    review_errors = []

    def approve():
        try:
            with factory() as session:
                barrier.wait()
                review_results.append(ReviewService(session).approve(candidate_id))
        except Exception as error:
            review_errors.append(error)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not review_errors
    assert sorted(result["reused"] for result in review_results) == [False, True]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ChangeEvent)) == before_events + 1
        assert session.scalar(select(func.count()).select_from(ContactHistory)) == before_history + 1


def test_source_import_double_confirm_has_one_effect(acceptance_db):
    root, _url, factory = acceptance_db
    paths = runtime_paths(root)
    store = PreviewStore(paths)
    content = csv_bytes([["Import Agency", "", "https://example.org/import", "source"]])
    entry = store.save("sources.csv", content)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def confirm():
        try:
            with factory() as session:
                barrier.wait()
                results.append(SourceImportService(session, paths).confirm(entry))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    assert sorted(result["reused"] for result in results) == [False, True]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Agency)) == 1
        assert session.scalar(select(func.count()).select_from(Source)) == 1
        assert session.scalar(select(func.count()).select_from(SourceBinding)) == 1
        assert session.scalar(select(func.count()).select_from(SourceImportLog)) == 1


def test_review_approve_vs_keep_and_defer_races_have_one_mutation_path(acceptance_db):
    _root, _url, factory = acceptance_db

    def candidate(index):
        with factory() as session:
            agency_id, source_id, _unit_id = add_context(
                session, url=f"https://example.org/race/{index}", agency_name=f"Race Agency {index}"
            )
            baseline_id, _observation_id, _record_id = add_extraction(session, source_id)
            MasterPromotionApplyService(session).apply(baseline_id, agency_id)
            changed_id, _observation_id, _record_id = add_extraction(
                session, source_id, phone=f"02-9999-{index:04d}"
            )
            detection = SourceChangeDetectionService(session).generate(changed_id, agency_id)
            return session.scalar(select(DetectedChangeCandidate.id).where(
                DetectedChangeCandidate.detection_run_id == uuid.UUID(detection["detection_id"]),
                DetectedChangeCandidate.existing_entity_id.is_not(None),
            ))

    for index, competing in enumerate(("reject", "defer"), start=1):
        candidate_id = candidate(index)
        with factory() as session:
            before_events = session.scalar(select(func.count()).select_from(ChangeEvent))
            before_history = session.scalar(select(func.count()).select_from(ContactHistory))
            before_active_contacts = session.scalar(select(func.count()).select_from(ContactPoint).where(ContactPoint.active.is_(True)))
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def act(action):
            try:
                with factory() as session:
                    barrier.wait()
                    results.append(getattr(ReviewService(session), action)(candidate_id))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=act, args=(action,)) for action in ("approve", competing)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert all(not thread.is_alive() for thread in threads)
        assert results
        with factory() as session:
            resolved = session.get(DetectedChangeCandidate, candidate_id)
            assert resolved.review_status in {
                ReviewStatus.APPROVED,
                ReviewStatus.REJECTED if competing == "reject" else ReviewStatus.DEFERRED,
            }
            assert session.scalar(select(func.count()).select_from(ChangeEvent)) - before_events <= 1
            assert session.scalar(select(func.count()).select_from(ContactHistory)) - before_history <= 1
            assert session.scalar(select(func.count()).select_from(ContactPoint).where(ContactPoint.active.is_(True))) == before_active_contacts


def test_atomic_filesystem_failure_injection_preserves_truth(acceptance_db, monkeypatch):
    root, _url, factory = acceptance_db
    paths = runtime_paths(root)
    with factory() as session:
        agency, _ = AgencyService(session).create_agency(
            official_name="Failure Agency", agency_type=AgencyType.OTHER
        )
        contact = ContactPoint(
            agency_id=uuid.UUID(agency["id"]), contact_type=ContactType.PHONE,
            value="02-1111-2222", normalized_value="0211112222", active=True,
        )
        session.add(contact)
        session.commit()
        prior_export = ContactExportService(session, export_root=paths.export_root).export()
        assert prior_export.is_file()
        original_export_replace = contact_export_module.os.replace
        monkeypatch.setattr(contact_export_module.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("injected")))
        with pytest.raises(OSError, match="injected"):
            ContactExportService(session, export_root=paths.export_root).export()
        assert prior_export.is_file()
        assert not list(paths.export_root.rglob("*.tmp"))
        monkeypatch.setattr(contact_export_module.os, "replace", original_export_replace)

    store = PreviewStore(paths)
    entry = store.save(
        "failure.csv",
        csv_bytes([["Import Failure Agency", "", "https://example.org/import-failure", "source"]]),
    )
    original_import_replace = source_import_module.os.replace
    monkeypatch.setattr(source_import_module.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("injected")))
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(SourceImportLog))
        with pytest.raises(OSError, match="injected"):
            SourceImportService(session, paths).confirm(entry)
        assert session.scalar(select(func.count()).select_from(SourceImportLog)) == before
        assert session.scalar(select(func.count()).select_from(Source).where(
            Source.normalized_url == "https://example.org/import-failure"
        )) == 0
    assert not list(paths.import_root.rglob("*.tmp"))
    monkeypatch.setattr(source_import_module.os, "replace", original_import_replace)
    store.discard(entry.token)

    credentials = ApiCredentialStore(paths.config_root)
    credentials.save("stable-secret", "stable-ref")
    original_credential_replace = credential_store_module.os.replace
    monkeypatch.setattr(credential_store_module.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(OSError, match="injected"):
        credentials.save("replacement-secret", "stable-ref")
    monkeypatch.setattr(credential_store_module.os, "replace", original_credential_replace)
    assert credentials.resolve("stable-ref") == "stable-secret"
    assert not list(paths.config_root.glob(".api-credentials-*.tmp"))


def test_restart_relocation_pragmas_ready_and_relative_state(tmp_path, monkeypatch):
    original = tmp_path / "original"
    database = original / "data" / "db" / "publicdb2.sqlite3"
    database.parent.mkdir(parents=True)
    url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_PROJECT_ROOT", str(original))
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() >= 5000
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
    with factory() as session:
        agency, _ = AgencyService(session).create_agency(
            official_name="Portable Agency", agency_type=AgencyType.OTHER
        )
        SourceService(session).register_binding(
            url="https://example.org/portable", agency_id=uuid.UUID(agency["id"])
        )
    paths = runtime_paths(original)
    secret = SecretStore(paths).get()
    credential_ref = ApiCredentialStore(paths.config_root).save("SENTINEL-06A")["credential_ref"]
    raw_path = paths.raw_root / "manual" / "evidence.html"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text("evidence", encoding="utf-8")
    engine.dispose()
    relocated = tmp_path / "relocated"
    shutil.copytree(original, relocated)
    monkeypatch.delenv("PUBLICDB2_DATABASE_URL")
    monkeypatch.setenv("PUBLICDB2_PROJECT_ROOT", str(relocated))
    app = create_app(project_root=relocated)
    assert relocated.resolve() in Path(app.state.engine.url.database).resolve().parents
    assert SecretStore(runtime_paths(relocated)).get() == secret
    assert ApiCredentialStore(runtime_paths(relocated).config_root).status(credential_ref)["configured"]
    assert (relocated / "data" / "raw" / "manual" / "evidence.html").is_file()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Agency)) == 1
    app.state.engine.dispose()


@pytest.mark.parametrize(
    "url",
    (
        "http://localhost/internal",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/internal",
        "http://192.0.2.1/reserved",
        "http://[::1]/internal",
    ),
)
def test_ssrf_non_public_boundaries_never_reach_transport(url):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with pytest.raises(UnsafeRequestTarget):
        HTTPFetcher(transport=httpx.MockTransport(handler), resolver=PUBLIC_DNS).fetch(url)
    assert calls == 0


def test_migration_from_05b_downgrade_reupgrade_and_metadata(tmp_path, monkeypatch):
    database = tmp_path / "migration-05b.sqlite3"
    url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.upgrade(config, "c7f205b05b01")
    engine = create_db_engine(url)
    assert "operation_claims" not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_db_engine(url)
    assert "operation_claims" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == MIGRATION_HEAD
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    engine.dispose()
    command.downgrade(config, "c7f205b05b01")
    engine = create_db_engine(url)
    assert "operation_claims" not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_db_engine(url)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == MIGRATION_HEAD
    engine.dispose()


def test_manage_user_cli_all_actions_and_last_admin(acceptance_db, monkeypatch, capsys):
    _root, _url, factory = acceptance_db
    passwords = iter(("CorrectHorse12!", "CorrectHorse12!", "SecondCorrectHorse12!"))
    monkeypatch.setattr(manage_user, "password_prompt", lambda: next(passwords))

    def invoke(*arguments):
        monkeypatch.setattr(sys, "argv", ["manage_user.py", *arguments])
        manage_user.main()

    invoke("create", "--username", "admin", "--role", "ADMIN")
    invoke("create", "--username", "operator", "--role", "OPERATOR")
    invoke("list")
    invoke("set-password", "--username", "operator")
    invoke("set-role", "--username", "operator", "--role", "VIEWER")
    invoke("deactivate", "--username", "operator")
    invoke("activate", "--username", "operator")
    with pytest.raises(SystemExit):
        invoke("deactivate", "--username", "admin")
    output = capsys.readouterr().out
    assert "CorrectHorse12!" not in output and "SecondCorrectHorse12!" not in output
    with factory() as session:
        users = {item.username: item for item in session.scalars(select(User))}
        assert set(users) == {"admin", "operator"}
        assert users["admin"].active and users["admin"].role is UserRole.ADMIN
        assert users["operator"].active and users["operator"].role is UserRole.VIEWER
        assert all("CorrectHorse" not in item.password_hash for item in users.values())


def test_coherent_e2e_from_clean_db_through_export_and_logout(acceptance_db):
    root, url, factory = acceptance_db
    paths = runtime_paths(root)
    with factory() as session:
        users = UserService(session)
        users.create(username="e2e-admin", password="CorrectHorse12!", role=UserRole.ADMIN)
        users.create(username="e2e-operator", password="CorrectHorse12!", role=UserRole.OPERATOR)
        users.create(username="e2e-viewer", password="CorrectHorse12!", role=UserRole.VIEWER)
        agencies = AgencyService(session)
        agency, _ = agencies.create_agency(official_name="E2E Agency", agency_type=AgencyType.OTHER)
        agency_id = uuid.UUID(agency["id"])
        unit = agencies.create_org_unit(
            agency_id=agency_id, name="E2E Unit", unit_type=OrgUnitType.DEPARTMENT
        )
        unit_id = uuid.UUID(unit["id"])
        agencies.create_duty(agency_id=agency_id, org_unit_id=unit_id, title="E2E Duty")
        binding, _ = SourceService(session).register_binding(
            url="https://example.org/e2e/staff", agency_id=agency_id, org_unit_id=unit_id,
        )
        source_id = uuid.UUID(binding["source_id"])
        first = _collector(
            session, root,
            lambda _request: httpx.Response(
                200, headers={"Content-Type": "text/html; charset=utf-8"}, content=HTML
            ),
        ).collect(source_id)
        assert first.observation is not None and first.artifact is not None
        assert (root / first.artifact.relative_path).is_file()
        extraction_id = session.scalar(select(ExtractionRun.id).where(
            ExtractionRun.observation_id == first.observation.id,
            ExtractionRun.extractor_name == "staff_directory",
        ))
        preview = MasterPromotionApplyService(session).preview(extraction_id, agency_id)
        assert preview["directory_records"] == 1
        assert preview["contacts_matched"] >= 1
        assert session.scalar(select(func.count()).select_from(ContactPoint)) >= 1
        applied = MasterPromotionApplyService(session).apply(extraction_id, agency_id)
        assert applied["contacts_created"] == 0
        assert applied["contacts_matched"] >= 1
        before_history = session.scalar(select(func.count()).select_from(ContactHistory))
        assert before_history >= 1
        assert session.scalar(select(func.count()).select_from(SourceOccurrence)) >= 1

        changed_html = HTML.replace(b"02-1111-2222", b"02-3333-4444")
        second = _collector(
            session, root,
            lambda _request: httpx.Response(
                200, headers={"Content-Type": "text/html; charset=utf-8"}, content=changed_html
            ),
        ).collect(source_id)
        assert second.crawl_run.status is RunStatus.SUCCESS
        candidate_id = session.scalar(select(DetectedChangeCandidate.id).where(
            DetectedChangeCandidate.review_status == ReviewStatus.PENDING_REVIEW,
            DetectedChangeCandidate.actionable.is_(True),
        ).order_by(DetectedChangeCandidate.created_at))
        assert candidate_id is not None
        reviewed = ReviewService(session).approve(candidate_id, note="06A E2E")
        assert reviewed["status"] == ReviewStatus.APPROVED.value
        assert session.scalar(select(func.count()).select_from(ContactHistory)) >= before_history
        assert ContactService(session).list_page()["items"]
        dashboard = DashboardService(session).read()
        assert dashboard["recent_runs"]
        settings = SettingsService(session).update(
            http_timeout_seconds=9, max_response_bytes=2 * 1024 * 1024,
            user_agent="PublicDB2-06A-E2E",
        )
        assert settings.http_timeout_seconds == 9
        exported = ContactExportService(session, export_root=paths.export_root).export()
        assert exported.is_file()

    app = create_app(database_url=url, project_root=root)
    with TestClient(app) as client:
        login_page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        response = client.post(
            "/login",
            data={"username": "e2e-admin", "password": "CorrectHorse12!", "csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        for route in ("/", "/agencies", "/sources", "/runs", "/contacts", "/review", "/settings"):
            assert client.get(route).status_code == 200
        dashboard_page = client.get("/")
        session_token = re.search(r'name="csrf-token" content="([^"]+)"', dashboard_page.text).group(1)
        assert client.post(
            "/logout", headers={"X-CSRF-Token": session_token}, follow_redirects=False
        ).status_code == 303
        assert client.get("/api/agencies").status_code == 401
    app.state.engine.dispose()
