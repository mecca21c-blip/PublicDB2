# PublicDB2 UI Contract

상태: Dashboard UI foundation 기준 계약

## 1. Visual SSOT

Dashboard가 PublicDB2 공통 application shell과 시각 언어의 기준이다.
`design/01-dashboard-reference-v0.1.png`의 어두운 남색 sidebar, 밝은 본문,
파란 primary accent, 카드·표·badge의 밀도를 참고한다. 이미지 안의 기관명,
수치, URL, 날짜와 수집 결과는 샘플이며 기능 또는 데이터 계약이 아니다.

공통 token은 `app/web/static/css/tokens.css`, 공통 component는
`components.css`와 `templates/components.html`, shell은 `app.css`와
`templates/base.html`이 소유한다. 화면 전용 CSS는 공통 규칙을 재정의하지 않는다.

## 2. Application shell

- Sidebar: Dashboard, 기관/조직, 수집 소스, 수집 이력, 연락처 DB, 변경/검토, 설정.
- Top header: 통합 검색, 빠른 등록, 알림, 사용자 영역의 위치만 제공한다.
- 아직 연결되지 않은 header control은 동작하는 기능처럼 표시하지 않는다.
- Dashboard 외 route는 현재 제목과 `구현 예정` 안내만 보여준다.
- desktop admin layout을 우선하며 1920, 1600, 1366 폭에서 shell을 유지한다.
- 폭이 좁아지면 sidebar는 축약되고 KPI와 panel은 wrap한다. 표는 자체 영역에서
  가로 스크롤하며 전체 shell 폭을 밀지 않는다.

## 3. Dashboard contract

Dashboard만 overview 화면이다. 포함 범위는 다음과 같다.

- KPI 최대 5개: 등록 기관, 수집 Source, 연락처 DB, 검토 대기, 수집 오류.
- 최근 수집: 수집 시각, 기관명, Source, 상태만 요약한다.
- 최근 7일 성공/오류 추이 chart 하나.
- 최근 검토 대상: 기관, 변경 항목, 이전 값, 새 값, 발견 시각, 상태.
- Quick Action: 기관 등록, Source 등록, 수집 이력, 검토 대상 route 이동.

실제 DB가 연결되기 전 sample은 별도 fixture에 격리하고 화면에 샘플임을 표시한다.
발견 후보와 Master 확정 데이터는 같은 상태나 수치로 표현하지 않는다.

## 4. Future tab contract

Dashboard 이외 화면은 `검색 → 목록 → 선택 → 작업` 흐름을 우선한다. KPI, graph,
마케팅형 widget을 반복하지 않는다.

### 기관/조직

기관 검색과 목록을 제공하고 선택 기관 상세에서 부서, 업무, 연결 Source를 확인한다.
업무는 별도 전역 메뉴가 아니라 기관/조직 문맥에 포함한다.

### 수집 소스

검색/필터와 Source 목록을 중심으로 URL 추가, Excel upload, 단계별 상태 확인,
검토·제외를 제공한다. URL과 기관/부서 연결 관계의 중복을 구분한다.

### 수집 이력

수집 실행별로 접속, RAW 저장, 추출, 결과 상태를 분리해 표시한다. HTTP 성공을
추출 성공이나 Master 반영 성공으로 표시하지 않는다.

### 연락처 DB

검색/필터와 핵심 연락처 table을 제공한다. 상세에서 공식 출처, 관찰 근거,
소속·업무 문맥, 변경 이력을 확인한다.

### 변경/검토

기존 값과 발견 값을 비교해 반영, 유지, 보류를 명시적으로 결정한다. 검토 대기는
이 화면 안에 포함하며 자동 반영을 기본값으로 삼지 않는다.

### 설정

실제 운영에 필요한 최소 설정만 제공한다. 구현 근거가 없는 계정, 테마, 자동화,
백업 기능을 임의로 추가하지 않는다.

## 5. Status and accessibility

- 상태는 색상과 텍스트를 함께 사용한다.
- 로딩 실패를 0건 또는 자료없음으로 바꾸지 않는다.
- 미수집, 접근 오류, 추출 오류, 미지원, 미확인, 자료없음을 구분한다.
- 긴 URL과 명칭은 shell을 밀지 않도록 잘라 표시하되 전체 값 접근 경로를 둔다.
- 가짜 기능 버튼은 활성 상태로 두지 않는다.
