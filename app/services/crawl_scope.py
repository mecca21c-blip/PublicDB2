"""Shared deterministic path-prefix rules for bounded web crawling."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit


MAX_CRAWL_PATH_RULES = 100


def normalize_path_prefix(value: object) -> str:
    path = str(value or "").strip()
    parts = urlsplit(path)
    if not path.startswith("/") or parts.scheme or parts.netloc or parts.query or parts.fragment:
        raise ValueError("경로 규칙은 /로 시작하고 query나 fragment를 포함하지 않아야 합니다.")
    if ".." in path.split("/"):
        raise ValueError("경로 규칙에 ..을 사용할 수 없습니다.")
    return path.rstrip("/") or "/"


def normalize_path_prefixes(values: object, *, fallback: Iterable[str] = ()) -> list[str]:
    if values is None:
        candidates = list(fallback)
    elif isinstance(values, str):
        candidates = values.splitlines()
    else:
        candidates = list(values)
    normalized: list[str] = []
    for candidate in candidates:
        if not str(candidate or "").strip():
            continue
        prefix = normalize_path_prefix(candidate)
        if prefix not in normalized:
            normalized.append(prefix)
    if len(normalized) > MAX_CRAWL_PATH_RULES:
        raise ValueError(f"경로 규칙은 각각 최대 {MAX_CRAWL_PATH_RULES}개까지 등록할 수 있습니다.")
    return normalized


def path_matches_prefix(path: str, prefix: str) -> bool:
    target = path or "/"
    rule = prefix.rstrip("/") or "/"
    return rule == "/" or target == rule or target.startswith(rule + "/")


def crawl_path_allowed(path: str, allowed_paths: Iterable[str], excluded_paths: Iterable[str]) -> bool:
    allowed = list(allowed_paths) or ["/"]
    return (
        any(path_matches_prefix(path, prefix) for prefix in allowed)
        and not any(path_matches_prefix(path, prefix) for prefix in excluded_paths)
    )
