"""Small, explicit demo fixtures for PublicDB2 workspace layout validation.

These values are not production records and are never persisted.
"""

from __future__ import annotations

from typing import Final


AGENCIES_FIXTURE: Final = {
    "items": (
        {"id": "agency-a", "name": "한빛시청", "type": "지방자치단체", "departments": 14, "contacts": 86, "sources": 7, "status": "운영", "tone": "success"},
        {"id": "agency-b", "name": "새봄환경공단", "type": "지방공기업", "departments": 8, "contacts": 41, "sources": 4, "status": "확인 필요", "tone": "warning"},
        {"id": "agency-c", "name": "누리문화재단", "type": "공공기관", "departments": 5, "contacts": 23, "sources": 3, "status": "운영", "tone": "success"},
    ),
    "details": (
        {
            "id": "agency-a", "name": "한빛시청", "type": "지방자치단체", "identifier": "샘플-001", "address": "샘플시 중앙로 100",
            "departments": ("기획조정실", "민원서비스과", "디지털정보과"),
            "tasks": ("정책 기획", "통합 민원 안내", "공공데이터 운영"),
            "sources": ("직원 안내 페이지", "부서별 업무 안내", "공식 조직도"),
        },
        {
            "id": "agency-b", "name": "새봄환경공단", "type": "지방공기업", "identifier": "샘플-002", "address": "샘플시 푸른길 24",
            "departments": ("경영지원부", "환경사업부"), "tasks": ("시설 운영", "환경 민원"),
            "sources": ("대표 연락처", "부서 안내"),
        },
        {
            "id": "agency-c", "name": "누리문화재단", "type": "공공기관", "identifier": "샘플-003", "address": "샘플시 문화로 7",
            "departments": ("운영지원팀", "문화사업팀"), "tasks": ("문화행사 운영", "대관 안내"),
            "sources": ("조직도", "업무 담당자 목록"),
        },
    ),
}

SOURCES_FIXTURE: Final = {
    "items": (
        {"id": "source-a", "agency": "한빛시청", "department": "디지털정보과", "url": "https://hanbit.example.test/staff", "checked": "오늘 14:20", "status": "정상", "tone": "success", "found": 18},
        {"id": "source-b", "agency": "한빛시청", "department": "민원서비스과", "url": "https://hanbit.example.test/service", "checked": "오늘 12:36", "status": "자료없음", "tone": "info", "found": 0},
        {"id": "source-c", "agency": "새봄환경공단", "department": "환경사업부", "url": "https://saebom.example.test/contact", "checked": "오늘 11:54", "status": "오류", "tone": "danger", "found": 0},
        {"id": "source-d", "agency": "누리문화재단", "department": "-", "url": "https://nuri.example.test/organization", "checked": "-", "status": "미확인", "tone": "neutral", "found": "-"},
        {"id": "source-e", "agency": "새봄환경공단", "department": "경영지원부", "url": "https://saebom.example.test/archive", "checked": "9월 27일", "status": "제외", "tone": "warning", "found": 6},
    ),
    "details": (
        {"id": "source-a", "agency": "한빛시청", "department": "디지털정보과", "url": "https://hanbit.example.test/staff", "description": "부서별 담당자와 업무를 안내하는 공식 페이지", "checked": "오늘 14:20", "result": "접속 및 추출 성공", "found": "18건", "error": "-"},
        {"id": "source-b", "agency": "한빛시청", "department": "민원서비스과", "url": "https://hanbit.example.test/service", "description": "민원 업무 안내 페이지", "checked": "오늘 12:36", "result": "정상 확인, 대상 자료 없음", "found": "0건", "error": "-"},
        {"id": "source-c", "agency": "새봄환경공단", "department": "환경사업부", "url": "https://saebom.example.test/contact", "description": "환경사업부 연락처 안내", "checked": "오늘 11:54", "result": "접근 실패", "found": "0건", "error": "응답 시간 초과"},
        {"id": "source-d", "agency": "누리문화재단", "department": "-", "url": "https://nuri.example.test/organization", "description": "재단 조직 안내", "checked": "-", "result": "검사 전", "found": "-", "error": "-"},
        {"id": "source-e", "agency": "새봄환경공단", "department": "경영지원부", "url": "https://saebom.example.test/archive", "description": "이전 조직 안내 보관 페이지", "checked": "9월 27일", "result": "사용자 제외", "found": "6건", "error": "-"},
    ),
    "import_rows": (
        {"agency": "한빛시청", "department": "기획조정실", "url": "https://hanbit.example.test/plan", "description": "기획 업무 안내", "result": "정상", "tone": "success"},
        {"agency": "한빛시청", "department": "민원서비스과", "url": "https://hanbit.example.test/service", "description": "민원 업무 안내", "result": "중복", "tone": "warning"},
        {"agency": "새봄환경공단", "department": "환경사업부", "url": "https://hanbit.example.test/plan", "description": "타 기관과 URL 중복", "result": "충돌", "tone": "danger"},
        {"agency": "-", "department": "운영팀", "url": "잘못된 URL", "description": "필수값 확인 필요", "result": "오류", "tone": "danger"},
    ),
}

