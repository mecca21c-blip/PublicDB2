from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from alembic import command
from alembic.config import Config
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import pytest

from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import (
    AgencyType,
    CollectionJob,
    CollectionJobItem,
    CollectionJobItemStatus,
    CollectionJobStatus,
    CollectionMethod,
    CollectionTriggerType,
    ContactPoint,
    ContactType,
    CrawlRun,
    Duty,
    OrgUnitType,
    Person,
    PersonAssignment,
    RunStatus,
    Source,
    StageStatus,
)
from app.services.agency_service import AgencyService
from app.services.global_search_service import GlobalSearchService
from app.services.source_service import SourceService
from tests.support import regression_app


GANGSEO_URL = "https://www.gangseo.seoul.kr/health/ht060301"
OTHER_GANGSEO_URL = "https://www.gangseo.seoul.kr/health/ht060304"
OPEN_API_URL = "https://data.gangseo.seoul.kr/openinf/openapiview.jsp?infId=OA-10005"
SEOUL_URL = "https://www.seoul.go.kr/seoul/seoul.do"


def _add_job(session, source_ids, created_at):
    job = CollectionJob(
        trigger_type=CollectionTriggerType.MANUAL_SELECTION,
        status=CollectionJobStatus.COMPLETED,
        priority=100,
        trigger_context={"scope": "06c7g"},
        total_items=len(source_ids),
        succeeded_items=len(source_ids),
        failed_items=0,
        skipped_items=0,
        created_at=created_at,
        started_at=created_at,
        finished_at=created_at + timedelta(seconds=2),
    )
    session.add(job)
    session.flush()
    for sequence, source_id in enumerate(source_ids, 1):
        session.add(CollectionJobItem(
            job_id=job.id,
            source_id=source_id,
            sequence=sequence,
            status=CollectionJobItemStatus.SUCCESS,
            attempt_count=1,
            started_at=created_at,
            finished_at=created_at + timedelta(seconds=1),
        ))


def seed_search_fixture(database_url: str) -> None:
    engine = create_db_engine(database_url)
    factory = create_session_factory(engine)
    with factory() as session:
        agencies = AgencyService(session)
        gangseo, _ = agencies.create_agency(
            official_name="강서구청",
            agency_type=AgencyType.BASIC_LOCAL_GOVERNMENT,
            external_identifier="gangseo-gu",
            address="서울특별시 강서구 화곡로 302",
        )
        gangseo_id = uuid.UUID(gangseo["id"])
        unit = agencies.create_org_unit(
            agency_id=gangseo_id,
            name="정보시스템과",
            unit_type=OrgUnitType.DEPARTMENT,
        )
        unit_id = uuid.UUID(unit["id"])
        duty = Duty(
            agency_id=gangseo_id,
            org_unit_id=unit_id,
            title="식품제조가공업",
            description="fixture description is intentionally not searched",
        )
        session.add(duty)
        session.flush()

        seoul, _ = agencies.create_agency(
            official_name="서울특별시",
            agency_type=AgencyType.METROPOLITAN_GOVERNMENT,
        )
        seoul_id = uuid.UUID(seoul["id"])

        source_ids = []
        source_service = SourceService(session)
        for url, agency_id, org_unit_id, title, method in (
            (GANGSEO_URL, gangseo_id, unit_id, "강서구 보건소 직원안내", CollectionMethod.WEB_CRAWL),
            (OTHER_GANGSEO_URL, gangseo_id, unit_id, "강서구 보건소 다른 경로", CollectionMethod.WEB_CRAWL),
            (OPEN_API_URL, gangseo_id, unit_id, "강서구 열린데이터", CollectionMethod.WEB_PAGE),
            (SEOUL_URL, seoul_id, None, "서울시 대표", CollectionMethod.WEB_PAGE),
        ):
            binding, _ = source_service.register_binding(
                url=url,
                agency_id=agency_id,
                org_unit_id=org_unit_id,
                description="강서 운영 연결" if agency_id == gangseo_id else "서울 운영 연결",
            )
            source_id = uuid.UUID(binding["source_id"])
            source = session.get(Source, source_id)
            source.title = title
            source.collection_method = method
            source_ids.append(source_id)

        person = Person(
            agency_id=gangseo_id,
            name="홍길동",
            normalized_name="홍길동",
        )
        session.add(person)
        session.flush()
        assignment = PersonAssignment(
            person_id=person.id,
            agency_id=gangseo_id,
            org_unit_id=unit_id,
            position_title="주무관",
            role_description="식품제조가공업 담당",
        )
        session.add(assignment)
        session.flush()
        session.add(ContactPoint(
            agency_id=gangseo_id,
            org_unit_id=unit_id,
            person_assignment_id=assignment.id,
            duty_id=duty.id,
            contact_type=ContactType.PHONE,
            value="02-2600-5861",
            normalized_value="0226005861",
        ))
        for index in range(11):
            value = f"fixture{index}@gangseo.go.kr"
            session.add(ContactPoint(
                agency_id=gangseo_id,
                org_unit_id=unit_id,
                person_assignment_id=assignment.id,
                duty_id=duty.id,
                contact_type=ContactType.EMAIL,
                value=value,
                normalized_value=value,
            ))

        base = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
        for index in range(10):
            source_id = source_ids[0] if index < 2 else source_ids[1 + (index % 3)]
            source = session.get(Source, source_id)
            session.add(CrawlRun(
                source_id=source_id,
                status=RunStatus.SUCCESS,
                connection_status=StageStatus.SUCCESS,
                raw_status=StageStatus.SUCCESS,
                extraction_status=StageStatus.SUCCESS,
                started_at=base - timedelta(minutes=index),
                finished_at=base - timedelta(minutes=index) + timedelta(seconds=3),
                records_observed=203 if index == 0 else 0,
                collection_method_snapshot=source.collection_method,
                collection_kind_snapshot="HTML",
                collection_config_snapshot={
                    "target_url": source.url,
                    "crawl_scope": "PATH_PREFIX",
                },
                collection_statistics={"pages_discovered": 203 if index == 0 else 1},
            ))

        _add_job(session, [source_ids[0], source_ids[1]], base + timedelta(minutes=3))
        _add_job(session, [source_ids[2]], base + timedelta(minutes=2))
        _add_job(session, [source_ids[3]], base + timedelta(minutes=1))
        session.commit()
    engine.dispose()


