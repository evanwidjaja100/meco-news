"""Explainable ranking, freshness, identity, and deterministic deduplication."""

from __future__ import annotations

from collections import defaultdict
import copy
from datetime import datetime, timedelta, UTC
from difflib import SequenceMatcher
from dataclasses import dataclass
import re
from typing import Any
from collections.abc import Iterable, Mapping

from .config import AppConfig
from .models import NewsItem, normalized_title
from .urls import canonical_url


@dataclass(frozen=True, slots=True)
class DedupStats:
    """Auditable work performed by one bounded deduplication pass."""

    input_count: int
    url_groups: int
    exact_title_groups: int
    fuzzy_candidates: int
    fuzzy_comparisons: int
    comparison_budget: int
    budget_exhausted: bool
    unprocessed_candidates: int


def _contains(text: str, term: str) -> bool:
    term = str(term).casefold().strip()
    if not term:
        return False
    if re.fullmatch(r"[a-z0-9_]+", term):
        return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None
    return term in text


def _topic_value(topic: Any, key: str, default: Any = None) -> Any:
    if isinstance(topic, Mapping):
        return topic.get(key, default)
    return getattr(topic, key, default)


def _utc_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _domain(item: NewsItem) -> str:
    # Collectors populate this only after article-URL validation. Do not
    # reparse raw metadata here: trusted-domain scoring must never be granted
    # by an unchecked URL or RSS provenance field.
    return item.source_host.casefold().removeprefix("www.") if item.source_host else ""


def _is_aggregator(item: NewsItem) -> bool:
    """Classify only code-owned collector/host provenance as an aggregator."""

    host = _domain(item)
    return item.collector.casefold() in {"google_news", "gdelt"} or host in {
        "news.google.com",
        "api.gdeltproject.org",
        "gdeltproject.org",
    }


def freshness_reason(
    item: NewsItem,
    config: Mapping[str, Any] | AppConfig,
    now: datetime | None = None,
) -> str:
    """Return a stable exclusion reason, or an empty string when fresh."""

    now = (now or datetime.now(UTC)).astimezone(UTC)
    if item.published_at is None:
        return "missing_date" if config.get("missing_date_policy", "exclude") == "exclude" else ""
    published = _utc_datetime(item.published_at)
    future_skew = timedelta(hours=int(config.get("future_skew_hours", 6)))
    if published > now + future_skew:
        return "future_date"
    cutoff = now - timedelta(days=int(config.get("lookback_days", 7)))
    if published < cutoff:
        return "stale"
    return ""


def filter_fresh(
    items: Iterable[NewsItem],
    config: Mapping[str, Any] | AppConfig,
    now: datetime | None = None,
) -> tuple[list[NewsItem], dict[str, int]]:
    fresh: list[NewsItem] = []
    exclusions: defaultdict[str, int] = defaultdict(int)
    for item in items:
        reason = freshness_reason(item, config, now)
        item.freshness_reason = reason
        if reason:
            exclusions[reason] += 1
        else:
            fresh.append(item)
    return fresh, dict(exclusions)


def rank_item(item: NewsItem, config: Mapping[str, Any] | AppConfig, now: datetime | None = None) -> NewsItem:
    now = now or datetime.now(UTC)
    title = item.title.casefold()
    body = f"{item.title} {item.summary}".casefold()
    score = 0
    matches: list[str] = []
    best_topic: Any | None = None
    best_topic_score = 0

    for topic in config["topics"]:
        topic_score = 0
        topic_matches: list[str] = []
        for term in _topic_value(topic, "strong_terms", []):
            if _contains(title, term):
                topic_score += 6
                topic_matches.append(str(term))
            elif _contains(body, term):
                topic_score += 4
                topic_matches.append(str(term))
        for term in _topic_value(topic, "keywords", []):
            if _contains(title, term):
                topic_score += 3
                topic_matches.append(str(term))
            elif _contains(body, term):
                topic_score += 1
                topic_matches.append(str(term))
        required_context = _topic_value(topic, "requires_any", [])
        if required_context and not any(_contains(body, term) for term in required_context):
            topic_score = 0
            topic_matches = []
        if topic_score > best_topic_score or (
            topic_score == best_topic_score
            and topic_score > 0
            and str(_topic_value(topic, "id", "")) < str(_topic_value(best_topic, "id", "~"))
        ):
            best_topic_score = topic_score
            best_topic = topic
            matches = topic_matches

    score += min(best_topic_score, 18)
    if best_topic:
        item.topic = str(_topic_value(best_topic, "id", ""))
        item.topic_label = str(_topic_value(best_topic, "label", ""))
        item.relevance_reason = str(_topic_value(best_topic, "why", ""))
    else:
        score -= 12

    company_text = str(config["company"]).casefold()
    if _contains(body, company_text) or _contains(body, "meco inoxprima"):
        score += 20
        matches.append("direct company mention")

    geo_hits = [str(term) for term in config.get("indonesia_terms", []) if _contains(body, str(term))]
    if geo_hits:
        score += 3
        matches.append(geo_hits[0])

    signals = [str(term) for term in config.get("business_signals", []) if _contains(body, str(term))]
    if signals:
        score += min(9, 3 * len(signals))
        matches.extend(signals[:2])

    domain = _domain(item)
    if any(domain == trusted or domain.endswith(f".{trusted}") for trusted in config.get("trusted_domains", [])):
        score += 2

    if item.published_at:
        age_hours = max(0.0, (now.astimezone(UTC) - _utc_datetime(item.published_at)).total_seconds() / 3600)
        if age_hours <= 24:
            score += 4
        elif age_hours <= 48:
            score += 3
        elif age_hours <= 96:
            score += 1

    negatives = [str(term) for term in config.get("negative_terms", []) if _contains(body, str(term))]
    score -= 8 * len(negatives)
    if negatives:
        matches.extend(f"excluded:{term}" for term in negatives[:2])

    item.score = score
    item.matches = list(dict.fromkeys(matches))
    return item


