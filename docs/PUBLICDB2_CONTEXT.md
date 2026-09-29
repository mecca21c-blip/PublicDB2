# PublicDB2 — 프로젝트 컨텍스트

작성일: 2026-09-29
문서 상태: Dashboard UI foundation
전체 UI Freeze: Dashboard shell만 구현, 업무 탭은 미완료
Production 기능 구현 승인: UI foundation만 승인

## 목적

PublicDB2는 정부·공공기관이 공식 공개한 기관·조직/부서·업무·사람/재직·연락처를
공식 출처, 수집 증거, 변경 이력과 함께 유지하는 로컬 데이터 관리 도구다.

## 프로젝트 경계

| 항목 | 값 | 취급 |
|---|---|---|
| 새 프로젝트 | `C:\PublicDB2` | PublicDB2 write root |
| Legacy reference | `C:\PublicDB` | 읽기 전용 |
| 원격 저장소 | `https://github.com/mecca21c-blip/PublicDB2.git` | PublicDB2 전용 |
| Legacy 감사 HEAD | `1319ae31ddf7234a1a8632271b5ff899006d47de` | 00A 감사 기준 |

PublicDB1 전체 코드, DB, RAW 파일, 설정을 복사하거나 직접 연결하지 않는다.
PublicDB1은 model/service/data contract를 확인할 때만 참고한다.

## 현재 구현 범위

`PUBLICDB2-DASHBOARD-UI-FOUNDATION-01A`에서 다음을 구현했다.

- FastAPI/Jinja2 application shell
- 어두운 sidebar와 top header
- 운영 Dashboard
- 공통 design token과 component
- `/`, `/agencies`, `/sources`, `/runs`, `/contacts`, `/review`, `/settings`
- Dashboard 외 route의 최소 `구현 예정` 화면

이번 단계에서는 DB, 수집, crawling, 추출, Master apply, migration, legacy DB import를
연결하지 않았다. Dashboard 값은 `app/web/dashboard_fixture.py`의 명시적 샘플이며
실제 운영 상태가 아니다.

## 새 프로젝트 원칙

- UI-first로 공통 shell과 Dashboard를 먼저 시각 검수한다.
- Dashboard가 공통 시각 언어의 SSOT다.
- Dashboard 외 업무 화면은 `검색 → 목록 → 선택 → 작업` 흐름으로 단순화한다.
- 발견 후보와 Master 확정 데이터를 같은 상태처럼 표현하지 않는다.
- 샘플 데이터는 production 값과 분리하고 화면에 샘플임을 표시한다.
- 기능이 연결되지 않은 route와 control은 `구현 예정`임을 명확히 표시한다.
- 실제 기능은 현재 코드, 격리 테스트, 운영 검증을 구분해 확인한 뒤 계승한다.

## 문서 기준

- `UI_CONTRACT.md`: application shell, Dashboard, 향후 탭 UI 계약
- `FEATURE_INHERITANCE.md`: PublicDB1 자산의 계승 분류
- `UI_SPEC.md`: 기존 상세 화면 논의 초안
- `FEATURE_MAP.md`: 기존 구현과 제안 화면의 상세 대응 자료
- `DECISIONS.md`: 확정/제안/미결정 기록
- `design/01-dashboard-reference-v0.1.png`: 시각 참고 이미지
- `evidence/PUBLICDB1_INVENTORY_00A.txt`: PublicDB1 읽기 전용 감사 결과

기준 이미지의 수치, 기관명, URL, 날짜, 수집 결과는 제품 계약이 아니다.

## 진행 방식

1. Dashboard shell의 시각 수용 여부를 확인한다.
2. 수집 소스, 연락처 DB, 변경/검토 화면의 계약을 차례로 확정한다.
3. 필요한 legacy 서비스는 PublicDB2 경계에서 다시 검증한다.
4. DB와 실제 업무 기능은 별도 Goal에서 연결한다.
