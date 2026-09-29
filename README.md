# PublicDB2

PublicDB2는 정부·공공기관이 공식 공개한 기관, 조직, 업무, 사람, 연락처를
공식 출처와 이력에 연결해 관리하는 로컬 운영 도구다.

FastAPI/Jinja2 application shell과 SQLAlchemy 2/Alembic 기반 독립 DB를 사용한다.
기관/부서/업무 및 수집 소스 binding은 실 DB에 연결됐다. Dashboard와 나머지
업무 화면은 명시적 sample/demo 상태이며 수집·추출은 아직 실행하지 않는다.

## 실행

Python 3.12 이상 환경에서 의존성을 설치한 뒤 실행한다.

```powershell
python scripts/run_web.py
```

기본 주소는 `http://127.0.0.1:8000`이다.

## 화면

- `/`: Dashboard
- `/agencies`: 기관/조직 placeholder
- `/sources`: 수집 소스 placeholder
- `/runs`: 수집 이력 placeholder
- `/contacts`: 연락처 DB placeholder
- `/review`: 변경/검토 placeholder
- `/settings`: 설정 placeholder

Dashboard 데이터는 `app/web/dashboard_fixture.py`의 명시적 샘플이다. 실제 기관,
수집 결과 또는 production DB 값이 아니다.

## 문서

- `docs/PUBLICDB2_CONTEXT.md`: 프로젝트 경계와 진행 원칙
- `docs/UI_CONTRACT.md`: UI 구조와 향후 탭 계약
- `docs/FEATURE_INHERITANCE.md`: PublicDB1 기능 계승 분류
- `docs/design/01-dashboard-reference-v0.1.png`: Dashboard 시각 참고
- `docs/evidence/PUBLICDB1_INVENTORY_00A.txt`: PublicDB1 읽기 전용 감사 결과
