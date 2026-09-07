"""Bounded HTTP fetching with explicit redirect and SSRF policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from http.client import HTTPConnection, HTTPSConnection, IncompleteRead, HTTPException
import contextlib
import ipaddress
import ssl
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from .config import CollectionLimits, NetworkPolicy
from .urls import (
    URLPolicyError,
    ValidatedURL,
    same_or_allowed_redirect,
    validate_resolved_addresses,
    validate_url,
)


REDIRECT_CODES = {301, 302, 303, 307, 308}


class NetworkError(OSError):
    """A bounded fetch failed with a stable reason code."""

    def __init__(self, reason_code: str, message: str = "network request failed", *, retryable: bool = True) -> None:
        self.reason_code = reason_code
        self.retryable = retryable
        super().__init__(message)


class ResponseTooLarge(NetworkError):
    def __init__(self) -> None:
        super().__init__("response_too_large", "remote response exceeded the configured byte limit")


@dataclass(frozen=True, slots=True)
class FetchResponse:
    url: ValidatedURL
    payload: bytes
    status: int
    content_type: str


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class _PinnedHTTPConnection(HTTPConnection):
    """HTTP connection whose socket target is an already validated address."""

    def __init__(self, hostname: str, port: int, address: str, timeout: float) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        address = ipaddress.ip_address(self._pinned_address)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            destination: tuple[Any, ...] = (self._pinned_address, self.port, 0, 0) if family == socket.AF_INET6 else (self._pinned_address, self.port)
            sock.connect(destination)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class _PinnedHTTPSConnection(HTTPSConnection):
    """TLS connection that pins TCP routing while retaining hostname SNI."""

    def __init__(self, hostname: str, port: int, address: str, timeout: float) -> None:
        self._tls_context = ssl.create_default_context()
        self._tls_server_hostname = hostname
        super().__init__(hostname, port, timeout=timeout, context=self._tls_context)
        self._pinned_address = address

    def connect(self) -> None:
        address = ipaddress.ip_address(self._pinned_address)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            destination: tuple[Any, ...] = (self._pinned_address, self.port, 0, 0) if family == socket.AF_INET6 else (self._pinned_address, self.port)
            sock.connect(destination)
            self.sock = self._tls_context.wrap_socket(sock, server_hostname=self._tls_server_hostname)
        except BaseException:
            sock.close()
            raise


class BoundedHTTPClient:
    """HTTP client with a hard byte ceiling and a monotonic deadline."""

    def __init__(
        self,
        limits: CollectionLimits | None = None,
        network_policy: NetworkPolicy | None = None,
        *,
        user_agent: str = "MecoMarketWatch/2.0 (+https://www.meco.co.id)",
        allow_private_for_tests: bool = False,
    ) -> None:
        self.limits = limits or CollectionLimits()
        self.network_policy = network_policy or NetworkPolicy()
        self.user_agent = user_agent
        self.allow_private_for_tests = allow_private_for_tests
        # Sources never inherit ambient HTTP(S)_PROXY settings.  The network
        # policy has no implicit proxy mode; a future proxy must be an
        # explicitly reviewed, separately pinned path.
        from urllib.request import ProxyHandler

        self._opener = build_opener(_NoRedirectHandler(), ProxyHandler({}))

    def _validated(self, value: object) -> ValidatedURL:
        return validate_url(
            value,
            max_length=self.limits.url_chars,
            allow_http=not self.network_policy.require_https,
            allow_private=self.allow_private_for_tests,
        )

    def _validate_origin(self, target: ValidatedURL) -> list[str]:
        return validate_resolved_addresses(
            target.hostname,
            target.port,
            allow_private=self.allow_private_for_tests,
        )

    def fetch(self, url: str, *, source_id: str = "") -> FetchResponse:
        current = self._validated(url)
        started = time.monotonic()
        deadline = started + self.limits.source_deadline_seconds
        for hop in range(self.limits.max_redirects + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkError("source_deadline_exceeded", "source deadline exceeded")
            try:
                addresses = self._validate_origin(current)
                response = self._open_pinned(current, addresses[0], remaining)
                try:
                    status = int(response.status)
                    if status in REDIRECT_CODES:
                        location = response.getheader("Location", "") or ""
                        response.close()
                        if hop >= self.limits.max_redirects:
                            raise NetworkError("redirect_limit", "redirect limit exceeded")
                        current = self._redirect_target(current, location)
                        continue
                    if status == 429:
                        raise NetworkError("http_429", "source returned HTTP 429")
                    if 500 <= status <= 599:
                        raise NetworkError("http_5xx", f"source returned HTTP {status}")
                    if status >= 400:
                        raise NetworkError("http_error", f"source returned HTTP {status}", retryable=False)
                    payload = self._read_bounded(response, deadline)
                    return FetchResponse(
                        url=current,
                        payload=payload,
                        status=status,
                        content_type=response.getheader("Content-Type", "") or "",
                    )
                finally:
                    response.close()
            except HTTPError as exc:
                try:
                    if exc.code in REDIRECT_CODES:
                        if hop >= self.limits.max_redirects:
                            raise NetworkError("redirect_limit", "redirect limit exceeded") from exc
                        location = exc.headers.get("Location", "") if exc.headers else ""
                        current = self._redirect_target(current, location)
                        continue
                    if exc.code == 429:
                        raise NetworkError("http_429", "source returned HTTP 429") from exc
                    if 500 <= exc.code <= 599:
                        raise NetworkError("http_5xx", f"source returned HTTP {exc.code}") from exc
                    raise NetworkError("http_error", f"source returned HTTP {exc.code}", retryable=False) from exc
                finally:
                    # HTTPError owns the response body returned by urllib.  A
                    # classified error is still a response and must not leak
                    # its socket/file object while the caller handles the
                    # stable NetworkError.
                    with contextlib.suppress(Exception):
                        exc.close()
            except URLPolicyError as exc:
                raise NetworkError(exc.reason_code, "URL policy rejected the request", retryable=False) from exc
            except ResponseTooLarge:
                raise
            except NetworkError:
                raise
            except (TimeoutError, ConnectionError, IncompleteRead, HTTPException, URLError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise NetworkError("source_deadline_exceeded", "source deadline exceeded") from exc
                raise NetworkError("network_error", type(exc).__name__) from exc
        raise NetworkError("redirect_limit", "redirect limit exceeded")

    def _open_pinned(self, target: ValidatedURL, address: str, remaining: float) -> Any:
        parts = urlsplit(target.normalized_url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        timeout = min(float(self.limits.socket_timeout_seconds), max(0.1, remaining))
        connection_cls = _PinnedHTTPSConnection if target.scheme == "https" else _PinnedHTTPConnection
        connection = connection_cls(target.hostname, target.port, address, timeout)
        try:
            host_header = target.hostname
            default_port = (target.scheme == "https" and target.port == 443) or (target.scheme == "http" and target.port == 80)
            if not default_port:
                host_header = f"{host_header}:{target.port}"
            connection.request(
                "GET",
                path,
                headers={
                    "Host": host_header,
                    "User-Agent": self.user_agent,
                    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json, */*",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            return connection.getresponse()
        except BaseException:
            connection.close()
            raise

    def _redirect_target(self, current: ValidatedURL, location: str) -> ValidatedURL:
        if not isinstance(location, str) or not location or len(location) > self.limits.url_chars:
            raise NetworkError("redirect_disallowed", "redirect location is invalid", retryable=False)
        try:
            target = self._validated(urljoin(current.normalized_url, location))
        except (URLPolicyError, ValueError) as exc:
            raise NetworkError("redirect_disallowed", "redirect target is invalid", retryable=False) from exc
        allowed_hosts = self.network_policy.allowed_redirect_hosts
        if self.network_policy.same_host_redirects_only and target.hostname != current.hostname:
            raise NetworkError("redirect_disallowed", "cross-host redirect is not allowed", retryable=False)
        if not same_or_allowed_redirect(current, target, allowed_hosts=allowed_hosts):
            raise NetworkError("redirect_disallowed", "redirect origin is not allowed", retryable=False)
        return target

    def _read_bounded(self, response: Any, deadline: float) -> bytes:
        content_length = response.headers.get("Content-Length", "")
        if content_length:
            try:
                declared = int(content_length)
            except (TypeError, ValueError) as exc:
                raise NetworkError("invalid_content_length", "invalid Content-Length", retryable=False) from exc
            if declared < 0 or declared > self.limits.response_bytes:
                raise ResponseTooLarge()
        chunks: list[bytes] = []
        total = 0
        while True:
            if time.monotonic() >= deadline:
                raise NetworkError("source_deadline_exceeded", "source deadline exceeded")
            try:
                chunk = response.read(min(64 * 1024, self.limits.response_bytes - total + 1))
            except IncompleteRead as exc:
                # IncompleteRead is a source-local failure, not permission to
                # parse the partial body because it may change the feed shape.
                raise NetworkError("incomplete_read", "incomplete source response") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > self.limits.response_bytes:
                raise ResponseTooLarge()
            chunks.append(chunk)
        return b"".join(chunks)


def fetch_bytes(
    url: str,
    timeout: int = 25,
    *,
    limits: CollectionLimits | None = None,
    network_policy: NetworkPolicy | None = None,
    allow_private_for_tests: bool = False,
) -> bytes:
    """Compatibility helper used by collectors and local tests."""

    base = limits or CollectionLimits()
    base = replace(
        base,
        source_deadline_seconds=min(base.source_deadline_seconds, max(1, timeout)),
        socket_timeout_seconds=min(base.socket_timeout_seconds, max(1, timeout)),
    )
    return BoundedHTTPClient(base, network_policy, allow_private_for_tests=allow_private_for_tests).fetch(url).payload
