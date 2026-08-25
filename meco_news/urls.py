"""URL validation and network-address policy.

All URLs entering the application pass through this module.  The module is
deliberately small and dependency-free so that the same policy is used by
collectors, ranking, and Telegram rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
import socket
import unicodedata
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_MAX_URL_LENGTH = 2048
TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\x80-\x9f\u2028\u2029]")


class URLPolicyError(ValueError):
    """A URL failed a fail-closed validation rule."""

    def __init__(self, reason_code: str, message: str = "invalid URL") -> None:
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    normalized_url: str
    scheme: str
    hostname: str
    display_hostname: str
    port: int
    explicit_port: bool


def _ascii_hostname(hostname: str) -> str:
    try:
        normalized = unicodedata.normalize("NFKC", hostname).strip().rstrip(".")
        if not normalized:
            raise URLPolicyError("missing_host")
        # urlsplit rejects some NFKC-dangerous delimiters, but doing the
        # normalization explicitly keeps behavior deterministic across Python
        # versions and platforms.
        if _CONTROL_RE.search(normalized):
            raise URLPolicyError("url_control_character")
        return normalized.encode("idna").decode("ascii").casefold()
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, URLPolicyError):
            raise
        raise URLPolicyError("invalid_hostname") from exc


def _validate_port(parts: SplitResult) -> tuple[int, bool]:
    try:
        port = parts.port
    except ValueError as exc:
        raise URLPolicyError("invalid_port") from exc
    if port is None:
        return (443 if parts.scheme.casefold() == "https" else 80), False
    if not 1 <= port <= 65535:
        raise URLPolicyError("invalid_port")
    return port, True


def _is_forbidden_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True
    # is_global is intentionally supplemented: Python's classification can
    # change for newly assigned special ranges, while these classes must never
    # be reachable by a production fetcher.
    # ponytail: explicitly block multicast/unspecified/reserved/mapped — C4.2 requires every non-global class fail-closed
    return (
        not parsed.is_global
        or parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_unspecified
        or parsed.is_reserved
        or getattr(parsed, "is_global", True) is False
    )


def validate_url(
    value: object,
    *,
    max_length: int = DEFAULT_MAX_URL_LENGTH,
    allow_http: bool = False,
    allow_private: bool = False,
) -> ValidatedURL:
    """Validate and canonicalize an HTTP(S) URL.

    ``allow_private`` exists only for local fake-server tests and explicitly
    controlled development environments.  Production configuration leaves it
    false; callers should not expose it as an operator-controlled live flag.
    """

    if not isinstance(value, str):
        raise URLPolicyError("url_not_string")
    if not value or len(value) > max_length:
        raise URLPolicyError("url_too_long" if value else "empty_url")
    if _CONTROL_RE.search(value):
        raise URLPolicyError("url_control_character")
    if value.strip() != value:
        raise URLPolicyError("url_whitespace")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise URLPolicyError("invalid_url") from exc
    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"} or (scheme == "http" and not allow_http):
        raise URLPolicyError("scheme_disallowed")
    if parts.username is not None or parts.password is not None:
        raise URLPolicyError("userinfo_disallowed")
    try:
        raw_hostname = parts.hostname
    except ValueError as exc:
        raise URLPolicyError("invalid_hostname") from exc
    if not parts.netloc or not raw_hostname:
        raise URLPolicyError("missing_host")
    hostname = _ascii_hostname(raw_hostname)
    port, explicit_port = _validate_port(parts)
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None and _is_forbidden_address(hostname) and not allow_private:
        raise URLPolicyError("ssrf_address_class")
    path = "/" if not parts.path else parts.path
    # Query parameter order is not article identity.  Tracking parameters and
    # fragments are not identity either.  Keep blank values because a
    # publisher may use them semantically.
    query_pairs = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in TRACKING_PARAMETERS
    ]
    query_pairs.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    netloc = hostname
    if ":" in hostname and not hostname.startswith("["):
        netloc = f"[{hostname}]"
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    if explicit_port and not default_port:
        netloc = f"{netloc}:{port}"
    normalized = urlunsplit((scheme, netloc, path.rstrip("/") or "/", urlencode(query_pairs), ""))
    return ValidatedURL(normalized, scheme, hostname, raw_hostname, port, explicit_port)


def validate_resolved_addresses(
    hostname: str,
    port: int,
    *,
    allow_private: bool = False,
) -> list[str]:
    """Resolve a host and reject any non-public answer.

    Rejecting mixed public/private DNS answers is intentional.  It avoids
    allowing a resolver to return one safe address followed by a metadata or
    loopback address.  Deployment egress policy remains a required second
    boundary against DNS rebinding.
    """

    try:
        addresses = sorted({str(result[4][0]) for result in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM) if result[4]})
    except (OSError, socket.gaierror) as exc:
        raise URLPolicyError("dns_resolution_failed") from exc
    if not addresses:
        raise URLPolicyError("dns_no_addresses")
    if not allow_private and any(_is_forbidden_address(address) for address in addresses):
        raise URLPolicyError("ssrf_address_class")
    return addresses


def same_or_allowed_redirect(
    current: ValidatedURL,
    target: ValidatedURL,
    *,
    allowed_hosts: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Return whether a redirect stays on the permitted origin policy."""

    if target.scheme != current.scheme:
        return False
    if target.hostname == current.hostname:
        return True
    return target.hostname in {host.casefold().rstrip(".") for host in allowed_hosts}


def sanitize_url_for_log(value: object) -> str:
    """Return a bounded host-only representation for operational logs."""

    try:
        validated = validate_url(value, allow_http=True, allow_private=True)
    except (URLPolicyError, TypeError):
        return "<invalid-url>"
    return f"{validated.scheme}://{validated.display_hostname}:{validated.port}"
