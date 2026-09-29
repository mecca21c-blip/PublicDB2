"""Clearly isolated demo data for the UI foundation.

Nothing in this module is production data or a persistence contract. It is
removed when real query services are connected in a later goal.
"""

from __future__ import annotations

from typing import Final


def _chart_points(values: tuple[int, ...]) -> str:
    width = 520
    height = 150
    inset_x = 14
    inset_y = 16
    maximum = max(values, default=1)
    step = (width - (inset_x * 2)) / max(len(values) - 1, 1)
    return " ".join(
        (
            f"{inset_x + (index * step):.1f},"
            f"{height - inset_y - ((value / maximum) * (height - inset_y * 2)):.1f}"
        )
        for index, value in enumerate(values)
    )


SUCCESS_COUNTS: Final = (31, 42, 38, 52, 47, 58, 54)
ERROR_COUNTS: Final = (4, 3, 6, 2, 5, 3, 4)

DASHBOARD_FIXTURE: Final = {
    "is_demo": True,
    "as_of": "UI foundation preview",
    "kpis": (
        {"label": "등록 기관", "value": "248", "note": "운영 대상 기관", "icon": "building", "tone": "blue"},
        {"label": "수집 Source", "value": "892", "note": "등록된 공식 출처", "icon": "link", "tone": "cyan"},
        {"label": "연락처 DB", "value": "12,486", "note": "Master 확정 연락처", "icon": "contacts", "tone": "violet"},
        {"label": "검토 대기", "value": "312", "note": "사용자 확인 필요", "icon": "review", "tone": "amber"},
        {"label": "수집 오류", "value": "8", "note": "최근 실행 기준", "icon": "alert", "tone": "red"},
    ),
    "recent_runs": (
        {"time": "오늘 14:20", "agency": "샘플 기관 A", "source": "직원 안내 페이지", "status": "성공", "tone": "success"},
        {"time": "오늘 13:48", "agency": "샘플 기관 B", "source": "조직/부서 안내", "status": "성공", "tone": "success"},
        {"time": "오늘 12:36", "agency": "샘플 기관 C", "source": "업무 담당자 목록", "status": "자료없음", "tone": "info"},
        {"time": "오늘 11:54", "agency": "샘플 기관 D", "source": "대표 연락처", "status": "오류", "tone": "danger"},
        {"time": "오늘 10:12", "agency": "샘플 기관 E", "source": "공식 조직도", "status": "성공", "tone": "success"},
        {"time": "어제 17:40", "agency": "샘플 기관 F", "source": "부서별 업무 안내", "status": "미확인", "tone": "neutral"},
    ),
    "trend": {
        "labels": ("월", "화", "수", "목", "금", "토", "일"),
        "success_total": sum(SUCCESS_COUNTS),
        "error_total": sum(ERROR_COUNTS),
        "success_points": _chart_points(SUCCESS_COUNTS),
        "error_points": _chart_points(ERROR_COUNTS),
    },
    "pending_reviews": (
        {"agency": "샘플 기관 A", "field": "부서 전화", "before": "02-0000-1000", "after": "02-0000-1100", "detected": "오늘 13:42", "status": "검토 대기"},
        {"agency": "샘플 기관 B", "field": "업무명", "before": "민원 안내", "after": "통합 민원 안내", "detected": "오늘 12:18", "status": "검토 대기"},
        {"agency": "샘플 기관 C", "field": "이메일", "before": "contact@example.test", "after": "help@example.test", "detected": "오늘 11:06", "status": "확인 필요"},
        {"agency": "샘플 기관 D", "field": "부서명", "before": "정보팀", "after": "디지털정보팀", "detected": "어제 16:31", "status": "검토 대기"},
        {"agency": "샘플 기관 E", "field": "팩스", "before": "02-0000-2000", "after": "미발견", "detected": "어제 15:07", "status": "보류"},
    ),
    "quick_actions": (
        {"label": "기관 등록", "description": "기관/조직 화면으로 이동", "href": "/agencies?intent=create", "icon": "building"},
        {"label": "Source 등록", "description": "수집 소스 화면으로 이동", "href": "/sources?intent=create", "icon": "link"},
        {"label": "수집 이력", "description": "최근 실행 결과 확인", "href": "/runs", "icon": "clock"},
        {"label": "검토 대상", "description": "변경 후보 확인", "href": "/review", "icon": "review"},
    ),
}
