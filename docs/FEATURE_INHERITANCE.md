# PublicDB2 Feature Inheritance

기준: PublicDB1 감사 HEAD 1319ae31ddf7234a1a8632271b5ff899006d47de.
03A는 legacy 의미와 검증된 로직을 선택적으로 옮겼으며 PublicDB1 runtime module이나
production DB를 참조하지 않는다.

| PublicDB1 자산/기능 | 03A 분류 | PublicDB2 위치 / 결정 |
|---|---|---|
| Agency, OrgUnit, Duty 모델 의미 | INHERITED_AND_ACTIVE | app/models/entities.py; PublicDB2 DB 소유 |
| AgencyRegistry 이름 정규화·등록 규칙 | ADAPTED_AND_ACTIVE | app/services/agency_service.py; repository/API/UI 경계 추가 |
| Person / PersonAssignment / ContactPoint | INHERITED_AND_ACTIVE | schema foundation 활성, 현재 UI writer는 STILL_PENDING |
| Source URL validation/normalization | ADAPTED_AND_ACTIVE | app/services/normalization.py; fragment 제거, public host 검증 유지 |
| legacy Source의 직접 Agency ownership | DO_NOT_INHERIT | 다중 context 요구와 충돌 |
| Canonical Source | ADAPTED_AND_ACTIVE | 전역 unique normalized_url은 물리 URL identity만 소유 |
| SourceBinding | NEW_PUBLICDB2 | Source와 Agency/optional OrgUnit 관계, context별 active/excluded 소유 |
| SourceRegistry / SourceAdministrationService | ADAPTED_AND_ACTIVE | app/services/source_service.py; metadata-only transaction, HTTP 없음 |
| CrawlRun / Observation / SourceOccurrence | INHERITED_AND_ACTIVE | provenance schema foundation; 실행 writer는 STILL_PENDING |
| ChangeEvent / ChangeDetection / ContactHistory | INHERITED_AND_ACTIVE | 이력 schema foundation; review/apply는 STILL_PENDING |
| SQLAlchemy base/session/config | ADAPTED_AND_ACTIVE | app/db, app/core; 독립 DB와 환경 override |
| Alembic migration | NEW_PUBLICDB2 | migrations/versions/68ed1d36c20d_initial_publicdb2_core_schema.py |
| 기관/부서/업무 API와 live UI | NEW_PUBLICDB2 | app/api/agencies.py, /agencies |
| SourceBinding API와 live UI | NEW_PUBLICDB2 | app/api/sources.py, /sources |
| binding 제외·복구 | NEW_PUBLICDB2 | 물리 삭제 없이 binding 상태와 사유/시각 보존 |
| HTTP 수집·RAW·추출·Master promotion | STILL_PENDING | 03A 실행 경로 없음 |
| Excel/CSV URL import와 충돌 검토 | NEW_PUBLICDB2 | PREVIEW/CONFIRM, 7개 분류, stale DB 재검증과 파일 audit 활성 |
| Dashboard/runs/contacts/review/settings live read model | STILL_PENDING | 명시적 fixture/demo 유지 |
| PublicDB1 Dashboard/UI/지도 placeholder | DO_NOT_INHERIT | PublicDB2 동결 UI 계약과 무관 |
| PublicDB1 production DB와 RAW 파일 | DO_NOT_INHERIT | 직접 연결·복사·import 금지 |

INHERITED_AND_ACTIVE는 schema/domain 의미가 PublicDB2 코드와 migration에 포함됐다는
뜻이며, 해당 entity의 모든 writer나 수집 실행이 활성화됐다는 뜻은 아니다.
ADAPTED_AND_ACTIVE는 PublicDB2 계약에 맞게 변경되어 PublicDB2가 소유한다.

## 03B portable runtime

app/core/config.py가 project-root 기반 DB, RAW, import, export, temp, log, backup,
config 경로를 소유한다. source_import_service.py는 03A SourceService를 transaction
내부에서 재사용하며 canonical URL uniqueness와 binding scope를 우회하지 않는다.
source_import_logs는 확정 파일의 원본명, project-relative 경로, SHA-256과 결과만
보존한다. CollectionService, CrawlRun 생성과 외부 HTTP는 STILL_PENDING이다.
