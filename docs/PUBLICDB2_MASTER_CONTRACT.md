# PublicDB2 Master Contract

## 06A Operational acceptance contract

- A canonical Source can have at most one `RUNNING` CrawlRun. SQLite and
  PostgreSQL enforce this with a partial unique index, while `operation_claims`
  provides the cross-session collection claim.
- Collection startup and every explicit collection request reconcile orphaned
  `RUNNING` rows after a 10-minute stale-heartbeat boundary. Recovery records a
  terminal `FAILED` run, a clear interruption reason, and no fabricated RAW,
  Observation, extraction, or success state.
- Source method changes use the same Source claim and cannot race an active
  collection. Each CrawlRun retains its immutable method/config snapshot.
- Baseline apply, review resolution, and source-import confirm have DB-backed
  idempotency claims. Repeated completion returns a reused terminal result or a
  clear conflict and does not repeat confirmed mutations.
- File-backed SQLite connections centrally enable foreign keys, a 5,000 ms busy
  timeout, and WAL mode. Other database dialects do not receive SQLite pragmas.
- `/agencies`, `/sources`, `/runs`, `/contacts`, and `/review` use server-side
  pagination: default 100 and maximum 500. Dashboard recent/history projections
  remain bounded. Contact XLSX export streams the complete filtered confirmed
  dataset and is independent of the visual page.
- Runtime DB selection follows the explicit `project_root` unless
  `PUBLICDB2_DATABASE_URL` overrides it. Runtime paths stored in DB remain
  project-relative so the PublicDB2 tree can be relocated as one unit.

상태: 향후 기능 구현의 제품·데이터 의미 SSOT

## 제품 목적과 경계

PublicDB2는 정부·공공기관이 공식 공개한 기관, 조직/부서, 업무, 사람/재직과
연락처를 출처 및 변경 근거와 함께 관리하는 로컬 운영 도구다. PublicDB2 소유 DB,
기관/부서/업무와 수집 소스 관리, Excel/CSV Source import, 명시적 WEB_PAGE
수집·RAW 증거·결정적 발견 후보 추출과 실제 수집 이력이 활성화됐다. 04B에서
승인된 baseline의 확정 연락처 반영, Source 범위 변경 감지, 검토 결정과 연락처
변경 이력도 활성화됐다.
05A에서 실 Dashboard, typed 운영 Settings, 확정 연락처 XLSX export와
application user 인증·역할·CSRF 및 서버 운영 경계가 활성화됐다.
05B에서 하나의 Source가 하나의 현재 방식을 소유하는 3-Way 수집, typed method
config, immutable CrawlRun snapshot, 다중 Observation, OpenAPI/RSS/Atom discovery,
portable API credential store가 활성화됐다.

PublicDB1(C:\PublicDB)은 읽기 전용 legacy reference다. 코드나 DB를 통째로
복사하거나 직접 연결하지 않으며, 확인된 의미와 재검증 가능한 로직만 별도 Goal에서
계승한다.

## Routes와 책임

| Route | 책임 |
|---|---|
| /login | application user의 서명 세션 로그인 |
| / | 전체 운영 상태를 요약하는 유일한 Dashboard |
| /agencies | 기관 → 부서 → 업무와 연결 소스 관계 관리 |
| /sources | 기관/부서별 수집 URL, 확인 상태와 제외 상태 관리 |
| /runs | 수집 시도, baseline 미리보기/반영, 변경 검토 생성 상태 확인 |
| /contacts | 확정 연락처 검색, 출처와 변경 이력 확인 |
| /review | 발견값과 확정값 비교 및 반영·유지·보류 처리 |
| /settings | 최소 운영 저장·수집 정책 설정 |

## 05A Authentication and operation contract

- User는 case-insensitive normalized username, Argon2 password hash, ADMIN/OPERATOR/
  VIEWER 역할과 active 상태를 소유한다. plaintext password와 기본 credential은 없다.
- VIEWER는 운영 화면 읽기만, OPERATOR는 기존 business mutation과 확정 연락처
  export를, ADMIN은 추가로 Settings와 사용자 관리를 수행한다. 모든 권한은 API
  boundary에서 현재 active User와 DB의 현재 role을 다시 읽어 강제한다.
- 브라우저 세션은 HttpOnly, SameSite=Lax, 만료 시간이 있는 서명 cookie다. 세션에는
  user_id와 예측 불가능 CSRF token만 둔다. 모든 POST/PUT/PATCH/DELETE는 CSRF를
  요구하며 logout도 POST다.
- session secret은 PUBLICDB2_SESSION_SECRET 또는 PROJECT_ROOT/config/security.json이
  소유한다. 환경 override가 없으면 암호학적으로 생성해 project tree에 지속하고
  Git에서 제외한다. 최초 ADMIN은 scripts/manage_user.py로 명시적으로 생성한다.
