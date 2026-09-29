# PublicDB2 UI 설계 초안 패키지 v0.1

이 패키지는 현재 디자인 방향과 기존 감사 결과를 보존하기 위한 문서 묶음입니다.
아직 전체 UI 확정안이나 Codex 구현 지시문이 아닙니다.

## 배치 위치

압축 안의 `docs` 폴더를 `C:\PublicDB2` 아래에 배치하면 됩니다.
같은 이름의 파일이 이미 있으면 먼저 비교하고 덮어쓰지 마세요.
압축 내용은 PublicDB1 코드, DB, 원문 수집 데이터, 비밀키를 포함하지 않습니다.

## 파일

- docs/PUBLICDB2_CONTEXT.md: 프로젝트 목적, 경로, 승인 범위, 진행 순서
- docs/UI_SPEC.md: 11개 논의용 화면, 주요 팝업, 상태와 Freeze 체크 기준
- docs/FEATURE_MAP.md: 기존 구현과 새 화면의 대응 및 미구현 영역
- docs/DECISIONS.md: 확정 요구/시각 방향/제안/미결정 구분
- docs/design/01-dashboard-reference-v0.1.png: 현재 대시보드 시각 참고 이미지
- docs/evidence/PUBLICDB1_INVENTORY_00A.txt: 사용자가 제공한 Codex 감사 원문

이번 단계에는 Codex 실행, 앱 구동, Git 초기화, 원격 업로드가 필요하지 않습니다.
다른 화면 시안을 검토해 합의한 후 이 문서를 갱신하고 구현 지시문을 별도로 작성합니다.
