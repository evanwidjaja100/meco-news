"""Bounded RSS/Atom/search collection with per-source isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
import html
import json
import logging
import multiprocessing as mp
import os
import pickle
import re
import sys
from queue import Empty, Queue
import threading
import time
from typing import Any
from collections.abc import Callable, Iterator, Mapping
from urllib.parse import urlencode, urlsplit
import xml.etree.ElementTree as ET

from .config import AppConfig, CollectionLimits, NetworkPolicy
from .models import NewsItem
from .network import NetworkError, fetch_bytes
from .observability import configure_logging
from .urls import URLPolicyError, validate_url
import contextlib


# C4.1: C0/C1 controls plus Unicode bidi overrides/isolates and LRM/RLM marks.
# U+061C (Arabic letter mark) is deliberately kept: it is legitimate text,
# not a spoofing control. Valid emoji/astral and combining marks are untouched.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")
LOGGER = logging.getLogger(__name__)
MAX_CONCURRENT_REQUESTS_PER_HOST = 2
DEFAULT_IPC_FRAME_BYTES = 4 * 1024 * 1024


class SourceDataError(ValueError):
    """A source payload could not be safely interpreted."""

    def __init__(self, reason_code: str, message: str = "invalid source data") -> None:
        self.reason_code = reason_code
        super().__init__(message)


def _failed_source_result(source_id: str, source_name: str, started: float, exc: BaseException) -> SourceResult:
    reason = str(getattr(exc, "reason_code", "source_exception"))[:80] or "source_exception"
    error_class = type(exc).__name__[:80]
    fields = {
        "source_id": source_id,
        "source_name": source_name,
        "reason_code": reason,
        "error_class": error_class,
    }
    if isinstance(exc, SourceDataError | NetworkError):
        LOGGER.warning("source failed", extra={"event_name": "source_failed", "event_fields": fields})
    else:
        LOGGER.exception("unexpected source-local failure", extra={"event_name": "source_unexpected_failure", "event_fields": fields})
    return SourceResult(
        source_id=source_id,
        source_name=source_name,
        outcome="failed",
        duration_ms=int((time.monotonic() - started) * 1000),
        reason_code=reason,
        error_class=error_class,
        error=f"{error_class}: {reason}",
    )


@dataclass(slots=True)
class SourceResult:
    source_id: str
    source_name: str
    outcome: str
    items: list[NewsItem] = field(default_factory=list)
    duration_ms: int = 0
    bytes_read: int = 0
    accepted_count: int = 0
    quarantined_count: int = 0
    reason_code: str = ""
    error_class: str = ""
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"


@dataclass(slots=True)
class CollectionResult:
    items: list[NewsItem]
    source_results: list[SourceResult]
    started_at: datetime
    duration_ms: int

    @property
    def issues(self) -> list[str]:
        return [
            f"{result.source_name}: {result.reason_code or result.error_class or 'source_failed'}"
            for result in self.source_results
            if not result.succeeded
        ]

    @property
    def successful_sources(self) -> int:
        return sum(result.succeeded for result in self.source_results)

    @property
    def failed_sources(self) -> int:
        return sum(not result.succeeded for result in self.source_results)

    @property
    def all_sources_failed(self) -> bool:
        return bool(self.source_results) and self.successful_sources == 0

    def __iter__(self) -> Iterator[Any]:
        """Compatibility with the original ``items, issues = collect_all`` API."""
        yield self.items
        yield self.issues


def _limits(config: Mapping[str, Any] | None) -> CollectionLimits:
    if config is not None and isinstance(config, AppConfig):
        return config.limits
    if isinstance(config, Mapping):
        raw = config.get("limits")
        if isinstance(raw, Mapping):
            fields = {field.name for field in CollectionLimits.__dataclass_fields__.values()}
            values = {name: raw[name] for name in fields if name in raw}
            try:
                return CollectionLimits(**values)
            except (TypeError, ValueError):
                return CollectionLimits()
    return CollectionLimits()


def _network_policy(config: Mapping[str, Any] | None) -> NetworkPolicy:
    if config is not None and isinstance(config, AppConfig):
        return config.network_policy
    if isinstance(config, Mapping):
        raw = config.get("network_policy")
        if isinstance(raw, Mapping):
            try:
                return NetworkPolicy(
                    allowed_redirect_hosts=frozenset(raw.get("allowed_redirect_hosts", ())),
                    same_host_redirects_only=bool(raw.get("same_host_redirects_only", True)),
                    require_https=bool(raw.get("require_https", True)),
                )
            except (TypeError, ValueError):
                return NetworkPolicy()
    return NetworkPolicy()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _is_invalid_scalar(text: str) -> bool:
    # ponytail: C4.1 — lone surrogates and invalid scalars must be quarantined before hashing/rendering
    for ch in text:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            return True
        if ch == "\ufffd":
            # Replacement char indicates prior invalid bytes
            return True
    return False


def _bounded_text(value: object, maximum: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    # Quarantine invalid scalars before any processing
    if _is_invalid_scalar(value):
        return "", True
    value = html.unescape(value)
    value = re.sub(r"<[^>]{0,4096}>", " ", value)
    value = _CONTROL_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
    # Check again after unescape (may introduce surrogates via entities)
    if _is_invalid_scalar(value):
        return "", True
    if len(value) <= maximum:
        return value, False
    return value[: max(0, maximum - 1)].rstrip() + "...", True


def _clean_html(value: str) -> str:
    return _bounded_text(value, CollectionLimits().summary_chars)[0]


def _text(node: ET.Element, names: set[str], maximum: int = 16_384) -> str:
    for child in node.iter():
        if _local_name(child.tag) in names and child.text:
            return _bounded_text(child.text, maximum)[0]
    return ""


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _repair_xml(payload: bytes, limits: CollectionLimits) -> bytes:
    repaired = payload.decode("utf-8", errors="replace")
    repaired = _CONTROL_RE.sub("", repaired)
    repaired = re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|[A-Za-z][A-Za-z0-9]+;)", "&amp;", repaired)
    encoded = repaired.encode("utf-8")
    if len(encoded) > limits.response_bytes:
        raise SourceDataError("response_too_large")
    return encoded


def _entry_values(entry: ET.Element, limits: CollectionLimits) -> tuple[str, str, str, str, str, str]:
    title = _bounded_text(_text(entry, {"title"}, limits.title_chars * 2), limits.title_chars)[0]
    summary = _bounded_text(
        _text(entry, {"description", "summary", "content"}, limits.summary_chars * 2),
        limits.summary_chars,
    )[0]
    published = _text(entry, {"pubdate", "published", "updated", "date"}, 256)
    link = ""
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        rel = str(child.attrib.get("rel", "alternate")).casefold()
        candidate = child.attrib.get("href") or (child.text or "").strip()
        if candidate and rel in {"alternate", ""}:
            link = candidate
            break
    if not link:
        link = _text(entry, {"guid", "id"}, limits.url_chars)
    source = ""
    source_url = ""
    for child in entry:
        if _local_name(child.tag) == "source":
            source = _bounded_text(child.text or "", limits.source_chars)[0]
            source_url = child.attrib.get("url", "")
            break
    return title, link, source, source_url, summary, published


def _entry_has_invalid_scalar(entry: ET.Element) -> bool:
    for node in entry.iter():
        if node.text and _is_invalid_scalar(node.text):
            return True
        if node.tail and _is_invalid_scalar(node.tail):
            return True
        if any(_is_invalid_scalar(value) for value in node.attrib.values()):
            return True
    return False


def _parse_xml_once(
    payload: bytes,
    feed_name: str,
    collector: str,
    query_name: str,
    source_id: str,
    limits: CollectionLimits,
    quarantine: list[str],
) -> list[NewsItem]:
    if len(payload) > limits.response_bytes:
        raise SourceDataError("response_too_large")
    # This runs on the raw payload before C4.1 UTF-8 sanitization: sanitizing first can misalign
    # non-UTF-8 multibyte structure (e.g. a UTF-32 BOM) and hide a DTD from every decoder.
    # ponytail: encoding-independent DTD/entity rejection — C4.3 requires parser-level prohibition before expansion
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise SourceDataError("xml_dtd_disallowed")
    for enc in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
        try:
            text = payload.decode(enc).lower()
            if "<!doctype" in text or "<!entity" in text:
                raise SourceDataError("xml_dtd_disallowed")
        except SourceDataError:
            raise
        except (UnicodeDecodeError, ValueError):
            continue
    # C4.1: sanitize invalid UTF-8 (e.g., lone surrogate bytes) before parsing so one bad item doesn't abort the whole feed
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        payload = payload.decode("utf-8", errors="replace").encode("utf-8")
    parser = ET.XMLPullParser(events=("start", "end"))
    depth = 0
    nodes = 0
    entries_seen = 0
    root_name: str | None = None
    results: list[NewsItem] = []
    try:
        for start in range(0, len(payload), 64 * 1024):
            parser.feed(payload[start : start + 64 * 1024])
            for raw_event in parser.read_events():
                if len(raw_event) != 2:
                    continue
                event = raw_event[0]
                element = raw_event[1]
                if not isinstance(element, ET.Element):
                    continue
                if event == "start":
                    depth += 1
                    if depth == 1:
                        root_name = _local_name(element.tag)
                        if root_name not in {"rss", "feed"}:
                            raise SourceDataError("document_type_invalid")
                    nodes += 1
                    if depth > limits.xml_depth:
                        raise SourceDataError("xml_depth_limit")
                    if nodes > limits.xml_nodes:
                        raise SourceDataError("xml_node_limit")
                    continue
                if _local_name(element.tag) in {"item", "entry"}:
                    entries_seen += 1
                    if entries_seen > limits.entries_per_source:
                        raise SourceDataError("entry_limit")
                    if _entry_has_invalid_scalar(element):
                        quarantine.append("invalid_unicode_scalar")
                        element.clear()
                        depth -= 1
                        continue
                    title, link, source, source_url, summary, published_text = _entry_values(element, limits)
                    if not title:
                        quarantine.append("missing_title")
                    else:
                        try:
                            validated = validate_url(link, max_length=limits.url_chars, allow_http=True)
                        except (URLPolicyError, TypeError):
                            quarantine.append("invalid_url")
                        else:
                            clean_source = source or feed_name
                            if len(clean_source) > limits.source_chars:
                                clean_source = clean_source[: limits.source_chars - 3] + "..."
                            provenance = ""
                            try:
                                provenance = (
                                    validate_url(source_url, max_length=limits.url_chars, allow_http=True).normalized_url
                                    if source_url
                                    else ""
                                )
                            except (URLPolicyError, TypeError):
                                # Provenance is optional.  It must never grant
                                # trusted-domain scoring when it is malformed.
                                provenance = ""
                            results.append(
                                NewsItem(
                                    title=title,
                                    url=validated.normalized_url,
                                    source=clean_source,
                                    source_url=provenance,
                                    published_at=_parse_date(published_text),
                                    summary=summary,
                                    collector=collector,
                                    query_name=query_name,
                                    source_id=source_id,
                                    source_host=validated.hostname,
                                )
                            )
                    element.clear()
                depth -= 1
        parser.close()
    except ET.ParseError as exc:
        raise SourceDataError("xml_parse_error") from exc
    if root_name not in {"rss", "feed"}:
        raise SourceDataError("document_type_invalid")
    return results


def parse_feed_result(
    payload: bytes,
    feed_name: str,
    collector: str,
    query_name: str = "",
    *,
    source_id: str = "",
    limits: CollectionLimits | None = None,
) -> tuple[list[NewsItem], list[str]]:
    """Parse an RSS/Atom payload and return items plus bounded quarantine codes."""

    chosen_limits = limits or CollectionLimits()
    if not isinstance(payload, bytes | bytearray):
        raise SourceDataError("invalid_payload")
    payload = bytes(payload)
    quarantine: list[str] = []
    try:
        return _parse_xml_once(payload, feed_name, collector, query_name, source_id, chosen_limits, quarantine), quarantine
    except MemoryError:
        raise
    except ET.ParseError:
        raise
    except SourceDataError:
        raise
    except Exception as exc:
        # The parser boundary is source-local; expose a stable class to the
        # collector rather than attacker-controlled exception text.
        raise SourceDataError("xml_parse_error") from exc


def parse_feed(
    payload: bytes,
    feed_name: str,
    collector: str,
    query_name: str = "",
    *,
    source_id: str = "",
    limits: CollectionLimits | None = None,
) -> list[NewsItem]:
    """Compatibility parser returning only accepted items."""

    chosen_limits = limits or CollectionLimits()
    if not isinstance(payload, bytes | bytearray):
        raise SourceDataError("invalid_payload")
    try:
        items, _ = parse_feed_result(
            payload,
            feed_name,
            collector,
            query_name,
            source_id=source_id,
            limits=chosen_limits,
        )
        return items
    except SourceDataError as first_error:
        if first_error.reason_code in {"xml_dtd_disallowed", "response_too_large", "xml_depth_limit", "xml_node_limit", "entry_limit"}:
            raise
        # Preserve the original prototype's useful repair behavior for bare
        # ampersands, but keep it inside the same byte/depth/node budgets.
        repaired = _repair_xml(payload, chosen_limits)
        items, _ = parse_feed_result(
            repaired,
            feed_name,
            collector,
            query_name,
            source_id=source_id,
            limits=chosen_limits,
        )
        return items


def _google_news_url(query: str, config: Mapping[str, Any], lookback_days: int) -> str:
    locale = str(config.get("locale", "id"))
    country = str(config.get("country", "ID"))
    edition = str(config.get("edition", "ID:id"))
    query_with_time = f"{query} when:{lookback_days}d"
    return "https://news.google.com/rss/search?" + urlencode({"q": query_with_time, "hl": locale, "gl": country, "ceid": edition})


def _fetch(url: str, timeout: int, *, config: Mapping[str, Any] | None = None) -> bytes:
    return fetch_bytes(
        url,
        timeout,
        limits=_limits(config),
        network_policy=_network_policy(config),
    )


def _feed_dict(feed: Any) -> dict[str, str]:
    if hasattr(feed, "id"):
        return {"id": feed.id, "name": feed.name, "url": feed.url}
    if not isinstance(feed, dict):
        raise SourceDataError("source_schema_invalid")
    return {"id": str(feed.get("id", feed.get("name", "source"))), "name": str(feed.get("name", "source")), "url": str(feed.get("url", ""))}


def _collect_rss(
    feed: Mapping[str, str],
    timeout: int,
    collector: str = "rss",
    *,
    config: Mapping[str, Any] | None = None,
) -> SourceResult:
    started = time.monotonic()
    source_id = feed["id"]
    source_name = feed["name"]
    try:
        payload = _fetch(feed["url"], timeout, config=config)
        chosen_limits = _limits(config)
        try:
            items, quarantine = parse_feed_result(
                payload,
                source_name,
                collector,
                feed.get("query_name", ""),
                source_id=source_id,
                limits=chosen_limits,
            )
        except SourceDataError as exc:
            # Repair only parser-shape failures; DTD and resource-limit
            # failures remain fail-closed.
            if exc.reason_code in {"xml_dtd_disallowed", "response_too_large", "xml_depth_limit", "xml_node_limit", "entry_limit"}:
                raise
            items, quarantine = parse_feed_result(
                _repair_xml(payload, chosen_limits),
                source_name,
                collector,
                feed.get("query_name", ""),
                source_id=source_id,
                limits=chosen_limits,
            )
        outcome = "failed" if not items and quarantine else "succeeded"
        return SourceResult(
            source_id=source_id,
            source_name=source_name,
            outcome=outcome,
            items=items,
            duration_ms=int((time.monotonic() - started) * 1000),
            bytes_read=len(payload),
            accepted_count=len(items),
            quarantined_count=len(quarantine),
            reason_code="all_items_quarantined" if not items and quarantine else ("item_quarantine" if quarantine else ""),
            error_class="SourceDataError" if not items and quarantine else "",
            error="all feed entries were quarantined" if not items and quarantine else "",
        )
    except MemoryError:
        raise
    except Exception as exc:
        return _failed_source_result(source_id, source_name, started, exc)


def _collect_gdelt(
    query: Mapping[str, str],
    gdelt: Mapping[str, Any],
    timeout: int,
    *,
    config: Mapping[str, Any] | None = None,
) -> SourceResult:
    started = time.monotonic()
    source_id = query["id"]
    source_name = f"GDELT: {query['name']}"
    try:
        params = {
            "query": query["query"],
            "mode": "artlist",
            "maxrecords": str(min(int(gdelt.get("max_records", 75)), _limits(config).entries_per_source)),
            "timespan": str(gdelt.get("timespan", "3d")),
            "sort": "datedesc",
            "format": "json",
        }
        url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urlencode(params)
        payload = _fetch(url, timeout, config=config)
        try:
            data = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceDataError("json_parse_error") from exc
        if not isinstance(data, dict) or "articles" not in data or not isinstance(data.get("articles"), list):
            raise SourceDataError("json_schema_invalid")
        articles = data.get("articles", [])
        if len(articles) > _limits(config).entries_per_source:
            raise SourceDataError("entry_limit")
        results: list[NewsItem] = []
        quarantine = 0
        for article in articles:
            if not isinstance(article, dict):
                quarantine += 1
                continue
            title = _bounded_text(article.get("title", ""), _limits(config).title_chars)[0]
            link = article.get("url", "")
            if not title:
                quarantine += 1
                continue
            try:
                validated = validate_url(link, max_length=_limits(config).url_chars, allow_http=True)
            except (URLPolicyError, TypeError):
                quarantine += 1
                continue
            domain = _bounded_text(article.get("domain", ""), _limits(config).source_chars)[0]
            results.append(
                NewsItem(
                    title=title,
                    url=validated.normalized_url,
                    source=domain or "GDELT source",
                    source_url="",
                    published_at=_parse_date(article.get("seendate", "")),
                    collector="gdelt",
                    query_name=query["name"],
                    source_id=source_id,
                    source_host=validated.hostname,
                )
            )
        outcome = "failed" if not results and quarantine else "succeeded"
        return SourceResult(
            source_id=source_id,
            source_name=source_name,
            outcome=outcome,
            items=results,
            duration_ms=int((time.monotonic() - started) * 1000),
            bytes_read=len(payload),
            accepted_count=len(results),
            quarantined_count=quarantine,
            reason_code="all_items_quarantined" if not results and quarantine else ("item_quarantine" if quarantine else ""),
            error_class="SourceDataError" if not results and quarantine else "",
            error="all source entries were quarantined" if not results and quarantine else "",
        )
    except MemoryError:
        raise
    except Exception as exc:
        return _failed_source_result(source_id, source_name, started, exc)


def _job_host(function: Callable[..., SourceResult], args: tuple[Any, ...], source_id: str) -> str:
    if function is _collect_gdelt:
        return "api.gdeltproject.org"
    source = args[0] if args else {}
    url = source.get("url", "") if isinstance(source, Mapping) else ""
    try:
        return (urlsplit(str(url)).hostname or source_id).casefold()
    except ValueError:
        return source_id.casefold()


def _send_ipc_frame(connection: Any, payload: tuple[Any, ...], max_frame_bytes: int) -> None:
    """Send one bounded, explicitly framed worker response."""

    try:
        frame = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    except (MemoryError, pickle.PickleError, TypeError, ValueError) as exc:
        frame = pickle.dumps(("exception", type(exc).__name__[:80], "worker_result_unserializable"))
    if len(frame) > max_frame_bytes:
        frame = pickle.dumps(("exception", "WorkerFrameTooLarge", "worker_result_too_large"))
    connection.send_bytes(frame)


def _start_ipc_receiver(connection: Any, max_frame_bytes: int) -> Queue[tuple[str, Any]]:
    """Read a pipe frame off the supervisor loop.

    ``Connection.poll()`` only proves that bytes are available; a malicious
    or crashed worker can still leave an incomplete length-prefixed frame for
    ``recv_bytes()`` to wait on.  The daemon reader lets the supervisor keep
    enforcing the source/cycle deadline while the process is terminated and
    the pipe is closed on timeout.
    """

    result: Queue[tuple[str, Any]] = Queue(maxsize=1)

    def read() -> None:
        try:
            result.put(("frame", connection.recv_bytes(maxlength=max_frame_bytes)))
        except BaseException as exc:
            with contextlib.suppress(Exception):
                result.put(("error", exc))

    threading.Thread(target=read, name="meco-ipc-receiver", daemon=True).start()
    return result


def _source_process_entry(
    connection: Any,
    function: Callable[..., SourceResult],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    max_frame_bytes: int = DEFAULT_IPC_FRAME_BYTES,
) -> None:
    """Run one source in an independently terminable process.

    The parent owns the deadline and the process lifecycle.  Only a bounded,
    typed result crosses the pipe; exception text is never used as a protocol.
    """
    # Spawned workers start without the parent logging configuration;
    # without this, failures fall through to logging.lastResort and reach
    # stderr raw, traceback and secrets included. Install the redacting
    # handler so worker diagnostics are sanitized like everywhere else.
    # Never a file: children must not rotate the parent log.
    if not logging.getLogger().handlers:
        configure_logging(level=os.getenv("LOG_LEVEL", "WARNING"), stream=sys.stderr)

    try:
        result = function(*args, **kwargs)
        if not isinstance(result, SourceResult):
            raise TypeError("source worker returned an invalid result")
        _send_ipc_frame(connection, ("result", result), max_frame_bytes)
    except MemoryError:
        with contextlib.suppress(BrokenPipeError, EOFError, OSError):
            _send_ipc_frame(connection, ("memory_error",), max_frame_bytes)
    except BaseException as exc:
        reason = str(getattr(exc, "reason_code", "source_exception"))[:80] or "source_exception"
        with contextlib.suppress(BrokenPipeError, EOFError, OSError):
            _send_ipc_frame(connection, ("exception", type(exc).__name__[:80], reason), max_frame_bytes)
    finally:
        connection.close()


def _deadline_result(source_id: str, source_name: str, reason_code: str = "source_deadline_exceeded") -> SourceResult:
    return SourceResult(
        source_id=source_id,
        source_name=source_name,
        outcome="failed",
        reason_code=reason_code,
        error_class="TimeoutError",
        error=reason_code.replace("_", " "),
    )


def _worker_failure(source_id: str, source_name: str, error_class: str, reason_code: str) -> SourceResult:
    stable_reason = reason_code[:80] or "source_exception"
    stable_class = error_class[:80] or "RuntimeError"
    return SourceResult(
        source_id=source_id,
        source_name=source_name,
        outcome="failed",
        reason_code=stable_reason,
        error_class=stable_class,
        error=f"{stable_class}: {stable_reason}",
    )


def _terminate_worker(process: Any) -> None:
    """Terminate and join a worker; never leave a child detached."""

    if process.is_alive():
        process.terminate()
    process.join(timeout=2)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=2)
    if process.is_alive():
        raise RuntimeError("source worker could not be reaped")


def collect_all(config: Mapping[str, Any] | AppConfig) -> CollectionResult:
    """Collect configured sources concurrently with bounded source isolation."""

    started_at = datetime.now(UTC)
    started = time.monotonic()
    limits = _limits(config)
    timeout = int(config.get("request_timeout_seconds", 25))
    # AppConfig deliberately keeps its compatibility mapping immutable.  Spawned
    # workers need a plain, pickleable snapshot, while the worker-side helpers
    # reconstruct the validated collection/network limits from that snapshot.
    worker_config: Mapping[str, Any] = config.as_dict() if isinstance(config, AppConfig) else config
    jobs: list[tuple[str, str, Any, tuple[Any, ...], dict[str, Any]]] = []
    for feed_value in config.get("rss_feeds", []):
        feed = _feed_dict(feed_value)
        jobs.append((feed["id"], feed["name"], _collect_rss, (feed, timeout), {"config": worker_config}))

    google = config.get("google_news", {})
    if isinstance(google, Mapping) and google.get("enabled", True):
        for query_value in google.get("queries", []):
            query = {
                "id": str(query_value.get("id", query_value["name"])),
                "name": str(query_value["name"]),
                "query": str(query_value["query"]),
            }
            feed = {
                "id": f"google-{query['id']}",
                "name": f"Google News: {query['name']}",
                "url": _google_news_url(query["query"], google, int(config.get("lookback_days", 3))),
                "query_name": query["name"],
            }
            jobs.append((feed["id"], feed["name"], _collect_rss, (feed, timeout, "google_news"), {"config": worker_config}))

    gdelt = config.get("gdelt", {})
    if isinstance(gdelt, Mapping) and gdelt.get("enabled", True):
        for query_value in gdelt.get("queries", []):
            query = {
                "id": str(query_value.get("id", query_value["name"])),
                "name": str(query_value["name"]),
                "query": str(query_value["query"]),
            }
            jobs.append((f"gdelt-{query['id']}", f"GDELT: {query['name']}", _collect_gdelt, (query, gdelt, timeout), {"config": worker_config}))
    if len(jobs) > limits.max_sources:
        jobs = jobs[: limits.max_sources]

    if not jobs:
        return CollectionResult([], [], started_at, 0)

    # A thread cannot be terminated if a source ignores its socket timeout.
    # Spawn one process per active source instead.  Host concurrency is owned
    # by this parent scheduler, so no lock/semaphore is sent across the spawn
    # boundary and all children remain pickle-safe on Windows.
    context = mp.get_context("spawn")
    max_workers = min(10, max(1, len(jobs)))
    pending_jobs = list(jobs)
    active: dict[Any, tuple[Any, str, str, str, float]] = {}
    receivers: dict[Any, Queue[tuple[str, Any]]] = {}
    host_active: dict[str, int] = {}
    results_by_id: dict[str, SourceResult] = {}
    deadline = time.monotonic() + limits.cycle_deadline_seconds
    timed_out = False

    def cleanup_active() -> None:
        cleanup_error: RuntimeError | None = None
        for process, (connection, _source_id, _source_name, _host, _started) in list(active.items()):
            try:
                connection.close()
                _terminate_worker(process)
            except RuntimeError as exc:
                cleanup_error = cleanup_error or exc
            finally:
                active.pop(process, None)
                receivers.pop(process, None)
        if cleanup_error:
            raise cleanup_error

    try:
        while pending_jobs or active:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break

            # Start as many eligible jobs as the global and per-host budgets allow.
            started_one = True
            while pending_jobs and len(active) < max_workers and started_one and time.monotonic() < deadline:
                started_one = False
                for index, (source_id, source_name, function, args, kwargs) in enumerate(pending_jobs):
                    host = _job_host(function, args, source_id)
                    if host_active.get(host, 0) >= MAX_CONCURRENT_REQUESTS_PER_HOST:
                        continue
                    pending_jobs.pop(index)
                    parent, child = context.Pipe(duplex=False)
                    process = context.Process(
                        target=_source_process_entry,
                        args=(child, function, args, kwargs, limits.ipc_frame_bytes),
                        name=f"meco-source-{source_id}",
                    )
                    try:
                        process.start()
                    except BaseException as exc:
                        parent.close()
                        child.close()
                        results_by_id[source_id] = _worker_failure(source_id, source_name, type(exc).__name__, "process_start_failed")
                    else:
                        child.close()
                        active[process] = (parent, source_id, source_name, host, time.monotonic())
                        host_active[host] = host_active.get(host, 0) + 1
                    started_one = True
                    break

            progressed = False
            for process, (connection, source_id, source_name, host, source_started) in list(active.items()):
                payload: tuple[Any, ...] | None = None
                receiver = receivers.get(process)
                if receiver is None and connection.poll():
                    receiver = _start_ipc_receiver(connection, limits.ipc_frame_bytes)
                    receivers[process] = receiver
                if receiver is not None:
                    try:
                        receiver_status, receiver_value = receiver.get_nowait()
                    except Empty:
                        # A short bounded drain closes the normal scheduling
                        # race where a worker has exited after sending its
                        # complete frame but the daemon reader has not yet
                        # been scheduled.  Never wait here while a live
                        # worker may still be producing a partial frame.
                        if not process.is_alive():
                            try:
                                receiver_status, receiver_value = receiver.get(timeout=0.05)
                            except Empty:
                                receiver_status, receiver_value = "pending", None
                        else:
                            receiver_status, receiver_value = "pending", None
                    if receiver_status == "frame":
                        receivers.pop(process, None)
                        try:
                            candidate = pickle.loads(receiver_value)
                            payload = candidate if isinstance(candidate, tuple) else ("invalid",)
                        except (EOFError, OSError, pickle.PickleError, TypeError, ValueError):
                            payload = ("worker_exit",)
                    elif receiver_status == "error":
                        receivers.pop(process, None)
                        payload = ("worker_exit",)
                if payload is None:
                    if process.is_alive() and time.monotonic() - source_started < limits.source_deadline_seconds:
                        continue
                    if process.is_alive():
                        payload = ("source_deadline", "partial_ipc" if receiver is not None else "deadline")
                progressed = True
                connection.close()
                process.join(timeout=0.2)
                if process.is_alive():
                    _terminate_worker(process)
                active.pop(process, None)
                receivers.pop(process, None)
                host_active[host] -= 1
                if payload is None or payload[0] in {"worker_exit", "invalid"}:
                    results_by_id[source_id] = _worker_failure(source_id, source_name, "WorkerProcessError", "worker_no_result")
                elif payload[0] == "source_deadline":
                    results_by_id[source_id] = _deadline_result(source_id, source_name, "source_deadline_exceeded")
                elif payload[0] == "memory_error":
                    raise MemoryError("source worker exhausted memory")
                elif payload[0] == "result" and len(payload) == 2 and isinstance(payload[1], SourceResult):
                    results_by_id[source_id] = payload[1]
                elif payload[0] == "exception":
                    results_by_id[source_id] = _worker_failure(
                        source_id,
                        source_name,
                        str(payload[1]) if len(payload) > 1 else "WorkerProcessError",
                        str(payload[2]) if len(payload) > 2 else "source_exception",
                    )
                else:
                    results_by_id[source_id] = _worker_failure(source_id, source_name, "WorkerProcessError", "worker_invalid_result")

            if not progressed:
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    finally:
        if timed_out:
            for process, (connection, source_id, source_name, host, _source_started) in list(active.items()):
                connection.close()
                _terminate_worker(process)
                host_active[host] -= 1
                active.pop(process, None)
                receivers.pop(process, None)
                results_by_id[source_id] = _deadline_result(source_id, source_name, "cycle_deadline_exceeded")
            for source_id, source_name, _function, _args, _kwargs in pending_jobs:
                results_by_id[source_id] = _deadline_result(source_id, source_name, "cycle_deadline_exceeded")
        elif active:
            cleanup_active()

    source_results = [
        results_by_id[source_id]
        for source_id, _name, _function, _args, _kwargs in sorted(jobs, key=lambda job: job[0])
        if source_id in results_by_id
    ]
    items = [item for result in source_results for item in result.items]
    return CollectionResult(items, source_results, started_at, int((time.monotonic() - started) * 1000))
