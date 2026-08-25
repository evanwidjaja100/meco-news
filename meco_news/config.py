"""Typed, fail-closed application configuration.

The JSON file remains intentionally human-editable.  It is converted to
validated dataclasses at the process boundary and the mapping interface is
kept as a compatibility layer for the original small API.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from collections.abc import Iterator, Mapping

from .timezones import get_timezone
from .urls import URLPolicyError, validate_url


class ConfigurationError(ValueError):
    """Configuration is malformed or outside the reviewed safety envelope."""


@dataclass(frozen=True, slots=True)
class CollectionLimits:
    response_bytes: int = 5 * 1024 * 1024
    source_deadline_seconds: int = 35
    socket_timeout_seconds: int = 5
    max_redirects: int = 2
    entries_per_source: int = 250
    xml_depth: int = 32
    xml_nodes: int = 10_000
    title_chars: int = 512
    summary_chars: int = 2_048
    source_chars: int = 160
    url_chars: int = 2_048
    fuzzy_candidates: int = 500
    fuzzy_comparisons: int = 20_000
    max_sources: int = 100
    max_query_chars: int = 1_024


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    allowed_redirect_hosts: frozenset[str] = frozenset()
    same_host_redirects_only: bool = True
    require_https: bool = True


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    enabled: bool = True
    max_attempts: int = 4
    base_delay_seconds: int = 60
    max_delay_seconds: int = 3_600
    jitter_seconds: int = 15


@dataclass(frozen=True, slots=True)
class FeedConfig:
    id: str
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class QueryConfig:
    id: str
    name: str
    query: str


@dataclass(frozen=True, slots=True)
class TopicConfig:
    id: str
    label: str
    why: str
    strong_terms: tuple[str, ...]
    keywords: tuple[str, ...]
    requires_any: tuple[str, ...]


_TOP_LEVEL_KEYS = {
    "company",
    "timezone",
    "delivery_time",
    "daily_min",
    "daily_max",
    "max_per_topic",
    "max_per_domain",
    "minimum_score",
    "fallback_score",
    "request_timeout_seconds",
    "lookback_days",
    "title_dedupe_days",
    "url_retention_days",
    "future_skew_hours",
    "missing_date_policy",
    "lease_ttl_seconds",
    "trusted_domains",
    "business_signals",
    "indonesia_terms",
    "negative_terms",
    "topics",
    "rss_feeds",
    "google_news",
    "gdelt",
    "limits",
    "network_policy",
    "retry_policy",
}
_LIMIT_KEYS = {
    "response_bytes",
    "source_deadline_seconds",
    "socket_timeout_seconds",
    "max_redirects",
    "entries_per_source",
    "xml_depth",
    "xml_nodes",
    "title_chars",
    "summary_chars",
    "source_chars",
    "url_chars",
    "fuzzy_candidates",
    "fuzzy_comparisons",
    "max_sources",
    "max_query_chars",
}
_NETWORK_KEYS = {"allowed_redirect_hosts", "same_host_redirects_only", "require_https"}
_RETRY_KEYS = {"enabled", "max_attempts", "base_delay_seconds", "max_delay_seconds", "jitter_seconds"}
_TOPIC_KEYS = {"id", "label", "why", "strong_terms", "keywords", "requires_any"}
_FEED_KEYS = {"id", "name", "url"}
_QUERY_KEYS = {"id", "name", "query"}
_GOOGLE_KEYS = {"enabled", "locale", "country", "edition", "queries"}
_GDELT_KEYS = {"enabled", "timespan", "max_records", "queries"}
_MISSING_DATE_POLICIES = {"exclude", "include"}


def _ensure_keys(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"Unknown {context} key(s): {', '.join(unknown)}")


def _string(value: Any, context: str, *, nonempty: bool = True, max_chars: int = 4096) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"{context} must be a string")
    if nonempty and not value.strip():
        raise ConfigurationError(f"{context} must not be empty")
    if len(value) > max_chars:
        raise ConfigurationError(f"{context} exceeds {max_chars} characters")
    return value.strip()


def _integer(value: Any, context: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{context} must be an integer")
    if not low <= value <= high:
        raise ConfigurationError(f"{context} must be between {low} and {high}")
    return int(value)


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{context} must be a boolean")
    return value


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:80] or "source"


def _list_of_strings(value: Any, context: str, *, max_items: int = 500, max_chars: int = 512) -> list[str]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{context} must be a list")
    if len(value) > max_items:
        raise ConfigurationError(f"{context} has too many entries")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_string(item, f"{context}[{index}]", max_chars=max_chars))
    return result


def _validate_hhmm(value: Any) -> str:
    text = _string(value, "delivery_time", max_chars=5)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ConfigurationError("delivery_time must use HH:MM")
    return text


@dataclass(frozen=True, slots=True)
class AppConfig(Mapping[str, Any]):
    company: str
    timezone: str
    delivery_time: str
    daily_min: int
    daily_max: int
    max_per_topic: int
    max_per_domain: int
    minimum_score: int
    fallback_score: int
    request_timeout_seconds: int
    lookback_days: int
    title_dedupe_days: int
    url_retention_days: int
    future_skew_hours: int
    missing_date_policy: str
    lease_ttl_seconds: int
    trusted_domains: tuple[str, ...]
    business_signals: tuple[str, ...]
    indonesia_terms: tuple[str, ...]
    negative_terms: tuple[str, ...]
    topics_typed: tuple[TopicConfig, ...]
    rss_typed: tuple[FeedConfig, ...]
    google_queries: tuple[QueryConfig, ...]
    gdelt_queries: tuple[QueryConfig, ...]
    limits: CollectionLimits
    network_policy: NetworkPolicy
    retry_policy: RetryPolicy
    _raw: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self._raw[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._raw)

    def __len__(self) -> int:
        return len(self._raw)

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)

    @property
    def topics(self) -> tuple[TopicConfig, ...]:
        return self.topics_typed

    @property
    def rss_feeds(self) -> tuple[FeedConfig, ...]:
        return self.rss_typed

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._raw)

    def redacted(self) -> dict[str, Any]:
        value = self.as_dict()
        # The JSON configuration contains no secrets today, but this keeps
        # config-show safe if a future non-secret credential reference is added.
        for key in list(value):
            if any(secret in key.casefold() for secret in ("token", "secret", "password", "authorization")):
                value[key] = "<redacted>"
        return value

    @property
    def config_hash(self) -> str:
        payload = json.dumps(self._raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_limits(raw: Mapping[str, Any]) -> CollectionLimits:
    _ensure_keys(raw, _LIMIT_KEYS, "limits")
    defaults = CollectionLimits()
    values: dict[str, int] = {}
    hard = {
        "response_bytes": 20 * 1024 * 1024,
        "source_deadline_seconds": 120,
        "socket_timeout_seconds": 30,
        "max_redirects": 8,
        "entries_per_source": 2_000,
        "xml_depth": 128,
        "xml_nodes": 100_000,
        "title_chars": 4_096,
        "summary_chars": 16_384,
        "source_chars": 1_024,
        "url_chars": 8_192,
        "fuzzy_candidates": 2_000,
        "fuzzy_comparisons": 100_000,
        "max_sources": 500,
        "max_query_chars": 8_192,
    }
    minimums = {
        "response_bytes": 1024,
        "source_deadline_seconds": 1,
        "socket_timeout_seconds": 1,
        "max_redirects": 0,
        "entries_per_source": 1,
        "xml_depth": 4,
        "xml_nodes": 100,
        "title_chars": 32,
        "summary_chars": 64,
        "source_chars": 16,
        "url_chars": 128,
        "fuzzy_candidates": 1,
        "fuzzy_comparisons": 1,
        "max_sources": 1,
        "max_query_chars": 32,
    }
    for field_name in defaults.__dataclass_fields__:
        raw_value = raw.get(field_name, getattr(defaults, field_name))
        values[field_name] = _integer(raw_value, f"limits.{field_name}", minimums[field_name], hard[field_name])
    return CollectionLimits(**values)


def _parse_topics(raw_topics: Any) -> tuple[TopicConfig, ...]:
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ConfigurationError("topics must be a nonempty list")
    result: list[TopicConfig] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_topics):
        if not isinstance(raw, dict):
            raise ConfigurationError(f"topics[{index}] must be an object")
        _ensure_keys(raw, _TOPIC_KEYS, f"topics[{index}]")
        topic_id = _string(raw.get("id"), f"topics[{index}].id", max_chars=80).casefold()
        if topic_id in seen:
            raise ConfigurationError(f"duplicate topic id: {topic_id}")
        seen.add(topic_id)
        result.append(
            TopicConfig(
                id=topic_id,
                label=_string(raw.get("label"), f"topics[{index}].label", max_chars=160),
                why=_string(raw.get("why"), f"topics[{index}].why", max_chars=512),
                strong_terms=tuple(_list_of_strings(raw.get("strong_terms", []), f"topics[{index}].strong_terms")),
                keywords=tuple(_list_of_strings(raw.get("keywords", []), f"topics[{index}].keywords")),
                requires_any=tuple(_list_of_strings(raw.get("requires_any", []), f"topics[{index}].requires_any")),
            )
        )
    return tuple(result)


def _parse_feeds(raw_feeds: Any, limits: CollectionLimits) -> tuple[tuple[FeedConfig, ...], list[dict[str, str]]]:
    if not isinstance(raw_feeds, list) or not raw_feeds:
        raise ConfigurationError("rss_feeds must be a nonempty list")
    if len(raw_feeds) > limits.max_sources:
        raise ConfigurationError("rss_feeds exceeds limits.max_sources")
    result: list[FeedConfig] = []
    raw_result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_feeds):
        if not isinstance(raw, dict):
            raise ConfigurationError(f"rss_feeds[{index}] must be an object")
        _ensure_keys(raw, _FEED_KEYS, f"rss_feeds[{index}]")
        name = _string(raw.get("name"), f"rss_feeds[{index}].name", max_chars=160)
        source_id = _string(raw.get("id", _slug(name)), f"rss_feeds[{index}].id", max_chars=80).casefold()
        if source_id in seen:
            raise ConfigurationError(f"duplicate source id: {source_id}")
        seen.add(source_id)
        url = _string(raw.get("url"), f"rss_feeds[{index}].url", max_chars=limits.url_chars)
        if not url.casefold().startswith("https://"):
            raise ConfigurationError(f"rss_feeds[{index}].url must use HTTPS")
        try:
            normalized_url = validate_url(url, max_length=limits.url_chars).normalized_url
        except URLPolicyError as exc:
            raise ConfigurationError(f"rss_feeds[{index}].url is not allowed") from exc
        result.append(FeedConfig(source_id, name, normalized_url))
        raw_result.append({"id": source_id, "name": name, "url": normalized_url})
    return tuple(result), raw_result


def _parse_queries(raw_queries: Any, context: str, limits: CollectionLimits) -> tuple[tuple[QueryConfig, ...], list[dict[str, str]]]:
    if not isinstance(raw_queries, list):
        raise ConfigurationError(f"{context}.queries must be a list")
    result: list[QueryConfig] = []
    raw_result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_queries):
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{context}.queries[{index}] must be an object")
        _ensure_keys(raw, _QUERY_KEYS, f"{context}.queries[{index}]")
        name = _string(raw.get("name"), f"{context}.queries[{index}].name", max_chars=160)
        query_id = _string(raw.get("id", _slug(name)), f"{context}.queries[{index}].id", max_chars=80).casefold()
        if query_id in seen:
            raise ConfigurationError(f"duplicate {context} query id: {query_id}")
        seen.add(query_id)
        query = _string(raw.get("query"), f"{context}.queries[{index}].query", max_chars=limits.max_query_chars)
        result.append(QueryConfig(query_id, name, query))
        raw_result.append({"id": query_id, "name": name, "query": query})
    return tuple(result), raw_result


def _parse_optional_source_block(
    raw: Mapping[str, Any], context: str, limits: CollectionLimits
) -> tuple[tuple[QueryConfig, ...], list[dict[str, str]]]:
    queries, raw_queries = _parse_queries(raw.get("queries", []), context, limits)
    return queries, raw_queries


def load_dotenv(path: str | Path = ".env") -> None:
    """Load a small .env file without adding a runtime dependency."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path is not None else Path(os.getenv("MECO_CONFIG", "config/watchlist.json"))
    try:
        with config_path.open("r", encoding="utf-8-sig") as handle:
            raw_input = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Unable to read configuration: {config_path}") from exc
    if not isinstance(raw_input, dict):
        raise ConfigurationError("configuration root must be an object")
    _ensure_keys(raw_input, _TOP_LEVEL_KEYS, "configuration")

    company = _string(raw_input.get("company"), "company", max_chars=160)
    timezone_name = _string(raw_input.get("timezone"), "timezone", max_chars=80)
    try:
        get_timezone(timezone_name)
    except Exception as exc:
        raise ConfigurationError(f"Unknown timezone: {timezone_name}") from exc
    delivery_time = _validate_hhmm(raw_input.get("delivery_time", "07:00"))
    daily_min = _integer(raw_input.get("daily_min"), "daily_min", 1, 100)
    daily_max = _integer(raw_input.get("daily_max"), "daily_max", daily_min, 100)
    max_per_topic = _integer(raw_input.get("max_per_topic", 3), "max_per_topic", 1, daily_max)
    max_per_domain = _integer(raw_input.get("max_per_domain", 2), "max_per_domain", 1, daily_max)
    minimum_score = _integer(raw_input.get("minimum_score", 0), "minimum_score", -100, 100)
    fallback_score = _integer(raw_input.get("fallback_score", minimum_score), "fallback_score", -100, 100)
    if fallback_score > minimum_score:
        raise ConfigurationError("fallback_score must not exceed minimum_score")
    request_timeout = _integer(raw_input.get("request_timeout_seconds", 25), "request_timeout_seconds", 1, 120)
    lookback_days = _integer(raw_input.get("lookback_days", 7), "lookback_days", 1, 31)
    title_dedupe_days = _integer(raw_input.get("title_dedupe_days", 14), "title_dedupe_days", 0, 3650)
    url_retention_days = _integer(raw_input.get("url_retention_days", 365), "url_retention_days", 1, 3650)
    future_skew_hours = _integer(raw_input.get("future_skew_hours", 6), "future_skew_hours", 0, 48)
    missing_date_policy = _string(raw_input.get("missing_date_policy", "exclude"), "missing_date_policy", max_chars=16).casefold()
    if missing_date_policy not in _MISSING_DATE_POLICIES:
        raise ConfigurationError("missing_date_policy must be exclude or include")
    lease_ttl = _integer(raw_input.get("lease_ttl_seconds", 180), "lease_ttl_seconds", 30, 3600)

    limits = _parse_limits(raw_input.get("limits", {}))
    if request_timeout > limits.source_deadline_seconds:
        raise ConfigurationError("request_timeout_seconds must not exceed limits.source_deadline_seconds")
    if lease_ttl < limits.source_deadline_seconds + 30:
        raise ConfigurationError("lease_ttl_seconds must exceed the source deadline by at least 30 seconds")
    topics = _parse_topics(raw_input.get("topics"))
    feeds, raw_feeds = _parse_feeds(raw_input.get("rss_feeds"), limits)

    trusted = tuple(
        domain.casefold().strip().rstrip(".")
        for domain in _list_of_strings(raw_input.get("trusted_domains", []), "trusted_domains", max_items=500, max_chars=255)
    )
    if any("/" in domain or ":" in domain for domain in trusted):
        raise ConfigurationError("trusted_domains must contain hostnames only")
    business = tuple(_list_of_strings(raw_input.get("business_signals", []), "business_signals"))
    indonesia = tuple(_list_of_strings(raw_input.get("indonesia_terms", []), "indonesia_terms"))
    negative = tuple(_list_of_strings(raw_input.get("negative_terms", []), "negative_terms"))

    google_raw = raw_input.get("google_news", {})
    if not isinstance(google_raw, dict):
        raise ConfigurationError("google_news must be an object")
    _ensure_keys(google_raw, _GOOGLE_KEYS, "google_news")
    google_enabled = _boolean(google_raw.get("enabled", True), "google_news.enabled")
    google_queries, raw_google_queries = _parse_queries(google_raw.get("queries", []), "google_news", limits)
    locale = _string(google_raw.get("locale", "id"), "google_news.locale", max_chars=16)
    country = _string(google_raw.get("country", "ID"), "google_news.country", max_chars=8)
    edition = _string(google_raw.get("edition", "ID:id"), "google_news.edition", max_chars=16)

    gdelt_raw = raw_input.get("gdelt", {})
    if not isinstance(gdelt_raw, dict):
        raise ConfigurationError("gdelt must be an object")
    _ensure_keys(gdelt_raw, _GDELT_KEYS, "gdelt")
    gdelt_enabled = _boolean(gdelt_raw.get("enabled", False), "gdelt.enabled")
    gdelt_queries, raw_gdelt_queries = _parse_optional_source_block(gdelt_raw, "gdelt", limits)
    gdelt_timespan = _string(gdelt_raw.get("timespan", "7d"), "gdelt.timespan", max_chars=16)
    gdelt_max_records = _integer(gdelt_raw.get("max_records", 75), "gdelt.max_records", 1, min(250, limits.entries_per_source))
    configured_source_count = len(feeds) + (len(google_queries) if google_enabled else 0) + (len(gdelt_queries) if gdelt_enabled else 0)
    if configured_source_count > limits.max_sources:
        raise ConfigurationError("configured source count exceeds limits.max_sources")

    network_raw = raw_input.get("network_policy", {})
    if not isinstance(network_raw, dict):
        raise ConfigurationError("network_policy must be an object")
    _ensure_keys(network_raw, _NETWORK_KEYS, "network_policy")
    allowed_hosts_list = [
        _string(host, "network_policy.allowed_redirect_hosts[]", max_chars=255).casefold().rstrip(".")
        for host in _list_of_strings(
            network_raw.get("allowed_redirect_hosts", []), "network_policy.allowed_redirect_hosts", max_items=100, max_chars=255
        )
    ]
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) for host in allowed_hosts_list):
        raise ConfigurationError("network_policy.allowed_redirect_hosts must contain hostnames only")
    allowed_hosts = frozenset(allowed_hosts_list)
    network_policy = NetworkPolicy(
        allowed_redirect_hosts=allowed_hosts,
        same_host_redirects_only=_boolean(network_raw.get("same_host_redirects_only", True), "network_policy.same_host_redirects_only"),
        require_https=_boolean(network_raw.get("require_https", True), "network_policy.require_https"),
    )

    retry_raw = raw_input.get("retry_policy", {})
    if not isinstance(retry_raw, dict):
        raise ConfigurationError("retry_policy must be an object")
    _ensure_keys(retry_raw, _RETRY_KEYS, "retry_policy")
    retry_policy = RetryPolicy(
        enabled=_boolean(retry_raw.get("enabled", True), "retry_policy.enabled"),
        max_attempts=_integer(retry_raw.get("max_attempts", 4), "retry_policy.max_attempts", 1, 20),
        base_delay_seconds=_integer(retry_raw.get("base_delay_seconds", 60), "retry_policy.base_delay_seconds", 1, 86_400),
        max_delay_seconds=_integer(retry_raw.get("max_delay_seconds", 3_600), "retry_policy.max_delay_seconds", 1, 604_800),
        jitter_seconds=_integer(retry_raw.get("jitter_seconds", 15), "retry_policy.jitter_seconds", 0, 3600),
    )
    if retry_policy.base_delay_seconds > retry_policy.max_delay_seconds:
        raise ConfigurationError("retry_policy.base_delay_seconds must not exceed max_delay_seconds")

    normalized = copy.deepcopy(raw_input)
    normalized.update(
        {
            "company": company,
            "timezone": timezone_name,
            "delivery_time": delivery_time,
            "daily_min": daily_min,
            "daily_max": daily_max,
            "max_per_topic": max_per_topic,
            "max_per_domain": max_per_domain,
            "minimum_score": minimum_score,
            "fallback_score": fallback_score,
            "request_timeout_seconds": request_timeout,
            "lookback_days": lookback_days,
            "title_dedupe_days": title_dedupe_days,
            "url_retention_days": url_retention_days,
            "future_skew_hours": future_skew_hours,
            "missing_date_policy": missing_date_policy,
            "lease_ttl_seconds": lease_ttl,
            "trusted_domains": list(trusted),
            "business_signals": list(business),
            "indonesia_terms": list(indonesia),
            "negative_terms": list(negative),
        }
    )
    normalized["topics"] = [
        {
            "id": topic.id,
            "label": topic.label,
            "why": topic.why,
            "strong_terms": list(topic.strong_terms),
            "keywords": list(topic.keywords),
            **({"requires_any": list(topic.requires_any)} if topic.requires_any else {}),
        }
        for topic in topics
    ]
    normalized["rss_feeds"] = raw_feeds
    normalized["google_news"] = {
        "enabled": google_enabled,
        "locale": locale,
        "country": country,
        "edition": edition,
        "queries": raw_google_queries,
    }
    normalized["gdelt"] = {
        "enabled": gdelt_enabled,
        "timespan": gdelt_timespan,
        "max_records": gdelt_max_records,
        "queries": raw_gdelt_queries,
    }
    normalized["limits"] = {field_name: getattr(limits, field_name) for field_name in limits.__dataclass_fields__}
    normalized["network_policy"] = {
        "allowed_redirect_hosts": sorted(network_policy.allowed_redirect_hosts),
        "same_host_redirects_only": network_policy.same_host_redirects_only,
        "require_https": network_policy.require_https,
    }
    normalized["retry_policy"] = {field_name: getattr(retry_policy, field_name) for field_name in retry_policy.__dataclass_fields__}

    return AppConfig(
        company=company,
        timezone=timezone_name,
        delivery_time=delivery_time,
        daily_min=daily_min,
        daily_max=daily_max,
        max_per_topic=max_per_topic,
        max_per_domain=max_per_domain,
        minimum_score=minimum_score,
        fallback_score=fallback_score,
        request_timeout_seconds=request_timeout,
        lookback_days=lookback_days,
        title_dedupe_days=title_dedupe_days,
        url_retention_days=url_retention_days,
        future_skew_hours=future_skew_hours,
        missing_date_policy=missing_date_policy,
        lease_ttl_seconds=lease_ttl,
        trusted_domains=trusted,
        business_signals=business,
        indonesia_terms=indonesia,
        negative_terms=negative,
        topics_typed=topics,
        rss_typed=feeds,
        google_queries=google_queries if google_enabled else tuple(),
        gdelt_queries=gdelt_queries if gdelt_enabled else tuple(),
        limits=limits,
        network_policy=network_policy,
        retry_policy=retry_policy,
        _raw=normalized,
    )
