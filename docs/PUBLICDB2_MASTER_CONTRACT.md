# PublicDB2 Master Contract

상태: 향후 기능 구현의 제품·데이터 의미 SSOT

## 제품 목적과 경계

PublicDB2는 정부·공공기관이 공식 공개한 기관, 조직/부서, 업무, 사람/재직과
연락처를 출처 및 변경 근거와 함께 관리하는 로컬 운영 도구다. 03A부터 PublicDB2
소유 DB와 기관/부서/업무 및 수집 소스 등록·조회가 활성화됐다. 수집, 추출,
Excel 처리와 검토 반영은 아직 활성화하지 않는다.

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

## 향후 action ownership

- 기관/부서 등록·수정: 기관/조직 기능이 소유한다.
- URL 추가·수정·수집·제외와 Excel import: 수집 소스 기능이 소유한다.
- 수집 실행과 단계 기록: 수집 서비스가 소유하고 수집 이력은 조회한다.
- 연락처 export: 연락처 DB 기능이 소유한다.
- 반영·유지·보류: 변경/검토 기능이 소유하며 이력 보존을 전제로 한다.
- 운영 설정 저장: 설정 기능이 소유한다.

03A에서는 기관/부서/업무와 SourceBinding의 등록·수정·제외·복구 action만
활성화한다. 수집, Excel import, 검토 반영 및 설정 action은 후속 Goal에서 연결한다.
