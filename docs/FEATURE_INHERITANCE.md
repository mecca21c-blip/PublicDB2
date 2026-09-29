# PublicDB2 Feature Inheritance

기준: PublicDB1 감사 HEAD `1319ae31ddf7234a1a8632271b5ff899006d47de`.
이번 UI foundation에서는 아래 기능을 이식하거나 DB에 연결하지 않는다.

분류는 `REUSE`, `REUSE_AFTER_VERIFICATION`, `NEW_IMPLEMENTATION`,
`DO_NOT_INHERIT` 네 가지로만 한다.

| PublicDB1 자산/기능 | 분류 | 이유 |
|---|---|---|
| Agency, OrgUnit, Duty 기본 개념 | REUSE_AFTER_VERIFICATION | 제품의 핵심 데이터 계약이나 PublicDB2 관계 모델 확정 전 재검증 필요 |
| Person / PersonAssignment | REUSE_AFTER_VERIFICATION | 모델은 있으나 기존 promotion apply가 사람·재직을 생성하지 않음 |
| ContactPoint와 발견 후보 분리 | REUSE | 발견과 Master 확정을 분리하는 의미는 유지 |
| Source, CrawlRun, Observation, SourceOccurrence | REUSE_AFTER_VERIFICATION | provenance 계약은 유효하나 Source-기관/부서 관계는 새 요구와 불일치 |
| ChangeEvent, ChangeDetection, ContactHistory | REUSE_AFTER_VERIFICATION | 후보/이력 모델은 참고하되 승인·반영 workflow가 미완성 |
| HTTP 단일 페이지 수집 로직 | REUSE_AFTER_VERIFICATION | 격리 테스트는 있으나 현장 실행 검증 근거가 없음 |
| HTML 연락처·직원명부 추출 | REUSE_AFTER_VERIFICATION | 결정론적 로직과 테스트는 있으나 PublicDB2 연결 전 재검증 필요 |
| Master preview/apply 서비스 | REUSE_AFTER_VERIFICATION | 원자적 경계는 참고하되 사람 처리와 review writer가 미완성 |
| Source URL 정규화 | REUSE_AFTER_VERIFICATION | 검증 로직은 유용하지만 URL 전역 unique 계약은 그대로 계승 불가 |
| 기관명 직접 입력 UI | NEW_IMPLEMENTATION | PublicDB1 웹 UI에는 없음 |
| 기관/부서별 복수 URL 연결 | NEW_IMPLEMENTATION | Source에 부서 연결이 없고 URL이 전역 unique임 |
| Excel URL 일괄 upload와 충돌 검토 | NEW_IMPLEMENTATION | 구현 없음 |
| 단계별 상태와 자료없음 판정 | NEW_IMPLEMENTATION | 기존 Dashboard는 최신 CrawlRun 중심이라 추출/미지원 상태를 충분히 구분하지 못함 |
| Source 제외·비활성·삭제 영향 workflow | NEW_IMPLEMENTATION | 사용자 UI/API 없음 |
| 검토 후보 승인·유지·보류 UI와 writer | NEW_IMPLEMENTATION | 기존에는 pending 집계/후보 저장까지만 존재 |
| PublicDB1 Dashboard HTML/CSS/JS | DO_NOT_INHERIT | PublicDB2 visual foundation을 새로 구성하며 legacy 화면을 복구하지 않음 |
| 비활성 legacy 조직 탐색 UI | DO_NOT_INHERIT | 현재 활성 경로가 아니고 승인된 PublicDB2 설계가 아님 |
| 지역 지도 placeholder와 가상 workflow | DO_NOT_INHERIT | 실제 기능 계약이나 검증 근거 없음 |
| PublicDB1 production DB와 RAW 파일 | DO_NOT_INHERIT | 직접 연결·복사·import 금지 |

`REUSE`는 의미 계약의 유지이며 코드 전체 복사를 뜻하지 않는다.
`REUSE_AFTER_VERIFICATION`은 현재 코드, 격리 테스트, 실제 운영 검증을 분리해 확인한
뒤 PublicDB2 경계에 맞게 가져온다는 뜻이다.