@pytest.fixture()
def search_db(tmp_path, monkeypatch):
    database_path = tmp_path / "06c7g.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("PUBLICDB2_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    seed_search_fixture(database_url)
    engine = create_db_engine(database_url)
    factory = create_session_factory(engine)
    yield database_url, factory, tmp_path
    engine.dispose()


def test_runs_native_get_form_active_path_and_exact_results(search_db):
    database_url, _factory, root = search_db
    with TestClient(regression_app(database_url, project_root=root)) as client:
        response = client.get(
            "/runs?date_from=&date_to=&agency_id=&status=&search=ht060301"
        )
        empty = client.get(
            "/runs?date_from=&date_to=&agency_id=&status=&search=nonexistentxyz"
        )
        combined = client.get(
            "/runs?date_from=2026-10-13&date_to=2026-10-13"
            "&status=SUCCESS&search=ht060301"
        )

    assert response.status_code == 200
    assert len(response.history) == 1
    assert response.history[0].status_code == 303
    assert response.url.path == "/runs"
    assert parse_qs(response.url.query.decode()) == {"search": ["ht060301"]}
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.select_one("form[data-run-search-form]")
    assert form["method"] == "get"
    assert form["action"] == "/runs"
    assert form["aria-label"] == "수집 이력 검색"
    assert soup.select_one("#run-search")["name"] == "search"
    assert soup.select_one("#run-search")["value"] == "ht060301"
    assert soup.select_one(".collection-job-history .table-count").get_text(strip=True) == "1건"
    assert soup.select_one(".run-list-panel .table-count").get_text(strip=True) == "2건"
    assert len(soup.select("[data-job-summary]")) == 1
    assert len(soup.select(".run-list-panel .selectable-row")) == 2
    assert "ht060301" in soup.select_one(
        ".run-detail-panel .detail-view:not([hidden])"
    ).get_text(" ", strip=True)
    assert OPEN_API_URL not in soup.select_one(".run-list-panel").get_text(" ")

    empty_soup = BeautifulSoup(empty.text, "html.parser")
    assert empty_soup.select_one(".collection-job-history .table-count").get_text(strip=True) == "0건"
    assert empty_soup.select_one(".run-list-panel .table-count").get_text(strip=True) == "0건"

    combined_soup = BeautifulSoup(combined.text, "html.parser")
    assert combined_soup.select_one("#run-search")["value"] == "ht060301"
    assert combined_soup.select_one("#run-from")["value"] == "2026-10-13"
    assert combined_soup.select_one("#run-to")["value"] == "2026-10-13"
    assert combined_soup.select_one("#run-state option[selected]")["value"] == "SUCCESS"


