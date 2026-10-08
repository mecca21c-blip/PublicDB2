from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    Agency,
    AgencyType,
    CollectionJob,
    CollectionJobStatus,
    CollectionTriggerType,
    RefreshRecurrence,
    Source,
    SourceBinding,
)
from app.services.collection_job_service import CollectionJobService
from app.services.collection_scheduler import next_schedule_slot
from app.services.settings_service import SettingsService, SettingsSnapshot
from tests.support import regression_app


SEOUL = ZoneInfo("Asia/Seoul")


@pytest.fixture()
def operational_env(tmp_path, monkeypatch):
    db_path = tmp_path / "operational-settings.sqlite3"
    database_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(database_url)
    factory = create_session_factory(engine)
    try:
        yield database_url, factory
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("path", "selected_text"),
    (
        ("/review?review_status=", "변경/검토"),
        ("/review?change_type=", "변경/검토"),
        ("/review?review_status=&change_type=", "변경/검토"),
        ("/review?review_status=PENDING_REVIEW", "변경/검토"),
        ("/review?change_type=CONTACT_CHANGED", "변경/검토"),
        ("/agencies?agency_type=", "기관"),
        ("/contacts?contact_type=", "연락처"),
        ("/runs?status=", "수집 이력"),
    ),
)
def test_html_optional_enum_filters_accept_blank_and_valid_values(
    operational_env, path, selected_text,
):
    database_url, _ = operational_env
    response = TestClient(regression_app(database_url)).get(path)

    assert response.status_code == 200
    assert selected_text in response.text
    assert '"detail"' not in response.text


@pytest.mark.parametrize(
    ("path", "safe_path"),
    (
        ("/review?review_status=NOT_A_STATUS", "/review"),
        ("/review?change_type=NOT_A_TYPE", "/review"),
        ("/runs?date_from=not-a-date", "/runs"),
    ),
)
def test_invalid_html_filters_redirect_to_safe_workspace_page(
    operational_env, path, safe_path,
):
    database_url, _ = operational_env
    client = TestClient(regression_app(database_url))

    rejected = client.get(path, follow_redirects=False)
    assert rejected.status_code == 303
    assert rejected.headers["location"] == safe_path

    recovered = client.get(path)
    assert recovered.status_code == 200
    assert recovered.headers["content-type"].startswith("text/html")
    assert '"detail"' not in recovered.text