- 마지막 active ADMIN의 비활성화와 역할 강등은 service layer에서 거부한다.

## 05A Dashboard, Settings, and export contract

- Dashboard는 fixture fallback 없이 live DB만 읽는다. active Agency, active binding을
  하나 이상 가진 distinct active Source, active ContactPoint, PENDING_REVIEW/DEFERRED,
  canonical Source별 최신 FAILED/PARTIAL run을 KPI로 계산한다.
- 최근 실행은 최신 CrawlRun 6건, 추이는 Asia/Seoul 기준 최근 7 calendar day,
  최근 검토는 unresolved candidate 5건이다. 빈 DB는 0/빈 상태이고 DB 장애는 한국어
  오류로 표시한다.
- OperationalSettings 단일 typed row는 HTTP timeout, 최대 response bytes,
  PublicDB User-Agent만 소유한다. row가 없으면 안전한 코드 기본값을 사용하고 ADMIN
  저장 후 다음 collection action의 HTTPFetcher부터 재시작 없이 반영한다.
- /settings는 ADMIN 전용이며 PROJECT_ROOT 소유 DB/RAW/import/export/temp/log/backup/
  config 경로와 3-Way 지원, 자동 수집 미사용을 read-only로 표시한다.
- 확정 연락처 export는 현재 연락처 필터를 적용해 ContactPoint당 한 행 XLSX를 만들며
  discovery candidate를 포함하지 않는다. 파일은 data/exports/YYYY/MM/DD 아래에서만
  생성하고 수식 시작 문자를 literal로 방어한다. OPERATOR와 ADMIN만 실행한다.
- /health는 최소 liveness, /ready는 DB와 Alembic head를 확인한다. allowed host,
  same-origin CSP/security headers, project logs의 bounded rotation을 적용한다.
  scripts/run_web.py는 localhost proxy에서 온 forwarded header만 신뢰한다.

## UI 관련 entity 관계

- 기관은 여러 부서를 가진다.
- 부서는 여러 업무와 연락처를 가질 수 있다.
- 기관 또는 부서는 여러 수집 소스 URL과 연결될 수 있다.
- 하나의 수집 실행은 하나의 소스에 대한 시도이며 단계별 결과를 가진다.
- 추출 결과는 발견 후보이며 검토 전 확정 연락처가 아니다.
- 확정 연락처는 공식 출처와 확인일, 변경 이력을 유지한다.

## 03A Source/Binding SSOT

- Source는 정규화 URL과 기술 수집 메타데이터를 소유하는 canonical identity다.
- Source.normalized_url은 전역 unique이며 URL fragment는 identity에서 제외한다.
- SourceBinding은 Source와 기관, 선택적 부서의 업무 관계를 소유한다.
- 같은 canonical Source를 여러 기관/부서 context에 연결할 수 있으나 동일 Source와
  동일 context의 중복 binding은 만들지 않는다.
- 부서 binding은 반드시 지정 기관 소속이어야 하며 기관 공통 binding도 지원한다.
- 제외 사유·시각·활성 상태는 canonical Source가 아니라 binding이 소유한다.
- 등록은 metadata transaction이며 HTTP 요청이나 수집을 시작하지 않는다.
- 기본 DB는 data/db/publicdb2.sqlite3이고 PUBLICDB2_DATABASE_URL로 재정의한다.

## 상태와 처리 의미

소스 상태는 미확인, 정상, 자료없음, 오류, 제외다. 자료없음은 정상 접근과 정상
추출 후 대상 데이터가 0건인 경우에만 사용한다. 미검사, 접근 실패, 추출 실패는
자료없음으로 바꾸지 않는다.

수집 단계는 접속 → RAW 저장 → 추출 → 발견 결과 → 확정 DB 반영 여부로 구분한다.
앞 단계의 성공은 뒤 단계의 성공을 보장하지 않는다.

발견 데이터는 수집·추출로 관찰된 후보이고, 확정 데이터는 사용자 검토 또는 승인된
규칙을 거친 운영값이다. 두 값은 UI와 저장 계약에서 분리한다.

## 04A Live Collection Foundation

- canonical Source가 CrawlRun과 Observation을 소유한다. SourceBinding은 기관과
  선택적 부서 context만 소유하며 binding 수만큼 같은 URL을 다시 수집하지 않는다.
- 사용자의 단일 수집 action이 안전한 HTTP GET, RAW 저장, Observation, 연락처와
  직원명부 추출, 발견 후보 저장, CrawlRun 확정을 연속 수행한다.
- 04A가 제공한 WEB_PAGE 단일 URL 경로는 05B dispatcher의 SCRAPE 경로로 보존된다.
- RAW는 PROJECT_ROOT/data/raw 아래 날짜/source/run 경로에 atomic 저장하고 DB에는
  project-relative POSIX 경로, SHA-256과 응답 byte 수만 기록한다.
