"""Bounded RSS/Atom/search collection with per-source isolation."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
import html
import json
import logging
import re
import threading
import time
from typing import Any
from collections.abc import Callable, Iterator, Mapping
from urllib.parse import urlencode, urlsplit
import xml.etree.ElementTree as ET

from .config import AppConfig, CollectionLimits, NetworkPolicy
from .models import NewsItem
from .network import NetworkError, fetch_bytes
from .urls import URLPolicyError, validate_url


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
LOGGER = logging.getLogger(__name__)
MAX_CONCURRENT_REQUESTS_PER_HOST = 2


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
    return CollectionLimits()


def _network_policy(config: Mapping[str, Any] | None) -> NetworkPolicy:
    if config is not None and isinstance(config, AppConfig):
        return config.network_policy
    return NetworkPolicy()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _bounded_text(value: object, maximum: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    value = html.unescape(value)
    value = re.sub(r"<[^>]{0,4096}>", " ", value)
    value = _CONTROL_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
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
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise SourceDataError("xml_dtd_disallowed")
    parser = ET.XMLPullParser(events=("start", "end"))
    depth = 0
    nodes = 0
    entries_seen = 0
    results: list[NewsItem] = []
    try:
        for start in range(0, len(payload), 64 * 1024):
            parser.feed(payload[start : start + 64 * 1024])
            for event, element in parser.read_events():
                if event == "start":
                    depth += 1
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
        return SourceResult(
            source_id=source_id,
            source_name=source_name,
            outcome="succeeded",
            items=items,
            duration_ms=int((time.monotonic() - started) * 1000),
            bytes_read=len(payload),
            accepted_count=len(items),
            quarantined_count=len(quarantine),
            reason_code="item_quarantine" if quarantine else "",
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
        if not isinstance(data, dict) or not isinstance(data.get("articles", []), list):
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
        return SourceResult(
            source_id=source_id,
            source_name=source_name,
            outcome="succeeded",
            items=results,
            duration_ms=int((time.monotonic() - started) * 1000),
            bytes_read=len(payload),
            accepted_count=len(results),
            quarantined_count=quarantine,
            reason_code="item_quarantine" if quarantine else "",
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


def _run_source_job(
    source_id: str,
    source_name: str,
    function: Callable[..., SourceResult],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    semaphore: threading.BoundedSemaphore,
    wait_seconds: int,
) -> SourceResult:
    started = time.monotonic()
    if not semaphore.acquire(timeout=max(1, wait_seconds)):
        return SourceResult(
            source_id=source_id,
            source_name=source_name,
            outcome="failed",
            duration_ms=int((time.monotonic() - started) * 1000),
            reason_code="source_deadline_exceeded",
            error_class="TimeoutError",
            error="source concurrency budget exceeded",
        )
    try:
        return function(*args, **kwargs)
    finally:
        semaphore.release()


def collect_all(config: Mapping[str, Any] | AppConfig) -> CollectionResult:
    """Collect configured sources concurrently with bounded source isolation."""

    started_at = datetime.now(UTC)
    started = time.monotonic()
    limits = _limits(config)
    timeout = int(config.get("request_timeout_seconds", 25))
    jobs: list[tuple[str, str, Any, tuple[Any, ...], dict[str, Any]]] = []
    for feed_value in config.get("rss_feeds", []):
        feed = _feed_dict(feed_value)
        jobs.append((feed["id"], feed["name"], _collect_rss, (feed, timeout), {"config": config}))

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
            jobs.append((feed["id"], feed["name"], _collect_rss, (feed, timeout, "google_news"), {"config": config}))

    gdelt = config.get("gdelt", {})
    if isinstance(gdelt, Mapping) and gdelt.get("enabled", True):
        for query_value in gdelt.get("queries", []):
            query = {
                "id": str(query_value.get("id", query_value["name"])),
                "name": str(query_value["name"]),
                "query": str(query_value["query"]),
            }
            jobs.append((f"gdelt-{query['id']}", f"GDELT: {query['name']}", _collect_gdelt, (query, gdelt, timeout), {"config": config}))
    if len(jobs) > limits.max_sources:
        jobs = jobs[: limits.max_sources]

    if not jobs:
        return CollectionResult([], [], started_at, 0)

    executor = ThreadPoolExecutor(max_workers=min(10, max(1, len(jobs))), thread_name_prefix="meco-source")
    futures: dict[Future[SourceResult], tuple[str, str]] = {}
    host_semaphores: dict[str, threading.BoundedSemaphore] = {}
    for source_id, source_name, function, args, kwargs in jobs:
        host = _job_host(function, args, source_id)
        semaphore = host_semaphores.setdefault(host, threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS_PER_HOST))
        futures[
            executor.submit(_run_source_job, source_id, source_name, function, args, kwargs, semaphore, limits.source_deadline_seconds)
        ] = (
            source_id,
            source_name,
        )
    pending = set(futures)
    results_by_id: dict[str, SourceResult] = {}
    deadline = time.monotonic() + limits.source_deadline_seconds + 2
    try:
        while pending:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                for future in pending:
                    future.cancel()
                for future in pending:
                    source_id, source_name = futures[future]
                    results_by_id[source_id] = SourceResult(
                        source_id,
                        source_name,
                        "failed",
                        reason_code="source_deadline_exceeded",
                        error_class="TimeoutError",
                        error="source deadline exceeded",
                    )
                break
            done, pending = wait(pending, timeout=min(1.0, remaining), return_when=FIRST_COMPLETED)
            for future in done:
                source_id, source_name = futures[future]
                try:
                    result = future.result()
                    results_by_id[source_id] = result
                except MemoryError:
                    raise
                except Exception as exc:
                    results_by_id[source_id] = _failed_source_result(source_id, source_name, time.monotonic(), exc)
    finally:
        # Do not wait for a source-local thread after its absolute budget.  The
        # request itself has a deadline; cancelling a Python thread is not
        # possible, so the executor is deliberately detached here.
        executor.shutdown(wait=False, cancel_futures=True)

    source_results = [
        results_by_id[source_id] for source_id, _ in sorted(futures.values(), key=lambda pair: pair[0]) if source_id in results_by_id
    ]
    items = [item for result in source_results for item in result.items]
    return CollectionResult(items, source_results, started_at, int((time.monotonic() - started) * 1000))
