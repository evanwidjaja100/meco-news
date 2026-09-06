"""Structured, redacted operational events, attempt lifecycle, and health helpers.

Logging contract (C1.4):

- JSON event logs go to stdout only. configure_logging never attaches a
  stderr handler; stream-capture tests lock this in.
- Human-readable command output that is not a JSON report (dry-run previews,
  restore confirmations, config validation errors) goes to stderr and is
  documented at the call site, so stdout stays JSON-parseable.
- Every value crossing into logs, persisted error text, or status output is
  passed through redact(), which removes credentials and tokens, URL
  userinfo/query strings, control/bidi characters, and caps hostile fields.
- Command, collection, delivery, and chunk attempts carry run/attempt,
  delivery, generation, and chunk identity and emit exactly one terminal
  record through AttemptLifecycle.finalize; a second finalize raises
  RuntimeError so double-terminal bugs surface loudly instead of logging
  twice. Recovery attempts link back with recovery_of while keeping their
  own exactly-one terminal record.
- Stable error class/reason codes are persisted separately from the
  sanitized human display text (storage._sanitize_error reuses redact).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
from typing import Any
from collections.abc import Mapping
from urllib.parse import urlsplit

from . import __version__


LOGGER = logging.getLogger("meco_news.events")
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{6,}[A-Za-z0-9._~+/-=]*")
_AUTH_KEYS = r"authorization|auth|cookie|cookies|token|secret|password|passwd|api[_-]?key|private[_-]?key|client[_-]?secret|session"
_AUTH_RE = re.compile(rf"(?i)({_AUTH_KEYS})\s*[:=]\s*[^\s,;\"']+")
_QUOTED_DQ_RE = re.compile(rf"(?i)\"[^\n\"]{{0,64}}(?:{_AUTH_KEYS})[^\n\"]{{0,64}}\"\s*:\s*\"[^\n\"]{{0,500}}\"")
_QUOTED_SQ_RE = re.compile(rf"(?i)'[^'\n]{{0,64}}(?:{_AUTH_KEYS})[^'\n]{{0,64}}'\s*:\s*'[^'\n]{{0,500}}'")
_QUERY_RE = re.compile(rf"(?i)([?&#](?:{_AUTH_KEYS})=)[^&\s\"'<>]*")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\ufeff\u00ad]")
_BIDI_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_SENSITIVE_KEYS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "auth",
    "cookie",
    "cookies",
    "set-cookie",
    "session",
    "api_key",
    "apikey",
    "api-key",
    "access_key",
    "private_key",
    "private-key",
    "client_secret",
    "bearer",
    "credential",
)
_MAX_DEPTH = 8
_MAX_ITEMS = 100


def _scrub_url_match(match: re.Match[str]) -> str:
    raw = match.group(0)
    trail = ""
    while raw and raw[-1] in ".,;!?)]}":
        trail = raw[-1] + trail
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        rebuilt = f"{parsed.scheme}://{host}"
        if port:
            rebuilt += f":{port}"
        rebuilt += (parsed.path or "")[:512]
    except ValueError:
        rebuilt = "<redacted-url>"
    return rebuilt + trail


def _scrub_text(value: str) -> str:
    text = _CONTROL_RE.sub(" ", value)
    text = _BIDI_RE.sub(" ", text)
    text = _URL_RE.sub(_scrub_url_match, text)
    text = _TOKEN_RE.sub("<redacted-token>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _QUOTED_DQ_RE.sub('"<redacted>": "<redacted>"', text)
    text = _QUOTED_SQ_RE.sub("'<redacted>': '<redacted>'", text)
    text = _QUERY_RE.sub(r"\1<redacted>", text)
    text = _AUTH_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return text


def redact(value: Any, *, limit: int = 2_000, _key: str = "", _depth: int = 0) -> Any:
    if any(marker in _key.casefold() for marker in _SENSITIVE_KEYS):
        return "<redacted>"
    if _depth > _MAX_DEPTH:
        return "<truncated>"
    if isinstance(value, Mapping):
        entries = list(value.items())[:_MAX_ITEMS]
        cleaned: dict[str, Any] = {str(key): redact(item, limit=limit, _key=str(key), _depth=_depth + 1) for key, item in entries}
        if len(value) > _MAX_ITEMS:
            cleaned["<truncated>"] = f"{len(value) - _MAX_ITEMS} more fields omitted"
        return cleaned
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(item, limit=limit, _depth=_depth + 1) for item in list(value)[:_MAX_ITEMS]]
    if isinstance(value, bytes | bytearray | memoryview):
        value = bytes(value).decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return value
    return " ".join(_scrub_text(value).split())[:limit]


class JsonEventFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "event_fields", {})
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event_name", record.getMessage()),
            "version": __version__,
        }
        if isinstance(fields, Mapping):
            payload.update(redact(fields))
        if record.exc_info:
            payload["error_class"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            payload["stack"] = redact(self.formatException(record.exc_info), limit=4_000)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(*, level: str = "INFO", file_path: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    formatter = JsonEventFormatter()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)
    if file_path:
        path = os.path.abspath(file_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def emit_event(event: str, *, level: int = logging.INFO, logger: logging.Logger | None = None, **fields: Any) -> None:
    target = logger or LOGGER
    target.log(level, event, extra={"event_name": event, "event_fields": fields})


_ATTEMPT_KINDS = ("command", "collection", "delivery", "chunk")


@dataclass
class AttemptLifecycle:
    kind: str
    run_id: str
    attempt_id: str
    delivery_id: int | None = None
    chunk_id: int | None = None
    generation: int | None = None
    recovery_of: str | None = None
    finalized: bool = False

    def finalize(
        self,
        result: str,
        *,
        outcome: str,
        error_class: str = "",
        error_text: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        if self.kind not in _ATTEMPT_KINDS:
            raise ValueError(f"unsupported attempt kind: {self.kind!r}")
        if not outcome:
            raise ValueError("attempt outcome is required")
        if self.finalized:
            raise RuntimeError(f"attempt already finalized: {self.attempt_id}")
        self.finalized = True
        record: dict[str, Any] = {
            "kind": self.kind,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "delivery_id": self.delivery_id,
            "chunk_id": self.chunk_id,
            "generation": self.generation,
            "recovery_of": self.recovery_of,
            "result": result,
            "outcome": outcome,
            "error_class": error_class[:80],
            "error_text": redact(error_text, limit=1_000) if error_text else "",
        }
        record.update(extra)
        emit_event("attempt_terminal", **record)
        return record