- 연락처 후보와 직원명부 행은 discovery data다. ContactPoint, Person,
  PersonAssignment, SourceOccurrence, ChangeEvent와 ContactHistory를 생성하지 않는다.
- 두 extractor가 모두 성공하고 발견 수가 1건 이상이면 정상, 모두 성공하고 0건이면
  자료없음이다. 접속/RAW 실패, partial extraction, 미지원 content는 오류다.
  binding 제외 상태는 이 공통 Source 실행 상태보다 우선한다.
- /runs는 CrawlRun/Observation/ExtractionRun의 실 DB projection이며 기관 filter는
  SourceBinding을 통해 해석한다. 여러 binding은 요약과 전체 context로 표시한다.

## 04B Confirmed Master and Review Contract

- 발견 데이터(ExtractionRun, ExtractedContactCandidate, ExtractedDirectoryRecord)와
  확정 데이터(OrgUnit, Duty, Person, PersonAssignment, ContactPoint)는 분리한다.
  발견 후보는 승인 전 /contacts에 표시하지 않는다.
- 성공한 staff-directory extraction에서 같은 canonical Source와 Agency context의
  확정 SourceOccurrence가 없으면 baseline 경로, 있으면 change detection 경로다.
- active SourceBinding이 한 Agency로만 모이면 자동 선택한다. 여러 Agency에 걸치면
  사용자가 대상 Agency를 선택해야 하며 Source에 agency_id를 다시 소유시키지 않는다.
- baseline preview는 읽기 전용이다. apply 시 계획을 다시 계산하고 exact conservative
  match가 안전한 OrgUnit, Duty, ContactPoint만 단일 transaction으로 반영한다.
- 사람 이름은 발견/검토 정보다. Person과 PersonAssignment는 자동 생성하지 않으며,
  사람 이름이 있어도 안전한 조직·업무·연락처 반영을 막지 않는다.
- 확정되거나 exact match된 OrgUnit, Duty, ContactPoint는 Observation 기반의
  idempotent SourceOccurrence로 출처를 보존한다. 생성 entity는 APPROVED
  ChangeEvent를, 생성 ContactPoint는 최초 ContactHistory를 가진다.
- Source.coverage_mode는 UNKNOWN, ADDITIVE_ONLY, COMPLETE_SNAPSHOT 중 하나다.
  UNKNOWN과 ADDITIVE_ONLY에서는 부재를 삭제 신호로 사용하지 않는다.
  COMPLETE_SNAPSHOT에서만 신뢰 가능한 비모호 extraction의 부재 후보를 만든다.
- ChangeDetection이 detection run을, DetectedChangeCandidate가 근거와 review 상태를
  소유한다. 일반 HTML 연락처 후보는 binding 하나일 때도 자동 확정하지 않고 검토
  후보가 되며, 여러 binding이면 CONTEXT_REQUIRED 비실행 후보가 된다.
- 검토 상태는 PENDING_REVIEW, APPROVED, REJECTED, DEFERRED다. 반영은 현재 DB를
  transaction 안에서 재검증하고 확정값을 변경한다. 유지는 REJECTED로 확정값을
  보존하고, 보류는 DEFERRED로 다시 검토할 수 있게 남긴다.
- 연락처 교체는 이전 활성 ContactHistory를 닫고 새 활성 history를 만든다.
  missing 승인은 물리 삭제 대신 active=false로 전환한다. stale 후보는 덮어쓰지 않고
  재검토 충돌을 반환한다.
- /contacts는 ContactPoint만 읽고 confirmed context별 전화·이메일·팩스를 묶는다.
  공식 출처는 ContactPoint → SourceOccurrence → Observation → Source로 계산한다.
- /review는 DetectedChangeCandidate만 읽는다. 비실행 후보의 반영 버튼은 비활성이다.
- Source 운영 상태는 수집·RAW·추출 상태가 계속 소유한다. 검토 대기 존재 여부는
  Source의 정상/오류/자료없음 상태를 바꾸지 않는다.

## 05B Three-Way Collection Contract

- 사용자 SSOT는 개별 URL · 스크래핑, Index URL · 크롤링, 공개 API / RSS다.
  내부 방식은 각각 WEB_PAGE, WEB_CRAWL, API이고 API subtype은 OPEN_API, RSS,
  ATOM이다. 하나의 Source는 정확히 하나의 현재 방식과 해당 typed config만 소유한다.
- SourceScrapeConfig는 연락처/직원명부 extractor 선택을, SourceCrawlConfig는
  PATH_PREFIX/SAME_DOMAIN, 허용 경로, 깊이·페이지·간격 한도를,
  SourceApiConfig는 subtype, JSON/XML/CSV mapping, bounded PAGE_NUMBER,
  non-secret parameter, auth metadata와 catalog provenance를 소유한다.