def test_api_enum_validation_remains_strict(operational_env):
    database_url, _ = operational_env
    response = TestClient(regression_app(database_url)).post(
        "/api/contacts/export?contact_type=NOT_A_TYPE",
        headers={"X-CSRF-Token": "test-csrf"},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")


def test_operational_projection_counts_distinct_eligible_sources_and_latest_full(
    operational_env,
):
    _, factory = operational_env
    with factory() as session:
        agency = Agency(
            official_name="운영 기관",
            normalized_name="운영기관",
            agency_type=AgencyType.OTHER,
        )
        eligible = Source(
            url="https://example.org/eligible",
            normalized_url="https://example.org/eligible",
            scheduled_refresh_enabled=True,
        )
        disabled = Source(
            url="https://example.org/disabled",
            normalized_url="https://example.org/disabled",
            scheduled_refresh_enabled=False,
        )
        inactive = Source(
            url="https://example.org/inactive",
            normalized_url="https://example.org/inactive",
            scheduled_refresh_enabled=True,
            active=False,
        )
        session.add_all((agency, eligible, disabled, inactive))
        session.flush()
        session.add_all((
            SourceBinding(source_id=eligible.id, agency_id=agency.id, scope_key="agency:a"),
            SourceBinding(source_id=eligible.id, agency_id=agency.id, scope_key="agency:b"),
            SourceBinding(source_id=disabled.id, agency_id=agency.id, scope_key="agency:c"),
            SourceBinding(source_id=inactive.id, agency_id=agency.id, scope_key="agency:d"),
        ))
        older = CollectionJob(
            trigger_type=CollectionTriggerType.SCHEDULED_FULL,
            status=CollectionJobStatus.COMPLETED,
            priority=20,
            trigger_context={},
            total_items=3,
            succeeded_items=3,
            failed_items=0,
            skipped_items=0,
            created_at=datetime(2026, 10, 7, 0, 0, tzinfo=SEOUL),
            finished_at=datetime(2026, 10, 7, 0, 1, tzinfo=SEOUL),
        )
        newest = CollectionJob(
            trigger_type=CollectionTriggerType.SCHEDULED_FULL,
            status=CollectionJobStatus.COMPLETED_WITH_ERRORS,
            priority=20,
            trigger_context={},
            total_items=5,
            succeeded_items=4,
            failed_items=1,
            skipped_items=0,
            created_at=datetime(2026, 10, 8, 0, 0, tzinfo=SEOUL),
            finished_at=datetime(2026, 10, 8, 0, 2, tzinfo=SEOUL),
        )
        retry = CollectionJob(
            trigger_type=CollectionTriggerType.SCHEDULED_RETRY,
            status=CollectionJobStatus.COMPLETED,
            priority=30,
            trigger_context={},
            total_items=1,
            succeeded_items=1,
            failed_items=0,
            skipped_items=0,
            created_at=datetime(2026, 10, 8, 1, 0, tzinfo=SEOUL),
        )
        session.add_all((older, newest, retry))
        session.commit()

        jobs = CollectionJobService(session)
        assert jobs.scheduled_source_count() == 1
        latest = jobs.latest_scheduled_full()
        assert latest is not None
        assert latest["id"] == str(newest.id)
        assert latest["status"] == "COMPLETED_WITH_ERRORS"
        assert latest["status_label"] == "완료 · 오류 있음"
        assert latest["succeeded_items"] == 4
        assert latest["failed_items"] == 1
        assert latest["finished_at_display"] == "2026-10-08 00:02"

        projection = SettingsService(session).operational_projection(
            next_run=None, background_runtime_running=True,
        )
        assert projection["next_run_display"] == "자동 수집 사용 안 함"
        assert projection["scheduled_source_count"] == 1
        assert projection["background_runtime_running"] is True
        assert projection["last_scheduled_full"]["id"] == str(newest.id)


@pytest.mark.parametrize(
    ("settings", "expected"),
    (
        (
            SettingsSnapshot(10, 1_000_000, "test", True, RefreshRecurrence.DAILY, None, None, "02:00", True),
            datetime(2026, 10, 9, 2, 0, tzinfo=SEOUL),
        ),
        (
            SettingsSnapshot(10, 1_000_000, "test", True, RefreshRecurrence.WEEKLY, 5, None, "02:00", True),
            datetime(2026, 10, 10, 2, 0, tzinfo=SEOUL),
        ),
        (
            SettingsSnapshot(10, 1_000_000, "test", True, RefreshRecurrence.MONTHLY, None, 31, "02:00", True),
            datetime(2026, 10, 31, 2, 0, tzinfo=SEOUL),
        ),
    ),
)
def test_next_schedule_projection_for_each_recurrence(settings, expected):
    now = datetime(2026, 10, 8, 12, 0, tzinfo=SEOUL)
    assert next_schedule_slot(settings, now) == expected


def test_settings_page_has_one_operational_form_and_conditional_controls(operational_env):
    database_url, _ = operational_env
    response = TestClient(regression_app(database_url)).get("/settings")

    assert response.status_code == 200
    html = response.text
    assert html.index("자동 수집") < html.index("고급 수집 설정")
    assert html.count('data-settings-form') == 1
    assert 'data-auto-enabled' in html
    assert 'data-refresh-recurrence' in html
    assert 'data-schedule-field="weekly"' in html
    assert 'data-schedule-field="monthly"' in html
    assert "자동 수집 사용 안 함" in html
    assert "수집 엔진" in html
    assert "마지막 자동 전체 수집" in html
    assert "아직 자동 수집 기록이 없습니다." in html
