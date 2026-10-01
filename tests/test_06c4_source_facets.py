from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text

from app.db.base import Base
from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    Agency, AgencyType, CollectionJob, CollectionJobItemStatus, CollectionMethod,
    CollectionTriggerType, OrgUnit, OrgUnitType, RunStatus, Source, SourceBinding,
)
from app.models.common import utc_now
from app.services.collection_job_service import CollectionJobError, CollectionJobService, SCHEDULED_FULL_PRIORITY
from app.services.collection_job_worker import CollectionJobWorker, WorkerResult
from app.services.collection_service import CollectionBusyError
from app.services.lookup_service import LookupService
from app.services.source_query_service import SourceFilterSpec, SourceQueryService
from app.services.source_service import SourceService, SourceServiceError
from tests.support import regression_app


@pytest.fixture()
def facet_db(tmp_path, monkeypatch):
    path = tmp_path / "facets.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    factory = create_session_factory(engine)
    yield url, factory, tmp_path
    engine.dispose()


def add_agency(session, name: str, region: str, *, active: bool = True) -> Agency:
    agency = Agency(
        official_name=name, normalized_name=name.replace(" ", ""),
        agency_type=AgencyType.OTHER, region_code=region, active=active,
    )
    session.add(agency)
    session.flush()
    return agency


def add_unit(session, agency: Agency, name: str, *, active: bool = True) -> OrgUnit:
    unit = OrgUnit(
        agency_id=agency.id, name=name, normalized_name=name.replace(" ", ""),
        unit_type=OrgUnitType.DEPARTMENT, active=active,
    )
    session.add(unit)
    session.flush()
    return unit


def add_source(
    session, index: int, method: CollectionMethod, bindings: list[tuple[Agency, OrgUnit | None]],
    *, scheduled: bool = True,
) -> Source:
    source = Source(
        url=f"https://example.org/source-{index}",
        normalized_url=f"https://example.org/source-{index}",
        title=f"Source {index}", collection_method=method,
        scheduled_refresh_enabled=scheduled,
    )
    session.add(source)
    session.flush()
    for agency, unit in bindings:
        session.add(SourceBinding(
            source_id=source.id, agency_id=agency.id,
            org_unit_id=unit.id if unit else None,
            scope_key=f"org:{unit.id}" if unit else f"agency:{agency.id}",
            description=f"{agency.official_name} {unit.name if unit else '기관 공통'}",
            active=True,
        ))
    session.flush()
    return source


def test_lookup_endpoints_are_bounded_scoped_and_html_has_no_full_dropdowns(facet_db):
    url, factory, root = facet_db
    now = datetime.now(timezone.utc)
    agency_rows = []
    agency_ids = []
    for index in range(5_000):
        agency_id = uuid.uuid4()
        agency_ids.append(agency_id)
        agency_rows.append({
            "id": agency_id,
            "official_name": f"서울 확장기관 {index:04d}",
            "normalized_name": f"서울확장기관{index:04d}",
            "agency_type": AgencyType.OTHER,
            "region_code": "SEOUL" if index < 4_000 else "BUSAN",
            "active": index != 4_999,
            "created_at": now, "updated_at": now,
        })
    with factory() as session:
        session.execute(Agency.__table__.insert(), agency_rows)
        unit_rows = []
        for index in range(50_000):
            unit_rows.append({
                "id": uuid.uuid4(), "agency_id": agency_ids[index % 100],
                "parent_org_unit_id": None,
                "name": f"공통부서 {index % 500:03d}",
                "normalized_name": f"공통부서{index % 500:03d}",
                "unit_type": OrgUnitType.DEPARTMENT,
                "active": index % 997 != 0,
                "created_at": now, "updated_at": now,
            })
        session.execute(OrgUnit.__table__.insert(), unit_rows)
        session.commit()
        service = LookupService(session)
        agencies = service.agencies(q="서울", region_code="SEOUL", limit=30)
        units = service.org_units(agency_id=agency_ids[0], q="공통", limit=30)
        assert len(agencies) == 30
        assert len(units) == 30
        assert agencies == sorted(agencies, key=lambda item: (item["name"], item["id"]))
        assert all(item["region_code"] == "SEOUL" for item in agencies)
        assert all(item["agency_id"] == str(agency_ids[0]) for item in units)
        assert service.agencies(q="", region_code=None, limit=30) == []

    client = TestClient(regression_app(url, project_root=root))
    response = client.get("/api/lookups/agencies", params={"q": "서울", "region_code": "SEOUL", "limit": 30})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 30
    scoped = client.get(f"/api/lookups/agencies/{agency_ids[1]}/org-units", params={"q": "공통", "limit": 30})
    assert scoped.status_code == 200
    assert len(scoped.json()["items"]) == 30
    page = client.get("/sources")
    assert page.status_code == 200
    assert 'select class="control" name="agency_id"' not in page.text
    assert 'select class="control" name="org_unit_id"' not in page.text
    assert "서울 확장기관 4998" not in page.text


