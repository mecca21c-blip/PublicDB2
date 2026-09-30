"""Bounded single-page HTTP fetching with redirect-by-redirect SSRF checks."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import (
    HTTP_TIMEOUT_SECONDS,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    PUBLICDB_USER_AGENT,
)


class HTTPFetchError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, final_url: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.final_url = final_url


class HTTPStatusFailure(HTTPFetchError):
    pass


class ResponseTooLarge(HTTPFetchError):
    pass


class UnsafeRequestTarget(HTTPFetchError):
    pass


@dataclass(frozen=True)
class FetchResult:
    content: bytes
    status_code: int
    final_url: str
    content_type: str
    declared_charset: str | None

    @property
    def response_bytes(self) -> int:
        return len(self.content)


def _content_metadata(header_value: str) -> tuple[str, str | None]:
    parts = [part.strip() for part in header_value.split(";")]
    content_type = parts[0].lower() if parts and parts[0] else ""
    charset = None
    for parameter in parts[1:]:
        name, separator, value = parameter.partition("=")
        if separator and name.strip().lower() == "charset":
            charset = value.strip().strip('"').strip("'") or None
            break
    return content_type, charset


def _system_resolver(host: str) -> Iterable[str]:
    return {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def validate_request_target(url: str, resolver: Callable[[str], Iterable[str]] = _system_resolver) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeRequestTarget("HTTP/HTTPS URL만 수집할 수 있습니다.", final_url=url)
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeRequestTarget("인증 정보가 포함된 URL은 수집할 수 없습니다.", final_url=url)
    host = parsed.hostname
    if not host or host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise UnsafeRequestTarget("로컬 또는 유효하지 않은 대상은 수집할 수 없습니다.", final_url=url)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            addresses = list(resolver(host))
        except OSError as error:
            raise HTTPFetchError("대상 호스트의 주소를 확인하지 못했습니다.", final_url=url) from error
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeRequestTarget("공개 인터넷 주소가 아닌 대상은 수집할 수 없습니다.", final_url=url)


class HTTPFetcher:
    """Fetch exactly one registered resource; redirects are bounded and revalidated."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        user_agent: str = PUBLICDB_USER_AGENT,
        resolver: Callable[[str], Iterable[str]] = _system_resolver,
    ) -> None:
        if max_response_bytes <= 0 or max_redirects < 0:
            raise ValueError("HTTP limits must be non-negative and response size positive")
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_redirects = max_redirects
        self._user_agent = user_agent
        self._resolver = resolver

    @property
    def max_response_bytes(self) -> int:
        return self._max_response_bytes

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def user_agent(self) -> str:
        return self._user_agent

    def fetch(self, url: str) -> FetchResult:
        target = url
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                headers={"User-Agent": self._user_agent},
            ) as client:
                for redirect_count in range(self._max_redirects + 1):
                    validate_request_target(target, self._resolver)
                    with client.stream("GET", target) as response:
                        final_url = str(response.url)
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise HTTPStatusFailure("리디렉션 위치가 없습니다.", status_code=response.status_code, final_url=final_url)
                            if redirect_count >= self._max_redirects:
                                raise HTTPFetchError("허용된 리디렉션 횟수를 초과했습니다.", status_code=response.status_code, final_url=final_url)
                            target = urljoin(final_url, location)
                            validate_request_target(target, self._resolver)
                            continue
                        if not 200 <= response.status_code < 300:
                            raise HTTPStatusFailure(
                                f"HTTP 응답 상태 {response.status_code}",
                                status_code=response.status_code,
                                final_url=final_url,
                            )
                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > self._max_response_bytes:
                                    raise ResponseTooLarge(
                                        f"응답이 최대 {self._max_response_bytes}바이트를 초과합니다.",
                                        status_code=response.status_code,
                                        final_url=final_url,
                                    )
                            except ValueError:
                                pass
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            if len(body) + len(chunk) > self._max_response_bytes:
                                raise ResponseTooLarge(
                                    f"응답이 최대 {self._max_response_bytes}바이트를 초과합니다.",
                                    status_code=response.status_code,
                                    final_url=final_url,
                                )
                            body.extend(chunk)
                        content_type, charset = _content_metadata(response.headers.get("content-type", ""))
                        return FetchResult(bytes(body), response.status_code, final_url, content_type, charset)
        except HTTPFetchError:
            raise
        except httpx.HTTPError as error:
            raise HTTPFetchError(f"HTTP 수집 실패: {error}", final_url=target) from error
        raise HTTPFetchError("HTTP 수집을 완료하지 못했습니다.", final_url=target)
