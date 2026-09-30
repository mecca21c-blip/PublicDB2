# PublicDB2 Feature Inheritance

기준: PublicDB1 감사 HEAD 1319ae31ddf7234a1a8632271b5ff899006d47de.
03A는 legacy 의미와 검증된 로직을 선택적으로 옮겼으며 PublicDB1 runtime module이나
production DB를 참조하지 않는다.

| PublicDB1 자산/기능 | 03A 분류 | PublicDB2 위치 / 결정 |
|---|---|---|
| Agency, OrgUnit, Duty 모델 의미 | INHERITED_AND_ACTIVE | app/models/entities.py; PublicDB2 DB 소유 |
| AgencyRegistry 이름 정규화·등록 규칙 | ADAPTED_AND_ACTIVE | app/services/agency_service.py; repository/API/UI 경계 추가 |
| Person / PersonAssignment / ContactPoint | INHERITED_AND_ACTIVE | ContactPoint writer/read model 활성; Person 계열 자동 writer는 의도적으로 비활성 |
| Source URL validation/normalization | ADAPTED_AND_ACTIVE | app/services/normalization.py; fragment 제거, public host 검증 유지 |
| legacy Source의 직접 Agency ownership | DO_NOT_INHERIT | 다중 context 요구와 충돌 |
| Canonical Source | ADAPTED_AND_ACTIVE | 전역 unique normalized_url은 물리 URL identity만 소유 |
| SourceBinding | NEW_PUBLICDB2 | Source와 Agency/optional OrgUnit 관계, context별 active/excluded 소유 |
| SourceRegistry / SourceAdministrationService | ADAPTED_AND_ACTIVE | app/services/source_service.py; metadata-only transaction, HTTP 없음 |
| CrawlRun / Observation | ADAPTED_AND_ACTIVE | app/models/evidence.py; Source 소유 stage lifecycle과 HTTP/RAW metadata 활성 |
| SourceOccurrence | INHERITED_AND_ACTIVE | app/services/master_promotion_apply_service.py와 review_service.py가 idempotent provenance 기록 |
| ChangeEvent / ChangeDetection / ContactHistory | ADAPTED_AND_ACTIVE | app/models/evidence.py; baseline/change/review/contact temporal contract 활성 |
| SQLAlchemy base/session/config | ADAPTED_AND_ACTIVE | app/db, app/core; 독립 DB와 환경 override |
| Alembic migration | NEW_PUBLICDB2 | core/import/04A에 이어 9b2d04b04b01 confirmed review workflow |
| 기관/부서/업무 API와 live UI | NEW_PUBLICDB2 | app/api/agencies.py, /agencies |
| SourceBinding API와 live UI | NEW_PUBLICDB2 | app/api/sources.py, /sources |
| binding 제외·복구 | NEW_PUBLICDB2 | 물리 삭제 없이 binding 상태와 사유/시각 보존 |
| HTTPFetcher | ADAPTED_AND_ACTIVE | app/collectors/http_fetcher.py; redirect별 public-host 재검증과 timeout/size 제한 |
| RawArtifactStore | ADAPTED_AND_ACTIVE | app/services/raw_artifact_store.py; PublicDB2 project-relative portable RAW 경로 |
| CollectionService/pipeline | ADAPTED_AND_ACTIVE | app/services/collection_service.py; canonical Source 단일 action과 stage 상태 계약 |
| artifact validation | ADAPTED_AND_ACTIVE | app/services/artifact_validation.py; RAW stage, 경로 경계, hash, type, size 재검증 |
| HTML contact extractor | INHERITED_AND_ACTIVE | app/collectors/html_contact_extractor.py; 결정적 PHONE/EMAIL/FAX discovery |
| staff-directory extractor | INHERITED_AND_ACTIVE | app/collectors/staff_directory_extractor.py; header 기반 구조 행 discovery |
| extraction persistence | ADAPTED_AND_ACTIVE | ExtractionRun, ExtractedContactCandidate, ExtractedDirectoryRecord |
| MasterPromotionPlanner / normalization / promotion plan | ADAPTED_AND_ACTIVE | app/services/master_promotion_planner.py, master_normalization.py, promotion_plan.py; exact context match와 person 비차단으로 적응 |
| MasterPromotionApply | ADAPTED_AND_ACTIVE | app/services/master_promotion_apply_service.py; preview 재계산, atomic safe apply, occurrence/event/history |
| SourceChangeDetection | ADAPTED_AND_ACTIVE | app/services/source_change_detection_service.py; canonical Source + Agency scope, persisted candidate, idempotent generation |
| SourceCoverage | ADAPTED_AND_ACTIVE | app/services/source_coverage_service.py; canonical Source의 UNKNOWN/ADDITIVE_ONLY/COMPLETE_SNAPSHOT |
| Review apply | NEW_PUBLICDB2 | app/services/review_service.py; candidate별 stale revalidation, approve/keep/defer |
| Contacts read model | NEW_PUBLICDB2 | app/services/contact_service.py; confirmed ContactPoint context grouping과 provenance/history |
| Excel/CSV URL import와 충돌 검토 | NEW_PUBLICDB2 | PREVIEW/CONFIRM, 7개 분류, stale DB 재검증과 파일 audit 활성 |
| /runs live read model | NEW_PUBLICDB2 | app/services/run_service.py; SourceBinding 기반 filter와 다중 context 표시 |
| /contacts와 /review live read model | NEW_PUBLICDB2 | fixture 비활성, ContactPoint/DetectedChangeCandidate 실 DB 소유 |
| Dashboard/settings live read model | STILL_PENDING | 기존 fixture/demo 유지 |
| PublicDB1 Dashboard/UI/지도 placeholder | DO_NOT_INHERIT | PublicDB2 동결 UI 계약과 무관 |
| PublicDB1 production DB와 RAW 파일 | DO_NOT_INHERIT | 직접 연결·복사·import 금지 |

INHERITED_AND_ACTIVE는 schema/domain 의미가 PublicDB2 코드와 migration에 포함됐다는
뜻이며, 해당 entity의 모든 writer나 수집 실행이 활성화됐다는 뜻은 아니다.
ADAPTED_AND_ACTIVE는 PublicDB2 계약에 맞게 변경되어 PublicDB2가 소유한다.

## 03B portable runtime

app/core/config.py가 project-root 기반 DB, RAW, import, export, temp, log, backup,
config 경로를 소유한다. source_import_service.py는 SourceService를 transaction
내부에서 재사용하며 canonical URL uniqueness와 binding scope를 우회하지 않는다.
source_import_logs는 확정 파일의 원본명, project-relative 경로, SHA-256과 결과만
보존한다. 04A CollectionService도 같은 project-root 경계 아래 RAW만 소유한다.