def _quality_key(item: NewsItem) -> tuple[Any, ...]:
    published = _utc_datetime(item.published_at).timestamp() if item.published_at else 0
    is_aggregator = int(_is_aggregator(item))
    return (
        is_aggregator,
        -int(item.score),
        -published,
        is_aggregator,
        -len(item.summary),
        normalized_title(item.title, item.source),
        canonical_url(item.url),
        item.source.casefold(),
        item.fingerprint,
    )


def _merge_group(items: list[NewsItem]) -> NewsItem:
    ordered = sorted(items, key=_quality_key)
    # Merging is a pure operation.  In particular, enriching the selected
    # publisher must not alter a caller-owned item that may be reused by a
    # second permutation or a different delivery.
    primary = copy.deepcopy(ordered[0])
    # Enrich without changing the identity or replacing a direct URL with an
    # aggregator URL.  Lists are copied so later ranking cannot mutate a
    # second input object's metadata.
    if len(primary.summary) < max(len(item.summary) for item in ordered):
        primary.summary = max((item.summary for item in ordered), key=len)
    if not primary.source_host:
        primary.source_host = next((item.source_host for item in ordered if item.source_host), "")
    if not primary.published_at:
        primary.published_at = next((item.published_at for item in ordered if item.published_at), None)
    primary.matches = list(dict.fromkeys(match for item in ordered for match in item.matches))[:20]
    if not primary.topic:
        topical = next((item for item in ordered if item.topic), None)
        if topical:
            primary.topic = topical.topic
            primary.topic_label = topical.topic_label
            primary.relevance_reason = topical.relevance_reason
    return primary


def _tokens(item: NewsItem, stop_words: set[str]) -> set[str]:
    return {
        token
        for token in normalized_title(item.title, item.source).split()
        if len(token) > 2 and token not in stop_words and not token.isdigit()
    }