RUNS_FIXTURE: Final = {
    "items": (
        {"id": "run-a", "time": "오늘 14:20", "agency": "한빛시청", "source": "직원 안내 페이지", "result": "추출 성공", "tone": "success", "found": 18, "duration": "4.2초"},
        {"id": "run-b", "time": "오늘 12:36", "agency": "한빛시청", "source": "민원 업무 안내", "result": "대상 자료 없음", "tone": "info", "found": 0, "duration": "2.8초"},
        {"id": "run-c", "time": "오늘 11:54", "agency": "새봄환경공단", "source": "환경사업부 연락처", "result": "접속 오류", "tone": "danger", "found": 0, "duration": "15.0초"},
        {"id": "run-d", "time": "어제 16:08", "agency": "누리문화재단", "source": "조직도", "result": "추출 오류", "tone": "warning", "found": 0, "duration": "3.6초"},
    ),
    "details": (
        {"id": "run-a", "title": "한빛시청 · 직원 안내 페이지", "reason": "-", "stages": (("접속", "성공", "success"), ("RAW 저장", "완료", "success"), ("추출", "성공", "success"), ("발견 결과", "18건", "info"), ("확정 DB 반영", "미반영", "neutral"))},
        {"id": "run-b", "title": "한빛시청 · 민원 업무 안내", "reason": "정상 확인 후 대상 자료가 발견되지 않았습니다.", "stages": (("접속", "성공", "success"), ("RAW 저장", "완료", "success"), ("추출", "성공", "success"), ("발견 결과", "0건", "info"), ("확정 DB 반영", "해당 없음", "neutral"))},
        {"id": "run-c", "title": "새봄환경공단 · 환경사업부 연락처", "reason": "요청 제한 시간 안에 응답이 도착하지 않았습니다.", "stages": (("접속", "오류", "danger"), ("RAW 저장", "미수행", "neutral"), ("추출", "미수행", "neutral"), ("발견 결과", "미확인", "neutral"), ("확정 DB 반영", "미반영", "neutral"))},
        {"id": "run-d", "title": "누리문화재단 · 조직도", "reason": "접속과 RAW 저장은 성공했지만 지원되는 구조를 찾지 못했습니다.", "stages": (("접속", "성공", "success"), ("RAW 저장", "완료", "success"), ("추출", "오류", "danger"), ("발견 결과", "미확인", "neutral"), ("확정 DB 반영", "미반영", "neutral"))},
    ),
}