def test_binding_safe_facets_method_status_and_canonical_dedup(facet_db):
    _url, factory, _root = facet_db
    with factory() as session:
        seoul = add_agency(session, "서울 기관", "SEOUL")
        busan = add_agency(session, "부산 기관", "BUSAN")
        seoul_unit = add_unit(session, seoul, "서울 부서")
        busan_unit = add_unit(session, busan, "동명이 부서")
        seoul_api = add_source(session, 1, CollectionMethod.API, [(seoul, seoul_unit)])
        add_source(session, 2, CollectionMethod.WEB_PAGE, [(seoul, seoul_unit)])
        add_source(session, 3, CollectionMethod.API, [(busan, busan_unit)])
        multi = add_source(session, 4, CollectionMethod.API, [
            (seoul, seoul_unit), (busan, busan_unit), (seoul, None),
        ])
        cross = add_source(session, 5, CollectionMethod.WEB_CRAWL, [
            (seoul, seoul_unit), (busan, busan_unit),
        ])
        session.commit()
        query = SourceQueryService(session)
        combined = SourceFilterSpec.build(
            region_codes=["SEOUL"], methods=[CollectionMethod.API], status="unchecked",
        )
        matched = query.resolve_sources(combined)
        assert {source.id for source in matched} == {seoul_api.id, multi.id}
        assert len(matched) == len({source.id for source in matched})

        false_cross = SourceFilterSpec.build(
            region_codes=["SEOUL"], agency_id=busan.id, org_unit_id=busan_unit.id,
            methods=[CollectionMethod.WEB_CRAWL],
        )
        assert query.count(false_cross) == 0

        agency_org = SourceFilterSpec.build(
            agency_id=seoul.id, org_unit_id=seoul_unit.id,
            methods=[CollectionMethod.API, CollectionMethod.WEB_PAGE],
        )
        assert query.count(agency_org) == 3
        assert query.count(SourceFilterSpec.build(methods=[])) == 0
        assert cross.id not in {source.id for source in matched}
        with pytest.raises(SourceServiceError, match="부서는 지정한 기관"):
            SourceService(session).register_binding(
                url="https://example.org/cross-agency-save",
                agency_id=busan.id, org_unit_id=seoul_unit.id,
            )


def test_filtered_job_freezes_server_resolved_targets_dedupes_and_zero_creates_nothing(facet_db):
    _url, factory, _root = facet_db
    with factory() as session:
        agency = add_agency(session, "서울 대상기관", "SEOUL")
        unit = add_unit(session, agency, "대상부서")
        for index in range(74):
            bindings = [(agency, unit), (agency, None)] if index == 0 else [(agency, unit)]
            add_source(session, index, CollectionMethod.API, bindings, scheduled=index % 2 == 0)
        session.commit()
        spec = SourceFilterSpec.build(region_codes=["SEOUL"], methods=[CollectionMethod.API])
        job = CollectionJobService(session).create_manual(
            CollectionTriggerType.MANUAL_FILTER,
            requested_by_user_id=None, filter_spec=spec,
        )
        job_id = job.id
        assert job.total_items == 74
        assert len({item.source_id for item in job.items}) == 74
        assert job.trigger_context["filter"]["resolved_count"] == 74
        assert job.trigger_context["filter"]["agency_name"] is None
        add_source(session, 999, CollectionMethod.API, [(agency, unit)])
        session.commit()
        frozen = session.get(CollectionJob, job_id)
        assert frozen.total_items == 74
        assert len(frozen.items) == 74
        before = session.scalar(select(func.count()).select_from(CollectionJob))
        with pytest.raises(CollectionJobError):
            CollectionJobService(session).create_manual(
                CollectionTriggerType.MANUAL_FILTER,
                requested_by_user_id=None,
                filter_spec=SourceFilterSpec.build(methods=[]),
            )
        session.rollback()
        assert session.scalar(select(func.count()).select_from(CollectionJob)) == before


