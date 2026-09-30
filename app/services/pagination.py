"""Shared bounded workspace pagination contract."""

from __future__ import annotations

import math


DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


def page_values(page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> tuple[int, int, int]:
    safe_page = max(int(page or 1), 1)
    safe_size = min(max(int(page_size or DEFAULT_PAGE_SIZE), 1), MAX_PAGE_SIZE)
    return safe_page, safe_size, (safe_page - 1) * safe_size


def page_metadata(total: int, page: int, page_size: int) -> dict[str, int | bool]:
    pages = max(1, math.ceil(total / page_size))
    current = min(page, pages)
    return {
        'page': current,
        'page_size': page_size,
        'total': total,
        'pages': pages,
        'has_previous': current > 1,
        'has_next': current < pages,
    }