CONTACTS_FIXTURE: Final = {
    "items": (
        {"id": "contact-a", "agency": "한빛시청", "department": "민원서비스과", "task": "통합 민원 안내", "person": "-", "phone": "02-0000-1100", "email": "civil@example.test"},
        {"id": "contact-b", "agency": "한빛시청", "department": "디지털정보과", "task": "공공데이터 운영", "person": "김하늘", "phone": "02-0000-1200", "email": "data@example.test"},
        {"id": "contact-c", "agency": "새봄환경공단", "department": "환경사업부", "task": "환경 민원", "person": "-", "phone": "02-0000-2100", "email": "green@example.test"},
        {"id": "contact-d", "agency": "누리문화재단", "department": "문화사업팀", "task": "대관 안내", "person": "이누리", "phone": "02-0000-3100", "email": "space@example.test"},
    ),
    "details": (
        {"id": "contact-a", "agency": "한빛시청", "department": "민원서비스과", "task": "통합 민원 안내", "person": "-", "phone": "02-0000-1100", "email": "civil@example.test", "fax": "02-0000-1199", "source": "https://hanbit.example.test/service", "verified": "2026-09-29", "history": "부서 전화번호 변경 후보가 최근 검토되었습니다."},
        {"id": "contact-b", "agency": "한빛시청", "department": "디지털정보과", "task": "공공데이터 운영", "person": "김하늘", "phone": "02-0000-1200", "email": "data@example.test", "fax": "-", "source": "https://hanbit.example.test/staff", "verified": "2026-09-29", "history": "최근 변경 없음"},
        {"id": "contact-c", "agency": "새봄환경공단", "department": "환경사업부", "task": "환경 민원", "person": "-", "phone": "02-0000-2100", "email": "green@example.test", "fax": "02-0000-2199", "source": "https://saebom.example.test/contact", "verified": "2026-09-27", "history": "출처 재확인 필요"},
        {"id": "contact-d", "agency": "누리문화재단", "department": "문화사업팀", "task": "대관 안내", "person": "이누리", "phone": "02-0000-3100", "email": "space@example.test", "fax": "-", "source": "https://nuri.example.test/organization", "verified": "2026-09-28", "history": "업무명이 갱신되었습니다."},
    ),
}

REVIEW_FIXTURE: Final = {
    "items": (
        {"id": "review-a", "agency": "한빛시청", "target": "민원서비스과", "field": "부서 전화", "current": "02-0000-1000", "discovered": "02-0000-1100", "detected": "오늘 13:42", "state": "검토 대기", "tone": "warning"},
        {"id": "review-b", "agency": "한빛시청", "target": "공공데이터 운영", "field": "담당자", "current": "-", "discovered": "김하늘", "detected": "오늘 12:18", "state": "신규", "tone": "info"},
        {"id": "review-c", "agency": "새봄환경공단", "target": "환경사업부", "field": "이메일", "current": "contact@example.test", "discovered": "green@example.test", "detected": "오늘 11:06", "state": "확인 필요", "tone": "info"},
        {"id": "review-d", "agency": "누리문화재단", "target": "문화사업팀", "field": "팩스", "current": "02-0000-3000", "discovered": "미발견", "detected": "어제 15:07", "state": "보류", "tone": "neutral"},
    ),
    "details": (
        {"id": "review-a", "title": "한빛시청 · 민원서비스과", "current": "02-0000-1000", "discovered": "02-0000-1100", "source": "https://hanbit.example.test/service", "detected": "오늘 13:42", "evidence": "공식 부서 안내의 대표 전화 영역에서 새 값을 발견했습니다."},
        {"id": "review-b", "title": "한빛시청 · 공공데이터 운영", "current": "등록값 없음", "discovered": "김하늘", "source": "https://hanbit.example.test/staff", "detected": "오늘 12:18", "evidence": "담당 업무 행과 이름이 같은 표 안에서 확인되었습니다."},
        {"id": "review-c", "title": "새봄환경공단 · 환경사업부", "current": "contact@example.test", "discovered": "green@example.test", "source": "https://saebom.example.test/contact", "detected": "오늘 11:06", "evidence": "접근 오류 이전 마지막 저장본에서 다른 이메일을 발견했습니다."},
        {"id": "review-d", "title": "누리문화재단 · 문화사업팀", "current": "02-0000-3000", "discovered": "미발견", "source": "https://nuri.example.test/organization", "detected": "어제 15:07", "evidence": "현재 페이지에서 기존 팩스 값을 찾지 못했습니다. 삭제 근거로 확정되지 않았습니다."},
    ),
}

SETTINGS_FIXTURE: Final = {
    "storage_path": "C:\\PublicDB2\\data",
    "raw_path": "C:\\PublicDB2\\data\\raw",
    "default_timeout": "15초",
    "default_interval": "1초",
    "user_agent_policy": "PublicDB2 식별 정보 사용",
    "robots_policy": "robots.txt 및 기관 정책 준수",
}

WORKSPACE_FIXTURES: Final = {
    "agencies": AGENCIES_FIXTURE,
    "sources": SOURCES_FIXTURE,
    "runs": RUNS_FIXTURE,
    "contacts": CONTACTS_FIXTURE,
    "review": REVIEW_FIXTURE,
    "settings": SETTINGS_FIXTURE,
}