def test_filtered_job_api_snapshot_priority_and_busy_retry_preserve_intent(facet_db):
    url, factory, root = facet_db
    with factory() as session:
        agency = add_agency(session, "서울 작업기관", "SEOUL")
        unit = add_unit(session, agency, "작업부서")
        source = add_source(session, 1, CollectionMethod.API, [(agency, unit)], scheduled=False)
        session.commit()
        source_id, agency_id, unit_id = source.id, agency.id, unit.id
    client = TestClient(regression_app(url, project_root=root))
    assert client.get("/sources").status_code == 200
    response = client.post(
        "/api/collection-jobs",
        headers={"X-CSRF-Token": "test-csrf"},
        json={
            "trigger_type": "MANUAL_FILTER",
            "filter": {
                "region_codes": ["SEOUL"], "agency_id": str(agency_id),
                "org_unit_id": str(unit_id), "methods": ["API"],
                "status": "unchecked", "scheduled": "excluded", "search": "작업",
            },
        },
    )
    assert response.status_code == 202
    payload = response.json()["job"]
    assert payload["priority"] == 100
    assert payload["total_items"] == 1
    assert payload["trigger_context"]["filter"]["agency_name"] == "서울 작업기관"
    assert payload["trigger_context"]["filter"]["org_unit_name"] == "작업부서"
    assert payload["trigger_context"]["filter"]["resolved_count"] == 1
    assert "서울 작업기관" in payload["scope_summary"]

    calls = 0

    class BusyOnce:
        def collect(self, selected_source_id):
            nonlocal calls
            calls += 1
            assert selected_source_id == source_id
            if calls == 1:
                raise CollectionBusyError("already claimed")
            return SimpleNamespace(crawl_run=SimpleNamespace(
                id=None, source_id=selected_source_id,
                status=RunStatus.SUCCESS, error_summary=None,
            ))

    worker = CollectionJobWorker(factory, lambda _session: BusyOnce())
    assert worker.run_one() is WorkerResult.RETRY_LATER
    with factory() as session:
        job = session.get(CollectionJob, uuid.UUID(payload["id"]))
        assert job.items[0].status is CollectionJobItemStatus.PENDING
        assert job.items[0].error_code == "SOURCE_BUSY_RETRY"
    assert worker.run_one() is WorkerResult.ITEM_COMPLETE
    with factory() as session:
        job = session.get(CollectionJob, uuid.UUID(payload["id"]))
        assert job.succeeded_items == 1
        assert job.items[0].attempt_count == 2


def test_manual_filter_preempts_scheduled_job_at_item_boundary(facet_db):
    _url, factory, _root = facet_db
    with factory() as session:
        agency = add_agency(session, "서울 우선기관", "SEOUL")
        methods = (CollectionMethod.WEB_PAGE, CollectionMethod.WEB_CRAWL, CollectionMethod.API)
        source_ids = [add_source(session, index, method, [(agency, None)]).id for index, method in enumerate(methods)]
        session.commit()
        service = CollectionJobService(session)
        service.create_from_sources(
            CollectionTriggerType.SCHEDULED_FULL, service.scheduled_sources(),
            priority=SCHEDULED_FULL_PRIORITY, trigger_context={"slot": "test"},
            schedule_slot_key="06c4-priority",
        )
    attempted = []

    class Success:
        def collect(self, source_id):
            attempted.append(source_id)
            return SimpleNamespace(crawl_run=SimpleNamespace(
                id=None, source_id=source_id, status=RunStatus.SUCCESS, error_summary=None,
            ))

    worker = CollectionJobWorker(factory, lambda _session: Success())
    assert worker.run_one() is WorkerResult.ITEM_COMPLETE
    with factory() as session:
        CollectionJobService(session).create_manual(
            CollectionTriggerType.MANUAL_FILTER, requested_by_user_id=None,
            filter_spec=SourceFilterSpec.build(methods=[CollectionMethod.API]),
        )
    assert worker.run_one() is WorkerResult.ITEM_COMPLETE
    assert attempted == [source_ids[0], source_ids[2]]