def deduplicate_with_stats(
    items: Iterable[NewsItem],
    config: Mapping[str, Any] | AppConfig | None = None,
) -> tuple[list[NewsItem], DedupStats]:
    """Deduplicate deterministically and return exact bounded-work facts."""

    material = [item for item in items if not item.is_quarantined]
    if not material:
        return [], DedupStats(0, 0, 0, 0, 0, 0, False, 0)
    by_url: dict[str, list[NewsItem]] = defaultdict(list)
    for item in material:
        by_url[canonical_url(item.url)].append(item)
    url_merged = [_merge_group(by_url[key]) for key in sorted(by_url)]

    by_title: dict[str, list[NewsItem]] = defaultdict(list)
    for item in url_merged:
        by_title[normalized_title(item.title, item.source)].append(item)
    exact = [_merge_group(by_title[key]) for key in sorted(by_title)]
    exact.sort(key=_quality_key)

    limits = config.get("limits", {}) if config is not None else {}
    max_candidates = int(limits.get("fuzzy_candidates", 500)) if isinstance(limits, Mapping) else 500
    max_comparisons = int(limits.get("fuzzy_comparisons", 20_000)) if isinstance(limits, Mapping) else 20_000
    max_candidates = max(0, max_candidates)
    max_comparisons = max(0, max_comparisons)
    candidates = exact[:max_candidates]
    stop_words = {
        "dan",
        "di",
        "ke",
        "dari",
        "yang",
        "untuk",
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "ini",
        "itu",
        "usai",
        "pasca",
        "baru",
        "latest",
        "news",
        "berita",
    }
    token_cache = {id(item): _tokens(item, stop_words) for item in candidates}
    title_cache = {id(item): normalized_title(item.title, item.source) for item in candidates}
    clustered: list[NewsItem] = []
    unprocessed: list[NewsItem] = []
    comparisons = 0
    for position, item in enumerate(candidates):
        item_tokens = token_cache[id(item)]
        duplicate = False
        fully_processed = True
        # A small first-token bucket preserves a linear-ish bound and still
        # catches translated headlines that share their core phrase.
        for kept in clustered:
            if comparisons >= max_comparisons:
                fully_processed = False
                break
            # Count the unique candidate pair before any token-overlap
            # shortcut.  This makes the hard budget auditable and prevents a
            # later "exhausted" pass from resurrecting a pair already proven
            # duplicate.
            comparisons += 1
            kept_tokens = token_cache.get(id(kept), _tokens(kept, stop_words))
            if item_tokens and kept_tokens and not (item_tokens & kept_tokens):
                continue
            overlap = len(item_tokens & kept_tokens) / max(1, min(len(item_tokens), len(kept_tokens)))
            sequence = SequenceMatcher(
                None,
                title_cache.get(id(item), normalized_title(item.title, item.source)),
                title_cache.get(id(kept), normalized_title(kept.title, kept.source)),
            ).ratio()
            if overlap >= 0.60 or sequence >= 0.84:
                duplicate = True
                break
        if duplicate:
            continue
        if not fully_processed:
            # This candidate was not proven unique against every retained
            # item.  Keep it as explicitly unprocessed, and keep every later
            # candidate likewise; never call them confirmed duplicates.
            unprocessed.extend(candidates[position:])
            break
        clustered.append(item)
    clustered.extend(unprocessed)
    clustered.extend(exact[max_candidates:])
    result = sorted(clustered, key=_quality_key)
    return result, DedupStats(
        input_count=len(material),
        url_groups=len(by_url),
        exact_title_groups=len(by_title),
        fuzzy_candidates=len(candidates),
        fuzzy_comparisons=comparisons,
        comparison_budget=max_comparisons,
        budget_exhausted=comparisons >= max_comparisons and bool(unprocessed),
        unprocessed_candidates=len(unprocessed),
    )


def deduplicate(
    items: Iterable[NewsItem],
    config: Mapping[str, Any] | AppConfig | None = None,
) -> list[NewsItem]:
    """Compatibility wrapper returning only the deterministic result list."""

    result, _ = deduplicate_with_stats(items, config)
    return result


def select_digest(
    items: Iterable[NewsItem],
    config: Mapping[str, Any] | AppConfig,
    sent_fingerprints: set[str] | None = None,
    *,
    sent_url_keys: set[str] | None = None,
    sent_title_keys: set[str] | None = None,
    now: datetime | None = None,
) -> list[NewsItem]:
    """Select a diverse digest while honoring dual cross-run identity."""

    sent_fingerprints = sent_fingerprints or set()
    sent_url_keys = sent_url_keys or set()
    sent_title_keys = sent_title_keys or set()
    current = now or datetime.now(UTC)
    candidates = []
    for item in items:
        if item.is_quarantined or not item.topic:
            continue
        if freshness_reason(item, config, current):
            continue
        if item.fingerprint in sent_fingerprints or item.url_key in sent_url_keys or item.title_key in sent_title_keys:
            continue
        candidates.append(item)

    candidates.sort(
        key=lambda item: (
            -item.score,
            -(_utc_datetime(item.published_at).timestamp() if item.published_at else 0),
            item.topic,
            _domain(item),
            canonical_url(item.url),
            normalized_title(item.title, item.source),
        )
    )

    daily_min = int(config["daily_min"])
    daily_max = int(config["daily_max"])
    max_per_topic = int(config.get("max_per_topic", 3))
    max_per_domain = int(config.get("max_per_domain", 2))
    minimum_score = int(config.get("minimum_score", 9))
    fallback_score = int(config.get("fallback_score", 5))
    selected: list[NewsItem] = []
    topic_counts: defaultdict[str, int] = defaultdict(int)
    domain_counts: defaultdict[str, int] = defaultdict(int)

    for item in candidates:
        domain = _domain(item)
        if item.score < minimum_score or topic_counts[item.topic] >= max_per_topic or domain_counts[domain] >= max_per_domain:
            continue
        selected.append(item)
        topic_counts[item.topic] += 1
        domain_counts[domain] += 1
        if len(selected) >= daily_max:
            return selected

    if len(selected) < daily_min:
        selected_ids = {item.fingerprint for item in selected}
        for item in candidates:
            if item.fingerprint in selected_ids or item.score < fallback_score:
                continue
            selected.append(item)
            selected_ids.add(item.fingerprint)
            if len(selected) >= daily_min:
                break
    return selected[:daily_max]
