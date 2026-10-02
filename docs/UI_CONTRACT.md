# PublicDB2 UI Contract

상태: PUBLICDB2_UI_FOUNDATION_FREEZE=YES

## 공통 시각 계약

Dashboard가 application shell과 시각 언어의 SSOT다. 어두운 sidebar, 밝은
workspace, 파란 primary accent, 표·badge·form control의 밀도를 모든 화면에서
공유한다. 공통 token과 component는 tokens.css, components.css, app.css,
workspace.css, templates/components.html이 소유한다.

Sidebar 메뉴는 대시보드, 기관/조직, 수집 소스, 수집 이력, 연락처 DB, 변경/검토,
설정의 7개만 유지한다. Dashboard만 KPI와 chart를 갖는 overview다. 나머지 화면은
제목 → 검색/필터/작업 → 목록 → 선택 상세 순서를 따른다.

Dashboard와 모든 workspace는 실 DB read model을 사용한다. 빈 DB는 실제 0/빈 상태로,
DB 장애는 fixture fallback 없이 명시적 한국어 오류로 표시한다.

인증되지 않은 운영 화면은 /login으로 이동한다. 공통 header에는 현재 사용자명,
역할과 POST 로그아웃만 간결하게 표시하며 알림·가상 avatar·profile dashboard는 없다.
Settings 메뉴는 ADMIN에게만 표시한다.

## 동결 workspace

### /agencies 기관/조직

기관 검색과 기관 목록을 제공한다. 목록은 기관명, 유형, 부서·연락처·소스 수와
상태만 표시한다. 선택 상세는 기관 기본 정보, 조직/부서, 업무, 연결된 수집 소스를
한 패널에서 보여준다.

### /sources 수집 소스

기관·부서·상태 필터와 URL 목록을 중심으로 한다. 상태는 미확인, 정상, 자료없음,
오류, 제외를 사용한다. 선택 상세에서 설명, 최근 확인·수집 결과, 발견 수, 오류
사유를 확인한다. Excel 진입 화면은 파일 선택부터 등록까지의 단계와 정상·중복·
충돌·오류 미리보기를 제공하며 preview/confirm과 실제 등록에 연결된다.
목록과 상세는 수집 방식을 텍스트로 표시한다. Source create의 현재 순서는
기본 정보 → 수집 설정 → 등록 확인이다. 기본 정보에서 개별 URL · 스크래핑,
Index URL · 크롤링, 공개 API / RSS 중 하나를 먼저 선택하고 URL별 기관·부서를 행으로
확인한다. WEB_PAGE는 최대 200개 URL을 줄바꿈으로 붙여넣을 수 있으며 각 행은 서로
다른 Agency와 OrgUnit을 가질 수 있다. 부서가 빈 행은 기관 전체 연결이다. URL 입력 후
동시 2개의 bounded metadata 확인을 수행하지만 발견값은 편집 가능한 제안이며 정확
일치 ID 또는 사용자가 확인한 신규 생성 의도만 다음 단계로 전달한다. 미등록 기관과
부서는 Step 3 최종 등록 전까지 DB에 쓰지 않는다. 크롤링은 Index URL 하나,
RSS/Atom은 feed URL 하나, OpenAPI는 endpoint 하나만 사용한다. 200개를 넘는 구조화된
목록은 기존 10,000행 Excel import가 소유한다. built-in catalog와 직접 API/RSS 추가도
같은 modal 안에 있고 별도 sidebar route는 만들지 않는다.

Source wizard의 하단 action bar는 modal 내부의 유일한 세로 scroll owner에 속하며,
현재 단계에 필요한 버튼만 layout과 tab order에 남긴다. 짧은 단계는 불필요한 scroll을
만들지 않고 긴 고급 설정만 scroll한다. Sources 목록은 일반 desktop 폭에서 전용 표가
가로 scroll을 강제하지 않으며, DB 자체가 비었을 때와 필터 결과만 비었을 때를 서로 다른
안내로 표시한다. 필터 결과 전체 및 전체 소스 즉시 수집은 서버가 다시 계산한 방식별
대상 수를 확인한 뒤에만 background CollectionJob을 만든다.

### /runs 수집 이력

기간·기관·상태·검색 필터와 실행 목록을 제공한다. 선택 상세에서 접속, RAW 저장,
추출, 발견 결과, 확정 DB 반영 여부를 분리한다. 접속 성공은 추출 성공이나 확정
DB 반영을 의미하지 않는다.
수집 방식은 현재 Source가 아니라 실행 당시 CrawlRun snapshot에서 표시한다. 다중
Observation 실행은 method config와 통계, 각 RAW evidence를 run context로 유지한다.

### /contacts 연락처 DB

확정 연락처 검색과 기관·부서·연락처 유형 필터를 제공한다. 목록은 기관, 부서,
업무, 담당자, 전화, 이메일만 표시한다. 조직 연락처에는 사람 값을 강제하지 않는다.
선택 상세에서 팩스, 공식 출처, 확인일과 최근 변경 이력을 확인한다.
공식 출처 URL과 함께 실제 발견 URL/endpoint 및 수집 방식을 provenance로 표시한다.

### /review 변경/검토

현재 확정값과 새 발견값을 비교한다. 목록은 기관, 대상, 항목, 두 값, 발견 시각과
상태를 표시한다. 선택 상세에서 출처와 근거를 확인하고 반영, 유지, 보류를 처리한다.
검토 대기, 확인 필요, 보류는 서로 다른 semantic tone을 사용한다.

### /settings 설정

ADMIN 전용이다. HTTP timeout, 최대 response bytes, PublicDB User-Agent를 저장하고,
project-owned 경로와 3-Way 수집/자동 수집 미사용 capability를 read-only로 표시한다.
같은 workspace 아래에서 사용자 목록·생성·역할·활성 상태·비밀번호 재설정을 제공하며
password hash는 표시하지 않는다. 개발자 옵션과 안전 경계 해제 control은 노출하지 않는다.

## 상호작용과 접근성

- 행 선택과 상세 전환은 client-side이며 실제 mutation은 role과 CSRF를 서버에서 검증한다.
- 연락처의 엑셀 내보내기는 현재 검색·기관·부서·유형 filter로 확정값만 다운로드한다.
- 상태는 색과 텍스트를 함께 사용한다.
- 긴 URL은 표에서 줄이되 선택 상세에서 전체 값을 제공한다.
- 표는 자체 가로 스크롤로 shell 너비를 보호한다.
- 실제 기능이 없는 작업 버튼은 disabled 또는 aria-disabled 상태다.