def test_source_page_filter_state_ui_actions_and_server_preview(facet_db):
    url, factory, root = facet_db
    with factory() as session:
        seoul = add_agency(session, "서울 UI기관", "SEOUL")
        unit = add_unit(session, seoul, "UI부서")
        for index, method in enumerate((CollectionMethod.WEB_PAGE, CollectionMethod.WEB_CRAWL, CollectionMethod.API)):
            add_source(session, index, method, [(seoul, unit)])
        session.commit()
        agency_id, unit_id = seoul.id, unit.id
    client = TestClient(regression_app(url, project_root=root))
    params = {
        "region_code": "SEOUL", "agency_id": str(agency_id), "org_unit_id": str(unit_id),
        "methods": "API", "source_status": "unchecked", "scheduled": "all",
    }
    page = client.get("/sources", params=params)
    assert page.status_code == 200
    assert "서울 UI기관" in page.text
    assert 'name="methods" value="API"' in page.text
    assert "필터 결과 1개" in page.text
    assert "현재 페이지 수집 가능한 소스 전체 선택" in page.text
    assert "필터 결과 전체 지금 수집" in page.text
    assert "data-job-region-run" not in page.text
    assert "정기 전체 수집 대상에 포함" in page.text
    assert "개별 웹페이지" in page.text
    assert "웹사이트 탐색" in page.text
    assert "공개 API · RSS" in page.text
    preview = client.get("/api/source-index/preview", params=params)
    assert preview.status_code == 200
    assert preview.json()["eligible_total"] == 1
    assert preview.json()["method_counts"] == {"API": 1}


def test_20k_sources_50k_bindings_are_bounded_distinct_and_use_method_index(facet_db):
    _url, factory, _root = facet_db
    now = utc_now()
    agencies = []
    with factory() as session:
        for index in range(100):
            agencies.append(add_agency(session, f"규모 기관 {index:03d}", "SEOUL" if index < 50 else "BUSAN"))
        session.commit()
        source_rows = []
        source_ids = []
        for index in range(20_000):
            source_id = uuid.uuid4()
            source_ids.append(source_id)
            source_rows.append({
                "id": source_id, "url": f"https://scale.example/{index}",
                "normalized_url": f"https://scale.example/{index}",
                "title": f"규모 소스 {index}", "source_type": "GENERAL_PAGE",
                "collection_method": ("API" if index % 3 == 0 else "WEB_PAGE" if index % 3 == 1 else "WEB_CRAWL"),
                "data_format": "HTML", "coverage_mode": "UNKNOWN",
                "scheduled_refresh_enabled": index % 2 == 0,
                "last_checked_at": None, "last_success_at": None,
                "active": True, "created_at": now, "updated_at": now,
            })
        session.execute(Source.__table__.insert(), source_rows)
        binding_rows = []
        for index in range(50_000):
            source_id = source_ids[index % 20_000]
            agency = agencies[index % 100]
            binding_rows.append({
                "id": uuid.uuid4(), "source_id": source_id, "agency_id": agency.id,
                "org_unit_id": None, "scope_key": f"scale:{index}",
                "description": "scale", "active": True,
                "exclusion_reason": None, "excluded_at": None,
                "created_at": now, "updated_at": now,
            })
        session.execute(SourceBinding.__table__.insert(), binding_rows)
        session.commit()
        query = SourceQueryService(session)
        spec = SourceFilterSpec.build(region_codes=["SEOUL"], methods=[CollectionMethod.API])
        page = query.list_page(spec, page=1, page_size=100)
        assert len(page["items"]) <= 100
        assert len({item["source_id"] for item in page["items"]}) == len(page["items"])
        assert page["total"] == query.count(spec)
        assert all(item["collection_method"] == "API" for item in page["items"])
        sql = query.source_ids_statement(spec).compile(
            session.bind, compile_kwargs={"literal_binds": True}
        )
        plan = session.execute(text("EXPLAIN QUERY PLAN " + str(sql))).all()
        assert any("ix_sources_collection_method" in " ".join(str(value) for value in row) for row in plan)


def test_06c4_migration_clean_prior_downgrade_reupgrade_and_metadata(tmp_path, monkeypatch):
    config = Config("alembic.ini")
    clean_url = f"sqlite:///{(tmp_path / 'clean.sqlite3').as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", clean_url)
    command.upgrade(config, "head")
    clean_engine = create_db_engine(clean_url)
    with clean_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "a0c406c40001"
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    clean_engine.dispose()

    prior_url = f"sqlite:///{(tmp_path / 'prior.sqlite3').as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", prior_url)
    command.upgrade(config, "f9b207c06c03")
    prior_engine = create_db_engine(prior_url)
    assert "ix_sources_collection_method" not in {item["name"] for item in inspect(prior_engine).get_indexes("sources")}
    prior_engine.dispose()
    command.upgrade(config, "head")
    upgraded_engine = create_db_engine(prior_url)
    assert "ix_sources_collection_method" in {item["name"] for item in inspect(upgraded_engine).get_indexes("sources")}
    upgraded_engine.dispose()
    command.downgrade(config, "f9b207c06c03")
    downgraded_engine = create_db_engine(prior_url)
    assert "ix_sources_collection_method" not in {item["name"] for item in inspect(downgraded_engine).get_indexes("sources")}
    downgraded_engine.dispose()
    command.upgrade(config, "head")
