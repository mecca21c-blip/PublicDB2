# PublicDB2 Master Contract

상태: 향후 기능 구현의 제품·데이터 의미 SSOT

## 제품 목적과 경계

PublicDB2는 정부·공공기관이 공식 공개한 기관, 조직/부서, 업무, 사람/재직과
연락처를 출처 및 변경 근거와 함께 관리하는 로컬 운영 도구다. PublicDB2 소유 DB,
기관/부서/업무와 수집 소스 관리, Excel/CSV Source import, 명시적 WEB_PAGE
수집·RAW 증거·결정적 발견 후보 추출과 실제 수집 이력이 활성화됐다. 발견 후보의
확정 연락처 반영과 변경/검토 처리는 아직 활성화하지 않는다.

PublicDB1(C:\PublicDB)은 읽기 전용 legacy reference다. 코드나 DB를 통째로
복사하거나 직접 연결하지 않으며, 확인된 의미와 재검증 가능한 로직만 별도 Goal에서
계승한다.

## Routes와 책임

| Route | 책임 |
|---|---|
| / | 전체 운영 상태를 요약하는 유일한 Dashboard |
| /agencies | 기관 → 부서 → 업무와 연결 소스 관계 관리 |
| /sources | 기관/부서별 수집 URL, 확인 상태와 제외 상태 관리 |
| /runs | 수집 시도와 처리 단계별 결과 확인 |
| /contacts | 확정 연락처 검색, 출처와 변경 이력 확인 |
| /review | 발견값과 확정값 비교 및 향후 처리 판단 |
| /settings | 최소 운영 저장·수집 정책 설정 |

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

## 04A Live Collection Contract

- canonical Source가 CrawlRun과 Observation을 소유한다. SourceBinding은 기관과
  선택적 부서 context만 소유하며 binding 수만큼 같은 URL을 다시 수집하지 않는다.
- 사용자의 단일 수집 action이 안전한 HTTP GET, RAW 저장, Observation, 연락처와
  직원명부 추출, 발견 후보 저장, CrawlRun 확정을 연속 수행한다.
- 활성 지원 방식은 CollectionMethod.WEB_PAGE의 등록된 단일 URL뿐이다. API,
  WEB_CRAWL, FILE, DOCUMENT와 자동 실행·scheduler는 비활성이다.
- RAW는 PROJECT_ROOT/data/raw 아래 날짜/source/run 경로에 atomic 저장하고 DB에는
  project-relative POSIX 경로, SHA-256과 응답 byte 수만 기록한다.
- 연락처 후보와 직원명부 행은 discovery data다. ContactPoint, Person,
  PersonAssignment, SourceOccurrence, ChangeEvent와 ContactHistory를 생성하지 않는다.
- 두 extractor가 모두 성공하고 발견 수가 1건 이상이면 정상, 모두 성공하고 0건이면
  자료없음이다. 접속/RAW 실패, partial extraction, 미지원 content는 오류다.
  binding 제외 상태는 이 공통 Source 실행 상태보다 우선한다.
- /runs는 CrawlRun/Observation/ExtractionRun의 실 DB projection이며 기관 filter는
  SourceBinding을 통해 해석한다. 여러 binding은 요약과 전체 context로 표시한다.

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

기관/부서/업무, SourceBinding 등록·수정·제외·복구, Excel/CSV import와 04A
WEB_PAGE 수집·RAW·발견 추출·수집 이력을 활성화한다. 확정값 반영, 변경/검토,
Dashboard 실집계와 설정 action은 현재 범위 밖이다.
