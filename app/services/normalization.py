"""Conservative identity normalization inherited from verified legacy behavior."""

from __future__ import annotations

import ipaddress
from urllib.parse import SplitResult, urlsplit, urlunsplit


class SourceURLValidationError(ValueError):
    """The URL is not an allowed public HTTP(S) identity."""


def collapse_whitespace(value: str) -> str:
    return " ".join(value.strip().split())


def normalize_agency_name(value: str) -> str:
    return collapse_whitespace(value)


def normalize_org_unit_name(value: str) -> str:
    return collapse_whitespace(value)


def validate_public_hostname(hostname: str) -> None:
    normalized_host = hostname.rstrip(".").lower()
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise SourceURLValidationError("localhost source URLs are not allowed")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        return
    if address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified or address.is_reserved:
        raise SourceURLValidationError("local, private, link-local, unspecified, and reserved IP addresses are not allowed")


def _normalized_netloc(parts: SplitResult, hostname: str) -> str:
    try:
        port = parts.port
    except ValueError as error:
        raise SourceURLValidationError("source URL has an invalid port") from error
    if (parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443):
        port = None
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    return f"{host_text}:{port}" if port is not None else host_text


def normalize_source_url(url: str) -> str:
    raw_url = url.strip()
    if not raw_url:
        raise SourceURLValidationError("source URL is required")
    try:
        parts = urlsplit(raw_url)
    except ValueError as error:
        raise SourceURLValidationError("source URL is malformed") from error
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SourceURLValidationError("only http and https source URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise SourceURLValidationError("credentials in source URLs are not allowed")
    if not parts.hostname:
        raise SourceURLValidationError("source URL must include a hostname")
    hostname = parts.hostname.rstrip(".").lower()
    validate_public_hostname(hostname)
    normalized = urlunsplit((scheme, _normalized_netloc(parts._replace(scheme=scheme), hostname), parts.path, parts.query, ""))
    if len(raw_url) > 2048 or len(normalized) > 2048:
        raise SourceURLValidationError("source URL exceeds the supported length")
    return normalized
