# PublicDB2

## Desktop and server launch

For a normal Windows desktop session, install the desktop extra once and then
double-click `PublicDB2.cmd` in the project root.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"
```

The desktop launcher starts an internal loopback-only Uvicorn server on an
available port, waits for `/health` and `/ready`, and opens one PublicDB2
window. Closing that window stops the server owned by the launcher. It does not
install packages or run database migrations automatically. For startup
diagnostics with a console, run:

```powershell
.\.venv\Scripts\python.exe scripts\run_desktop.py
```

For development or independent web-server operation, continue to use the
existing server launcher:

```powershell
.\.venv\Scripts\python.exe scripts\run_web.py
```

PublicDB2는 정부·공공기관이 공식 공개한 기관, 조직, 업무, 사람, 연락처를
공식 출처와 이력에 연결해 관리하는 로컬 운영 도구다.

FastAPI/Jinja2 application shell과 SQLAlchemy 2/Alembic 기반 독립 DB를 사용한다.
기관/부서/업무, 수집 소스, import, 명시적 3-Way 수집(WEB_PAGE/WEB_CRAWL/API),
RAW/추출, 확정 연락처,
검토, Dashboard, Settings와 XLSX export가 실 DB에 연결됐다. application user의
서명 세션과 ADMIN/OPERATOR/VIEWER 권한, CSRF 보호를 사용한다.

## 실행

Python 3.12 이상 환경에서 의존성을 설치한 뒤 실행한다.

```powershell
python scripts/run_web.py
```

기본 계정은 없다. migration 후 최초 ADMIN은 비밀번호를 안전한 prompt로 입력한다.

```powershell
python scripts/manage_user.py create --username admin --role ADMIN
```

기본 주소는 `http://127.0.0.1:8000`이다.

DB, import, RAW, export, temp, log, backup과 local config 기본 경로는 모두
PublicDB2 설치 폴더 아래에 있으며 runtime 파일은 Git에 포함하지 않는다.

## 화면

- `/`: Dashboard
- `/login`: 로그인
- `/agencies`: 기관/조직
- `/sources`: 수집 소스
- `/runs`: 수집 이력
- `/contacts`: 확정 연락처 DB와 XLSX export
- `/review`: 변경/검토
- `/settings`: ADMIN 운영 설정과 사용자 관리

Dashboard와 workspace는 fixture fallback 없이 PublicDB2 DB의 실제 값을 읽는다.

## 문서

- `docs/PUBLICDB2_CONTEXT.md`: 프로젝트 경계와 진행 원칙
- `docs/UI_CONTRACT.md`: UI 구조와 향후 탭 계약
- `docs/FEATURE_INHERITANCE.md`: PublicDB1 기능 계승 분류
- `docs/design/01-dashboard-reference-v0.1.png`: Dashboard 시각 참고
- `docs/evidence/PUBLICDB1_INVENTORY_00A.txt`: PublicDB1 읽기 전용 감사 결과