- 크롤링은 순차 실행, 최대 깊이 5, 최대 페이지 200, 최소 요청 간격 500ms이며
  robots.txt, SSRF, redirect와 scope 검증을 우회하지 않는다. 성공한 각 page는 같은
  CrawlRun 아래 별도 Observation과 충돌 없는 project-relative RAW를 가진다.
- OpenAPI는 GET만 지원하고 단순 dot/element/header mapping 결과를 기존
  ExtractedDirectoryRecord/ExtractedContactCandidate discovery에 기록한다. RSS/Atom
  item은 ExtractedFeedItem discovery이며 Person/ContactPoint를 자동 생성하지 않는다.
- API secret은 Source URL, DB config, CrawlRun snapshot, log, HTML/JS/Git에 저장하지
  않는다. PROJECT_ROOT/config의 Git-ignored atomic credential store만 실제 값을
  소유하고 DB에는 opaque credential_ref만 둔다.
- CrawlRun은 실행 당시 method/kind/config의 비밀 없는 불변 snapshot과 bounded
  statistics를 소유한다. pre-05B snapshot 없는 run은 역사적으로 명확한 WEB_PAGE
  legacy projection으로만 읽는다.
- 확정 연락처 provenance와 export는 공식 Source URL, 실제 Observation URL, 실행 당시
  수집 방식을 보존한다. RSS item은 review/Master 경로를 우회하지 않는다.
- built-in catalog는 code-owned template이며 검증된 무인증 PublicDB 관련 항목만
  포함한다. 활성화는 Agency context를 요구하고 network나 collection을 시작하지 않는다.
- 자동 scheduler와 startup network는 계속 비활성이다.

## PORTABLE PROJECT CONTRACT

PublicDB2 설치 폴더가 application과 data의 이동 단위다. 기본 runtime 경로는 모두
PROJECT_ROOT 아래의 data/db, data/raw, data/imports, data/exports, data/temp,
logs, backups, config다. 사용자 profile, AppData, Desktop, Documents 또는
PublicDB1 경로를 runtime 소유 위치로 사용하지 않는다.

runtime DB, RAW, import/export/temp 파일, logs, backups, .env와 local config는
Git에서 제외한다. DB에 보존하는 import 파일 경로는 project-relative POSIX 경로다.
PUBLICDB2_DATABASE_URL과 PUBLICDB2_PROJECT_ROOT 환경 override를 지원한다.
배포 목표는 향후 publicdb.rhythmus.co.kr Windows Server web application이며,
PublicDB2 폴더 전체를 application/data migration unit으로 취급한다.

## 03B Source Import Contract

XLSX와 CSV import는 PREVIEW와 CONFIRM 두 단계다. Preview는 production DB를
쓰지 않으며 서버 token과 data/temp 원본만 사용한다. Confirm은 원본을 다시 파싱하고
현재 DB에 대해 재분류한 뒤 importable 행만 처리한다. 최대 행 수는 10,000개다.

행의 primary classification은 READY, NEW_AGENCY, NEW_ORG_UNIT,
EXACT_DUPLICATE, EXISTING_SOURCE_NEW_BINDING, CONFLICT, INVALID 중 하나다.
필수 컬럼은 기관명과 URL이고 부서명과 소스 설명은 선택이다. 기관과 부서는 정규화
exact match만 권한 있는 일치로 사용하며 fuzzy match나 타 기관 부서 cross-binding을
허용하지 않는다. EXACT_DUPLICATE는 건너뛰고 INVALID/CONFLICT는 쓰지 않는다.
확정 원본은 data/imports/YYYY/MM/DD 아래 충돌 안전 파일명으로 보존하며 원본명,
project-relative 저장 경로, SHA-256, 확정 시각과 결과 집계를 기록한다.

## 향후 action ownership

- 기관/부서 등록·수정: 기관/조직 기능이 소유한다.
- URL 추가·수정·수집·제외와 Excel import: 수집 소스 기능이 소유한다.
- 수집 실행과 단계 기록: 수집 서비스가 소유하고 수집 이력은 조회한다.
- 연락처 export: 연락처 DB 기능이 소유한다.
- 반영·유지·보류: 변경/검토 기능이 소유하며 이력 보존을 전제로 한다.
- 운영 설정 저장: 설정 기능이 소유한다.

기관/부서/업무, SourceBinding 등록·수정·제외·복구, Excel/CSV import, 05B
3-Way 수집·RAW·발견 추출·수집 이력과 04B 확정값 반영·변경 검토를 활성화한다.
Dashboard 실집계, typed 운영 설정, confirmed XLSX export와 인증/권한을 활성화한다.
자동 scheduler와 실제 Apache/HTTPS/Windows service 구성은 활성화하지 않는다.