@pytest.mark.parametrize(
    ("query", "group", "kind"),
    (
        ("강서구청", "organizations", "기관"),
        ("정보시스템과", "organizations", "부서"),
        ("식품제조가공업", "organizations", "업무"),
        ("ht060301", "sources", "수집 소스"),
        ("02-2600-5861", "contacts", "연락처"),
    ),
)
def test_global_search_operational_fields(search_db, query, group, kind):
    _database_url, factory, _root = search_db
    with factory() as session:
        results = GlobalSearchService(session).search(query)
    assert results[group]["total"] > 0
    assert any(item["kind"] == kind for item in results[group]["items"])
    assert all(len(results[name]["items"]) <= 10 for name in (
        "organizations", "sources", "contacts",
    ))
    if query == "강서구청":
        assert results["sources"]["total"] == 3
        assert results["contacts"]["total"] == 12
        assert len(results["contacts"]["items"]) == 10
        assert results["contacts"]["has_more"] is True
    if query in {"정보시스템과", "식품제조가공업"}:
        assert results["contacts"]["total"] == 12


def test_global_search_route_header_state_navigation_zero_and_form_ownership(search_db):
    database_url, _factory, root = search_db
    with TestClient(regression_app(database_url, project_root=root)) as client:
        response = client.get("/search?q=강서구청")
        zero = client.get("/search?q=nonexistentxyz")
        runs = client.get("/runs")

        soup = BeautifulSoup(response.text, "html.parser")
        header_form = soup.select_one("form[data-global-search-form]")
        header_input = soup.select_one("#global-search-input")
        assert header_form["method"] == "get"
        assert header_form["action"] == "/search"
        assert header_input["name"] == "q"
        assert header_input["value"] == "강서구청"
        assert "readonly" not in header_input.attrs
        assert soup.select_one('[data-search-group="organizations"] .table-count').get_text(strip=True) == "1건"
        assert soup.select_one('[data-search-group="sources"] .table-count').get_text(strip=True) == "3건"
        assert soup.select_one('[data-search-group="contacts"] .table-count').get_text(strip=True) == "12건"
        source_link = soup.select_one('[data-search-group="sources"] [data-search-result-kind="수집 소스"]')
        params = parse_qs(urlparse(source_link["href"]).query)
        assert params == {"search": ["강서구청"]}
        destination = client.get(source_link["href"])
        assert destination.status_code == 200
        assert GANGSEO_URL in destination.text

    zero_soup = BeautifulSoup(zero.text, "html.parser")
    assert zero_soup.select_one("[data-global-search-zero]") is not None
    assert "0건" in zero_soup.get_text()

    runs_soup = BeautifulSoup(runs.text, "html.parser")
    assert len(runs_soup.select("#global-search-input")) == 1
    assert len(runs_soup.select("#run-search")) == 1
    assert runs_soup.select_one("form[data-global-search-form]")["action"] == "/search"
    assert runs_soup.select_one("form[data-run-search-form]")["action"] == "/runs"
    assert runs_soup.select_one("#global-search-input")["name"] == "q"
    assert runs_soup.select_one("#run-search")["name"] == "search"


def test_global_search_empty_query_is_neutral(search_db):
    database_url, _factory, root = search_db
    with TestClient(regression_app(database_url, project_root=root)) as client:
        response = client.get("/search?q=%20%20")
    soup = BeautifulSoup(response.text, "html.parser")
    assert response.status_code == 200
    assert soup.select_one("#global-search-input")["value"] == ""
    assert soup.select_one(".global-search-neutral") is not None
