"""Bounded single-page HTTP fetching with redirect-by-redirect SSRF checks."""

from __future__ import annotations

import ipaddress
import socket
import ssl
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
    error_code = "HTTP_FETCH_ERROR"

    def __init__(self, message: str, *, status_code: int | None = None, final_url: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.final_url = final_url


class HTTPStatusFailure(HTTPFetchError):
    error_code = "HTTP_STATUS_ERROR"


class ResponseTooLarge(HTTPFetchError):
    error_code = "RESPONSE_TOO_LARGE"


class UnsafeRequestTarget(HTTPFetchError):
    error_code = "UNSAFE_REQUEST_TARGET"


class RequestTimeout(HTTPFetchError):
    error_code = "HTTP_TIMEOUT"


class DNSResolutionFailure(HTTPFetchError):
    error_code = "DNS_FAILURE"


class TLSConnectionFailure(HTTPFetchError):
    error_code = "TLS_FAILURE"


class RemoteConnectionFailure(HTTPFetchError):
    error_code = "CONNECTION_FAILURE"


class RemoteDisconnected(HTTPFetchError):
    error_code = "REMOTE_DISCONNECTED"


class RedirectFailure(HTTPFetchError):
    error_code = "REDIRECT_ERROR"


def safe_http_error_summary(error: HTTPFetchError) -> str:
    """Return the bounded, user-facing category for an HTTP failure."""
    if isinstance(error, HTTPStatusFailure):
        return f"HTTP {error.status_code}" if error.status_code is not None else "HTTP 상태 오류"
    labels = (
        (RequestTimeout, "연결 시간 초과"),
        (DNSResolutionFailure, "DNS 확인 실패"),
        (TLSConnectionFailure, "TLS 연결 실패"),
        (RemoteDisconnected, "원격 서버가 연결을 종료함"),
        (RemoteConnectionFailure, "원격 서버 연결 실패"),
        (RedirectFailure, "리디렉션 오류"),
        (UnsafeRequestTarget, "안전하지 않은 주소 차단"),
        (ResponseTooLarge, "응답 크기 제한 초과"),
    )
    for error_type, label in labels:
        if isinstance(error, error_type):
            return label
    return "기타 HTTP 수집 실패"


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
            raise DNSResolutionFailure("DNS 확인 실패", final_url=url) from error
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

    def fetch(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
        target_validator: Callable[[str], None] | None = None,
    ) -> FetchResult:
        target = url
        request_params = params
        request_headers = {"User-Agent": self._user_agent, **(headers or {})}
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                headers=request_headers,
            ) as client:
                for redirect_count in range(self._max_redirects + 1):
                    validate_request_target(target, self._resolver)
                    if target_validator is not None:
                        target_validator(target)
                    with client.stream("GET", target, params=request_params) as response:
                        request_params = None
                        final_url = str(response.url)
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise RedirectFailure("리디렉션 오류", status_code=response.status_code, final_url=final_url)
                            if redirect_count >= self._max_redirects:
                                raise RedirectFailure("리디렉션 오류", status_code=response.status_code, final_url=final_url)
                            target = urljoin(final_url, location)
                            validate_request_target(target, self._resolver)
                            if target_validator is not None:
                                target_validator(target)
                            continue
                        if not 200 <= response.status_code < 300:
                            raise HTTPStatusFailure(
                                f"HTTP {response.status_code}",
                                status_code=response.status_code,
                                final_url=final_url,
                            )
                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > self._max_response_bytes:
                                    raise ResponseTooLarge(
                                        "응답 크기 제한 초과",
                                        status_code=response.status_code,
                                        final_url=final_url,
                                    )
                            except ValueError:
                                pass
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            if len(body) + len(chunk) > self._max_response_bytes:
                                raise ResponseTooLarge(
                                    "응답 크기 제한 초과",
                                    status_code=response.status_code,
                                    final_url=final_url,
                                )
                            body.extend(chunk)
                        content_type, charset = _content_metadata(response.headers.get("content-type", ""))
                        return FetchResult(bytes(body), response.status_code, final_url, content_type, charset)
        except HTTPFetchError:
            raise
        except httpx.TimeoutException as error:
            raise RequestTimeout("연결 시간 초과", final_url=target) from error
        except httpx.RemoteProtocolError as error:
            raise RemoteDisconnected("원격 서버가 연결을 종료함", final_url=target) from error
        except httpx.ConnectError as error:
            causes = []
            cause: BaseException | None = error
            while cause is not None and cause not in causes:
                causes.append(cause)
                cause = cause.__cause__ or cause.__context__
            if any(isinstance(value, socket.gaierror) for value in causes):
                raise DNSResolutionFailure("DNS 확인 실패", final_url=target) from error
            if any(isinstance(value, ssl.SSLError) for value in causes) or "SSL" in str(error).upper():
                raise TLSConnectionFailure("TLS 연결 실패", final_url=target) from error
            raise RemoteConnectionFailure("원격 서버 연결 실패", final_url=target) from error
        except httpx.HTTPError as error:
            raise HTTPFetchError("기타 HTTP 수집 실패", final_url=target) from error
        raise HTTPFetchError("기타 HTTP 수집 실패", final_url=target)
