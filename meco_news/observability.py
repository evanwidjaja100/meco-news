"""Structured, redacted operational events and health helpers."""

from __future__ import annotations

from datetime import datetime, UTC
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from typing import Any
from collections.abc import Mapping
from urllib.parse import urlsplit

from . import __version__


LOGGER = logging.getLogger("meco_news.events")
_TOKEN_RE = re.compile(r"(?i)(bot\d{5,}:[a-z0-9_-]{20,})")
_AUTH_RE = re.compile(r"(?i)(authorization|cookie|token|password|secret)\s*[:=]\s*[^\s,;]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]")


def redact(value: Any, *, limit: int = 2_000) -> Any:
    if isinstance(value, Mapping):
        return {str(key): redact(item, limit=limit) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [redact(item, limit=limit) for item in list(value)[:100]]
    if not isinstance(value, str):
        return value
    text = _CONTROL_RE.sub(" ", value)
    text = _TOKEN_RE.sub("<redacted-token>", text)
    text = _AUTH_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    if text.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(text)
            text = f"{parsed.scheme}://{parsed.hostname or ''}{':' + str(parsed.port) if parsed.port else ''}{parsed.path[:512]}"
        except ValueError:
            text = "<redacted-url>"
    return " ".join(text.split())[:limit]


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
    stdout_handler = logging.StreamHandler()
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
