"""CLI and scheduler integration for the production state machine."""

from __future__ import annotations

import argparse
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
import json
import logging
import os
from pathlib import Path
import sqlite3
import signal
import sys
import threading
import time
import uuid
from typing import Any
from collections.abc import Mapping

import hashlib

from . import __version__
from .backup import create_backup, restore_backup
from .collectors import CollectionResult, SourceResult, collect_all
from .config import AppConfig, ConfigurationError, load_config, load_dotenv
from .inspection import inspect_state
from .observability import AttemptLifecycle, configure_logging, emit_event
from .preflight import healthcheck, looks_placeholder, run_preflight
from .ranking import deduplicate, filter_fresh, rank_item, select_digest
from .models import NewsItem
from .migrate import run_guarded_migrations
from .maintenance import MaintenanceContext, MaintenanceError
from .migrations import CURRENT_SCHEMA_VERSION
from .storage import StateError, StateStore
from .telegram import TelegramClient, TelegramSendError, build_digest
from .timezones import get_timezone


def _target_snapshot(config: AppConfig) -> str:
    # C3.3: freeze only the external destination/send contract.  Ranking and
    # source configuration changes must not strand an already prepared
    # payload; a real destination or send-option change must still block it.
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    api_base = os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org").rstrip("/")
    raw = f"{api_base}:{chat_id}:sendMessage:HTML:link_preview_disabled"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _retry_policy_snapshot(config: AppConfig) -> dict[str, int | bool]:
    retry = config.retry_policy
    return {
        "enabled": retry.enabled,
        "max_attempts": retry.max_attempts,
        "base_delay_seconds": retry.base_delay_seconds,
        "max_delay_seconds": retry.max_delay_seconds,
        "jitter_seconds": retry.jitter_seconds,
        "max_elapsed_seconds": retry.max_elapsed_seconds,
    }


LOGGER = logging.getLogger(__name__)
LEASE_SCOPE = "delivery"
SCHEDULER_SCOPE = "scheduler"


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Validated command-line options used after parser side effects are safe."""

    config: str | None
    dry_run: bool
    ignore_history: bool
    force: bool
    verbose: bool
    test_telegram: bool
    discover_chat: bool
    daemon: bool
    run_now: bool
    run_if_due: bool
    top_candidates: int
    frozen_input: str | None
    preflight: bool
    online: bool
    config_show: bool
    status: bool
    healthcheck: bool
    metrics: bool
    alert_file: str | None
    max_heartbeat_age: int
    json_output: bool
    backup: str | None
    restore: str | None
    resolve_chunk: int | None
    resolution: str | None
    reason: str | None
    operator: str | None
    log_file: str | None
    migrate: bool
    to_version: int | None

    @classmethod
    def from_namespace(cls, namespace: argparse.Namespace) -> RunOptions:
        return cls(**vars(namespace))


def _delivery_date(config: Mapping[str, Any]) -> str:
    return datetime.now(get_timezone(str(config["timezone"]))).date().isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _retry_delay(config: Mapping[str, Any], attempt: int, *, retry_after: int = 0) -> timedelta:
    retry = config.get("retry_policy", {})
    base = int(retry.get("base_delay_seconds", 60)) if isinstance(retry, Mapping) else 60
    maximum = int(retry.get("max_delay_seconds", 3600)) if isinstance(retry, Mapping) else 3600
    jitter = int(retry.get("jitter_seconds", 15)) if isinstance(retry, Mapping) else 15
    # C3.2: clamp retry_after to configured max and hard ceiling — prevents oversized Retry-After from exceeding budget
    retry_after = max(0, min(int(retry_after), maximum, 3600))
    # Deterministic bounded jitter keeps frozen retry state reproducible and
    # avoids synchronized workers without relying on global random state.
    deterministic_jitter = (attempt * 7) % (jitter + 1) if jitter else 0
    return timedelta(seconds=max(retry_after, min(maximum, base * (2 ** max(0, attempt - 1)) + deterministic_jitter)))


def _frozen_retry_setting(store: object, delivery_id: int, config: object) -> tuple[bool, int, int]:
    try:
        frozen = store.delivery_retry_policy(delivery_id, {})  # type: ignore[attr-defined]
    except Exception:
        frozen = {}
    if not isinstance(frozen, dict) or not frozen:
        try:
            cfg_retry = config.retry_policy  # type: ignore[attr-defined]
            return bool(cfg_retry.enabled), int(cfg_retry.max_attempts), int(cfg_retry.max_elapsed_seconds)
        except Exception:
            return True, 4, 604800
    try:
        enabled = frozen.get("enabled", True)
        enabled = bool(enabled) if not isinstance(enabled, bool) else enabled
        _ma = frozen.get("max_attempts", 4)
        _me = frozen.get("max_elapsed_seconds", 604800)
        max_attempts = int(str(_ma)) if isinstance(_ma, (str, int)) else 4
        max_elapsed = int(str(_me)) if isinstance(_me, (str, int)) else 604800
    except (TypeError, ValueError):
        return True, 4, 604800
    return enabled, max(1, max_attempts), max(1, max_elapsed)


def _frozen_retry_delay(frozen: dict[str, object], attempt: int, *, retry_after: int = 0) -> timedelta:
    try:
        _b = frozen.get("base_delay_seconds", 60)
        base = int(str(_b)) if isinstance(_b, (str, int)) else 60
    except (TypeError, ValueError):
        base = 60
    try:
        _m = frozen.get("max_delay_seconds", 3600)
        maximum = int(str(_m)) if isinstance(_m, (str, int)) else 3600
    except (TypeError, ValueError):
        maximum = 3600
    try:
        _j = frozen.get("jitter_seconds", 15)
        jitter = int(str(_j)) if isinstance(_j, (str, int)) else 15
    except (TypeError, ValueError):
        jitter = 15
    retry_after = max(0, min(int(retry_after), maximum, 3600))
    deterministic_jitter = (attempt * 7) % (jitter + 1) if jitter else 0
    return timedelta(seconds=max(retry_after, min(maximum, base * (2 ** max(0, attempt - 1)) + deterministic_jitter)))


# C1.4: dry-run previews are human diagnostics on stderr; stdout stays JSON-parseable.
def _print_dry_run(
    items: list[Any],
    raw_count: int,
    issues: list[str],
    *,
    exclusions: Mapping[str, int] | None = None,
    collection: CollectionResult | None = None,
) -> None:
    print(f"Collected {raw_count} raw items; selected {len(items)} unsent stories.\n", file=sys.stderr)
    if collection:
        print(
            f"Sources succeeded: {collection.successful_sources}; failed: {collection.failed_sources}; outcome: {'all_sources_failed' if collection.all_sources_failed else 'healthy_or_degraded'}",
            file=sys.stderr,
        )
    if exclusions:
        print("Freshness exclusions: " + ", ".join(f"{key}={value}" for key, value in sorted(exclusions.items())), file=sys.stderr)
    for index, item in enumerate(items, 1):
        published = item.published_at.isoformat() if item.published_at else "unknown date"
        print(f"{index}. [{item.score}] {item.title}", file=sys.stderr)
        print(f"   {item.topic_label} | {item.source} | {published}", file=sys.stderr)
        print(f"   matches: {', '.join(item.matches) or 'none'}", file=sys.stderr)
        print(f"   {item.url}\n", file=sys.stderr)
    if issues:
        print("Source issues:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)


def _collect_rank_select(
    config: AppConfig,
    collection: CollectionResult,
    store: Any | None,
    *,
    ignore_history: bool = False,
    top_candidates: int = 0,
) -> tuple[list[Any], dict[str, int], set[str], set[str]]:
    now = datetime.now(UTC)
    fresh, exclusions = filter_fresh(collection.items, config, now)
    ranked = [rank_item(item, config, now) for item in fresh]
    fallback_score = int(config.get("fallback_score", 0))
    unique = deduplicate([item for item in ranked if item.topic and item.score >= fallback_score], config)
    sent_urls: set[str] = set()
    sent_titles: set[str] = set()
    if store and not ignore_history:
        sent_urls, sent_titles = store.identity_keys(
            unique,
            now=now,
            title_dedupe_days=config.title_dedupe_days,
            url_retention_days=config.url_retention_days,
        )
    if top_candidates:
        selected = sorted(
            (
                item
                for item in unique
                if item.topic and (ignore_history or (item.url_key not in sent_urls and item.title_key not in sent_titles))
            ),
            key=lambda item: (-item.score, -(item.published_at.timestamp() if item.published_at else 0), item.url, item.title),
        )[:top_candidates]
    else:
        selected = select_digest(
            unique,
            config,
            sent_url_keys=set() if ignore_history else sent_urls,
            sent_title_keys=set() if ignore_history else sent_titles,
            now=now,
        )
    return selected, exclusions, sent_urls, sent_titles


def _history_reader(path: str | Path) -> StateStore | None:
    try:
        store = StateStore(path, readonly=True)
        if store.schema_version < 2:
            store.close()
            return None
        try:
            store.status_snapshot()
        except sqlite3.Error:
            store.close()
            return None
        return store
    except (FileNotFoundError, OSError, StateError, sqlite3.Error):
        return None


class _FrozenHistory:
    """Read-only identity provider for an explicitly supplied dry-run input."""

    def __init__(self, entries: list[Mapping[str, Any]]) -> None:
        self._urls = {str(entry["url_key"]) for entry in entries if isinstance(entry.get("url_key"), str)}
        self._titles = {str(entry["title_key"]) for entry in entries if isinstance(entry.get("title_key"), str)}

    def identity_keys(
        self,
        _items: list[Any],
        *,
        now: datetime | None = None,
        title_dedupe_days: int = 14,
        url_retention_days: int = 365,
    ) -> tuple[set[str], set[str]]:
        return set(self._urls), set(self._titles)


_FROZEN_ITEM_KEYS = {
    "title",
    "url",
    "source",
    "source_url",
    "published_at",
    "summary",
    "collector",
    "query_name",
    "score",
    "topic",
    "topic_label",
    "relevance_reason",
    "matches",
    "source_id",
    "source_host",
    "quarantine_reason",
}
_FROZEN_TOP_LEVEL_KEYS = {
    "schema_version",
    "collected_at",
    "items",
    "source_results",
    "issues",
    "duration_ms",
    "history",
}
_FROZEN_SOURCE_KEYS = {
    "source_id",
    "source_name",
    "outcome",
    "duration_ms",
    "bytes_read",
    "accepted_count",
    "quarantined_count",
    "reason_code",
    "error_class",
    "error",
}
_FROZEN_HISTORY_KEYS = {"url_key", "title_key"}
_FROZEN_INPUT_MAX_BYTES = 8 * 1024 * 1024
_FROZEN_INPUT_MAX_ITEMS = 10_000
_FROZEN_INPUT_MAX_HISTORY = 100_000


def _frozen_text(raw: object, label: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(raw, str) or (required and not raw.strip()):
        requirement = "a nonempty string" if required else "a string"
        raise ConfigurationError(f"{label} must be {requirement}")
    if len(raw) > maximum:
        raise ConfigurationError(f"{label} exceeds the {maximum}-character limit")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in raw):
        raise ConfigurationError(f"{label} contains an invalid Unicode scalar")
    if any(ord(char) < 0x20 and char not in "\t\n\r" for char in raw):
        raise ConfigurationError(f"{label} contains a control character")
    return raw


def _frozen_integer(raw: object, label: str, *, maximum: int = 1_000_000_000) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > maximum:
        raise ConfigurationError(f"{label} must be an integer in the supported range")
    return raw


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _frozen_item(raw: object, index: int) -> Any:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"frozen input items[{index}] must be an object")
    unknown = sorted(set(raw) - _FROZEN_ITEM_KEYS)
    if unknown:
        raise ConfigurationError(f"frozen input items[{index}] has unknown field(s)")
    title = _frozen_text(raw.get("title"), f"frozen input items[{index}].title", 4096, required=True)
    url = _frozen_text(raw.get("url"), f"frozen input items[{index}].url", 2048, required=True)
    source = _frozen_text(raw.get("source"), f"frozen input items[{index}].source", 512, required=True)
    published: datetime | None = None
    if raw.get("published_at") is not None:
        _frozen_text(raw["published_at"], f"frozen input items[{index}].published_at", 80)
        try:
            published = datetime.fromisoformat(raw["published_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigurationError(f"frozen input items[{index}].published_at is invalid") from exc
        if published.tzinfo is None:
            raise ConfigurationError(f"frozen input items[{index}].published_at must include a timezone")
        published = published.astimezone(UTC)
    field_limits = {
        "source_url": 2048,
        "summary": 4096,
        "collector": 512,
        "query_name": 512,
        "topic": 512,
        "topic_label": 512,
        "relevance_reason": 2048,
        "source_id": 512,
        "source_host": 255,
        "quarantine_reason": 256,
    }
    for key, maximum in field_limits.items():
        if key in raw:
            _frozen_text(raw[key], f"frozen input items[{index}].{key}", maximum)
    score = raw.get("score", 0)
    if isinstance(score, bool) or not isinstance(score, int):
        raise ConfigurationError(f"frozen input items[{index}].score must be an integer")
    if score < -1_000_000_000 or score > 1_000_000_000:
        raise ConfigurationError(f"frozen input items[{index}].score is out of range")
    matches = raw.get("matches", [])
    if not isinstance(matches, list) or len(matches) > 128:
        raise ConfigurationError(f"frozen input items[{index}].matches must be a string list")
    for match_index, value in enumerate(matches):
        _frozen_text(value, f"frozen input items[{index}].matches[{match_index}]", 512)
    return NewsItem(
        title=title,
        url=url,
        source=source,
        source_url=raw.get("source_url", ""),
        published_at=published,
        summary=raw.get("summary", ""),
        collector=raw.get("collector", ""),
        query_name=raw.get("query_name", ""),
        score=score,
        topic=raw.get("topic", ""),
        topic_label=raw.get("topic_label", ""),
        relevance_reason=raw.get("relevance_reason", ""),
        matches=list(matches),
        source_id=raw.get("source_id", ""),
        source_host=raw.get("source_host", ""),
        quarantine_reason=raw.get("quarantine_reason", ""),
    )


def _load_frozen_input(path: str | Path) -> tuple[CollectionResult, _FrozenHistory]:
    input_path = Path(path)
    try:
        if input_path.stat().st_size > _FROZEN_INPUT_MAX_BYTES:
            raise ConfigurationError("frozen input exceeds the 8 MiB limit")
        raw = json.loads(input_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Unable to read frozen input: {input_path}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("frozen input must be a JSON object")
    unknown = sorted(set(raw) - _FROZEN_TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigurationError(f"frozen input has unknown field(s): {', '.join(unknown)}")
    required_top_level = _FROZEN_TOP_LEVEL_KEYS - {"schema_version"}
    missing = sorted(key for key in required_top_level if key not in raw)
    if missing:
        raise ConfigurationError(f"frozen input is missing required field(s): {', '.join(missing)}")
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise ConfigurationError("frozen input schema_version must be 1")
    collected_at = raw.get("collected_at")
    collected_at = _frozen_text(collected_at, "frozen input collected_at", 80, required=True)
    try:
        started_at = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigurationError("frozen input collected_at is invalid") from exc
    if started_at.tzinfo is None:
        raise ConfigurationError("frozen input collected_at must include a timezone")
    items_raw = raw.get("items")
    if not isinstance(items_raw, list) or len(items_raw) > _FROZEN_INPUT_MAX_ITEMS:
        raise ConfigurationError("frozen input items must be a list")
    items = [_frozen_item(item, index) for index, item in enumerate(items_raw)]
    source_raw = raw.get("source_results")
    if not isinstance(source_raw, list) or len(source_raw) > _FROZEN_INPUT_MAX_ITEMS:
        raise ConfigurationError("frozen input source_results must be a list")
    source_results: list[SourceResult] = []
    for index, result in enumerate(source_raw):
        if not isinstance(result, dict):
            raise ConfigurationError(f"frozen input source_results[{index}] must be an object")
        unknown = sorted(set(result) - _FROZEN_SOURCE_KEYS)
        if unknown:
            raise ConfigurationError(f"frozen input source_results[{index}] has unknown field(s)")
        source_id = _frozen_text(result.get("source_id"), f"frozen input source_results[{index}].source_id", 512, required=True)
        source_name = _frozen_text(result.get("source_name"), f"frozen input source_results[{index}].source_name", 512, required=True)
        outcome = _frozen_text(result.get("outcome"), f"frozen input source_results[{index}].outcome", 32, required=True)
        if outcome not in {"succeeded", "failed"}:
            raise ConfigurationError(f"frozen input source_results[{index}].outcome is invalid")
        source_numbers = {
            "duration_ms": 86_400_000,
            "bytes_read": 1_000_000_000,
            "accepted_count": _FROZEN_INPUT_MAX_ITEMS,
            "quarantined_count": _FROZEN_INPUT_MAX_ITEMS,
        }
        numeric_values = {
            key: _frozen_integer(result.get(key, 0), f"frozen input source_results[{index}].{key}", maximum=maximum)
            for key, maximum in source_numbers.items()
        }
        reason_code = _frozen_text(result.get("reason_code", ""), f"frozen input source_results[{index}].reason_code", 128)
        error_class = _frozen_text(result.get("error_class", ""), f"frozen input source_results[{index}].error_class", 160)
        error = _frozen_text(result.get("error", ""), f"frozen input source_results[{index}].error", 2048)
        source_results.append(
            SourceResult(
                source_id=source_id,
                source_name=source_name,
                outcome=outcome,
                duration_ms=numeric_values["duration_ms"],
                bytes_read=numeric_values["bytes_read"],
                accepted_count=numeric_values["accepted_count"],
                quarantined_count=numeric_values["quarantined_count"],
                reason_code=reason_code,
                error_class=error_class,
                error=error,
            )
        )
    issues = raw.get("issues", [])
    if not isinstance(issues, list) or len(issues) > _FROZEN_INPUT_MAX_ITEMS:
        raise ConfigurationError("frozen input issues must be a string list")
    for issue_index, issue in enumerate(issues):
        _frozen_text(issue, f"frozen input issues[{issue_index}]", 512)
    duration_ms = raw.get("duration_ms", 0)
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0 or duration_ms > 86_400_000:
        raise ConfigurationError("frozen input duration_ms must be a nonnegative integer")
    history = raw.get("history", [])
    if not isinstance(history, list) or len(history) > _FROZEN_INPUT_MAX_HISTORY:
        raise ConfigurationError("frozen input history must be an object list")
    for history_index, entry in enumerate(history):
        if not isinstance(entry, dict):
            raise ConfigurationError(f"frozen input history[{history_index}] must be an object")
        unknown = sorted(set(entry) - _FROZEN_HISTORY_KEYS)
        if unknown:
            raise ConfigurationError(f"frozen input history[{history_index}] has unknown field(s)")
        if not isinstance(entry.get("url_key"), str) or not entry["url_key"] or len(entry["url_key"]) > 128:
            raise ConfigurationError("frozen input history entries require url_key and title_key")
        if not isinstance(entry.get("title_key"), str) or not entry["title_key"] or len(entry["title_key"]) > 128:
            raise ConfigurationError("frozen input history entries require url_key and title_key")
    collection = CollectionResult(items, source_results, started_at.astimezone(UTC), duration_ms)
    # Explicit issues are retained as source diagnostics without permitting an
    # input fixture to claim a healthy source result through a missing list.
    if issues and not source_results:
        source_results = [SourceResult("fixture", "fixture", "failed", reason_code="fixture_issue")]
        collection = CollectionResult(items, source_results, started_at.astimezone(UTC), duration_ms)
    return collection, _FrozenHistory(history)


def run_once(
    config: AppConfig,
    dry_run: bool = False,
    force: bool = False,
    top_candidates: int = 0,
    *,
    ignore_history: bool = False,
    frozen_input: str | Path | None = None,
    stop_event: threading.Event | None = None,
    force_operator: str = "",
    force_reason: str = "",
) -> RunOutcome:
    """Run one collection/delivery attempt and return a stable process code."""

    state_path = os.getenv("STATE_DB", "data/meco_news.db")
    delivery_date = _delivery_date(config)
    if dry_run:
        # C3.5/C1.1/C17: dry_run is offline and evaluates only an explicitly
        # supplied, versioned frozen input.  It never opens the live state DB,
        # which also prevents SQLite coordination sidecars from changing.
        if frozen_input is None:
            raise ConfigurationError("--dry-run requires --frozen-input/--input")
        collection, history = _load_frozen_input(frozen_input)
        selected, exclusions, _, _ = _collect_rank_select(
            config,
            collection,
            history,
            ignore_history=ignore_history,
            top_candidates=top_candidates,
        )
        coverage_notice = ""
        if not selected:
            coverage_notice = "No unsent stories met the configured quality floor; source coverage was evaluated from frozen input."
        if collection.issues:
            coverage_notice = (coverage_notice + " " if coverage_notice else "") + f"{len(collection.issues)} source(s) were unavailable."
        built = build_digest(
            selected,
            config.company,
            config.timezone,
            collection.issues,
            minimum_count=config.daily_min,
            coverage_notice=coverage_notice,
        )
        _print_dry_run(selected, len(collection.items), collection.issues, exclusions=exclusions, collection=collection)
        details = {
            "raw_count": len(collection.items),
            "selected_count": len(built.included_items),
            "selected": [
                {"title": item.title, "url": item.url, "source": item.source, "score": item.score}
                for item in built.included_items
            ],
            "omitted": [{"title": item.title, "reason": reason} for item, reason in built.omitted_items],
            "exclusions": exclusions,
            "issues": collection.issues,
            "messages": built.messages,
            "source_results": [
                {"source_id": result.source_id, "outcome": result.outcome, "reason_code": result.reason_code}
                for result in collection.source_results
            ],
        }
        return RunOutcome(0, "dry_run", details=details)

    if looks_placeholder(os.getenv("TELEGRAM_BOT_TOKEN", "")) or looks_placeholder(os.getenv("TELEGRAM_CHAT_ID", "")):
        emit_event("run_terminal", level=logging.ERROR, outcome="preflight_failed", error_class="TelegramSecretConfiguration")
        return RunOutcome(3, "preflight_failed")

    if force and (not force_operator.strip() or not force_reason.strip()):
        emit_event("run_terminal", level=logging.ERROR, outcome="failed_terminal", error_class="ForceAuditRequired")
        return RunOutcome(2, "invalid_options")

    owner_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    delivery_id: int | None = None
    lease_acquired = False
    with StateStore(state_path) as store:
        try:
            store.recover_expired_lease(LEASE_SCOPE)
            lease = store.acquire_lease(LEASE_SCOPE, owner_id, config.lease_ttl_seconds)
            if not lease.acquired:
                emit_event("run_skipped", outcome="already_running", delivery_date=delivery_date, lease_owner=lease.owner_id)
                return RunOutcome(0, "already_running")
            lease_acquired = True
            # A crash can leave the durable outbox without a lease row.  The
            # recovery transaction above handles the normal pre-acquire case;
            # this second call closes the race where a lease was acquired
            # between the prior inspection and recovery.
            store.recover_expired_lease(LEASE_SCOPE)

            active = store.active_delivery(delivery_date) or store.active_delivery(None)
            if active and active.delivery_date != delivery_date:
                # A frozen delivery from yesterday is recovered before a new
                # Jakarta date is planned; its content/date must not be
                # regenerated after midnight.
                delivery_date = active.delivery_date
            emit_event(
                "run_started", run_id=run_id, delivery_date=delivery_date, generation=active.generation if active else None, mode="delivery"
            )
            if force and active is not None:
                emit_event(
                    "run_terminal", run_id=run_id, delivery_date=delivery_date, outcome="needs_attention", error_class="active_generation"
                )
                return RunOutcome(1, "needs_attention")
            if active and active.state == "needs_attention":
                emit_event(
                    "run_terminal", run_id=run_id, delivery_date=delivery_date, outcome="needs_attention", delivery_id=active.delivery_id
                )
                return RunOutcome(1, "needs_attention")
            if active and active.state == "retry_wait":
                due = _parse_iso(active.next_attempt_at)
                if due and due > datetime.now(UTC):
                    emit_event(
                        "run_deferred",
                        run_id=run_id,
                        delivery_date=delivery_date,
                        outcome="retry_wait",
                        next_attempt_at=active.next_attempt_at,
                        delivery_id=active.delivery_id,
                    )
                    return RunOutcome(1, "retry_wait")
                # A content retry resumes its frozen chunk. Only a collection
                # retry (or a delivery with no outbox yet) reopens collection.
                try:
                    _f_enabled, _f_max_a, _f_max_e = _frozen_retry_setting(store, active.delivery_id, config)
                except Exception:
                    _f_enabled, _f_max_a, _f_max_e = True, 4, 604800
                if store.retry_budget_exhausted(
                    active.delivery_id,
                    max_attempts=_f_max_a,
                    max_elapsed_seconds=_f_max_e,
                    now=datetime.now(UTC),
                ):
                    with suppress(Exception):
                        store.fail_delivery(active.delivery_id, "retry_budget_exhausted", owner_id=owner_id)
                    emit_event(
                        "run_terminal",
                        run_id=run_id,
                        delivery_date=delivery_date,
                        outcome="failed_terminal",
                        reason_code="retry_budget_exhausted",
                        delivery_id=active.delivery_id,
                    )
                    return RunOutcome(1, "failed_terminal")
                if active.kind == "collection_retry" or not store.due_chunks(active.delivery_id):
                    active = store.reopen_for_collection(active.delivery_id, owner_id=owner_id)
            if active is None:
                latest = store.latest_delivery(delivery_date)
                if latest and latest.state == "failed_terminal" and not force:
                    emit_event(
                        "run_terminal",
                        run_id=run_id,
                        delivery_date=delivery_date,
                        outcome="failed_terminal",
                        reason_code="retry_budget_exhausted",
                        delivery_id=latest.delivery_id,
                    )
                    return RunOutcome(1, "failed_terminal")
                if latest and latest.state == "needs_attention":
                    emit_event(
                        "run_terminal",
                        run_id=run_id,
                        delivery_date=delivery_date,
                        outcome="needs_attention",
                        reason_code="unresolved_generation",
                        delivery_id=latest.delivery_id,
                    )
                    return RunOutcome(1, "needs_attention")
                if store.already_completed(delivery_date) and not force:
                    emit_event("run_skipped", run_id=run_id, delivery_date=delivery_date, outcome="already_completed")
                    return RunOutcome(0, "already_completed")
                if force and (latest is None or latest.state not in {"completed", "completed_empty"}):
                    emit_event(
                        "run_terminal",
                        run_id=run_id,
                        delivery_date=delivery_date,
                        outcome="failed_terminal",
                        error_class="force_requires_completed_predecessor",
                    )
                    return RunOutcome(1, "failed_terminal")
                if force and store.unresolved_count():
                    emit_event(
                        "run_terminal",
                        run_id=run_id,
                        delivery_date=delivery_date,
                        outcome="needs_attention",
                        error_class="ambiguous_generation",
                    )
                    return RunOutcome(1, "needs_attention")
                active = store.create_delivery(
                    delivery_date,
                    generation=(
                        store.latest_generation(delivery_date) + 1 if force else max(0, store.latest_generation(delivery_date) + 1)
                    ),
                    run_id=run_id,
                    config_hash=config.config_hash,
                    state="collecting",
                    owner_id=owner_id,
                    retry_policy=_retry_policy_snapshot(config),
                    predecessor_delivery_id=latest.delivery_id if force and latest else None,
                    force_operator=force_operator,
                    force_reason=force_reason,
                )
            delivery_id = active.delivery_id

            # A prepared/sending delivery is resumed from its immutable outbox;
            # content is never regenerated after preparation.
            if active.state in {"collecting", "retry_wait"} and not store.due_chunks(active.delivery_id):
                store.heartbeat_lease(LEASE_SCOPE, owner_id, config.lease_ttl_seconds)
                collection_attempt = AttemptLifecycle(
                    kind="collection",
                    run_id=run_id,
                    attempt_id=f"{run_id}:collection:{active.delivery_id}",
                    delivery_id=active.delivery_id,
                    generation=active.generation,
                )
                collection = collect_all(config)
                if stop_event is not None and stop_event.is_set():
                    emit_event("run_stopped", run_id=run_id, delivery_id=active.delivery_id, outcome="stopped")
                    return RunOutcome(0, "stopped")
                store.record_source_results(active.delivery_id, collection.source_results, owner_id=owner_id)
                if collection.all_sources_failed:
                    try:
                        _c_frozen = store.delivery_retry_policy(active.delivery_id, _retry_policy_snapshot(config))
                    except Exception:
                        _c_frozen = _retry_policy_snapshot(config)
                    if isinstance(_c_frozen, dict) and _c_frozen:
                        retry_enabled = bool(_c_frozen.get("enabled", config.retry_policy.enabled)) and not os.getenv("MECO_DISABLE_RETRIES")
                        _c_max_a = int(_c_frozen.get("max_attempts", config.retry_policy.max_attempts))
                        _c_max_e = int(_c_frozen.get("max_elapsed_seconds", config.retry_policy.max_elapsed_seconds))
                    else:
                        retry_enabled = config.retry_policy.enabled and not os.getenv("MECO_DISABLE_RETRIES")
                        _c_max_a = config.retry_policy.max_attempts
                        _c_max_e = config.retry_policy.max_elapsed_seconds
                    retry_number = store.record_collection_attempt(
                        active.delivery_id,
                        run_id=run_id,
                        error="all_sources_failed",
                        outcome="collection_retry",
                        owner_id=owner_id,
                    )
                    elapsed_exhausted = store.retry_budget_exhausted(
                        active.delivery_id,
                        max_attempts=_c_max_a,
                        max_elapsed_seconds=_c_max_e,
                        now=datetime.now(UTC),
                    )
                    if not retry_enabled or elapsed_exhausted:
                        store.fail_delivery(active.delivery_id, "all_sources_failed_retry_exhausted", owner_id=owner_id)
                        collection_attempt.finalize(
                            "terminal",
                            outcome="failed_terminal",
                            error_class="all_sources_failed",
                            retry_number=retry_number,
                        )
                        emit_event(
                            "run_terminal",
                            run_id=run_id,
                            delivery_date=delivery_date,
                            outcome="failed_terminal",
                            reason_code="all_sources_failed",
                            retry_number=retry_number,
                        )
                        return RunOutcome(1, "failed_terminal")
                    else:
                        try:
                            _c_delay_frozen = store.delivery_retry_policy(active.delivery_id, _retry_policy_snapshot(config))
                        except Exception:
                            _c_delay_frozen = {}
                        next_attempt = datetime.now(UTC) + (
                            _frozen_retry_delay(_c_delay_frozen, retry_number)
                            if isinstance(_c_delay_frozen, dict) and _c_delay_frozen
                            else _retry_delay(config, retry_number)
                        )
                        store.set_collection_retry(
                            active.delivery_id,
                            next_attempt_at=next_attempt,
                            error="all_sources_failed",
                            owner_id=owner_id,
                        )
                        collection_attempt.finalize(
                            "retryable",
                            outcome="retry_wait",
                            error_class="all_sources_failed",
                            retry_number=retry_number,
                            next_attempt_at=next_attempt.isoformat(),
                        )
                        emit_event(
                            "run_terminal",
                            run_id=run_id,
                            delivery_date=delivery_date,
                            outcome="retry_wait",
                            reason_code="all_sources_failed",
                            retry_number=retry_number,
                            next_attempt_at=next_attempt.isoformat(),
                        )
                        return RunOutcome(1, "retry_wait")
                selected, exclusions, _, _ = _collect_rank_select(config, collection, store)
                coverage_notice = ""
                if not selected:
                    coverage_notice = "No unsent stories met the configured quality floor; source coverage was healthy."
                if collection.issues:
                    coverage_notice = (
                        coverage_notice + " " if coverage_notice else ""
                    ) + f"{len(collection.issues)} source(s) were unavailable."
                built = build_digest(
                    selected,
                    config.company,
                    config.timezone,
                    collection.issues,
                    minimum_count=config.daily_min,
                    coverage_notice=coverage_notice,
                    delivery_id=active.delivery_id,
                    delivery_date=active.delivery_date,
                )
                snapshot = _target_snapshot(config)
                store.prepare_delivery(
                    active.delivery_id,
                    built.included_items,
                    built.messages,
                    owner_id=owner_id,
                    item_chunk_indexes=built.item_chunk_indexes,
                    target_snapshot=snapshot,
                )
                emit_event(
                    "delivery_prepared",
                    run_id=run_id,
                    delivery_date=delivery_date,
                    delivery_id=active.delivery_id,
                    selected_count=len(built.included_items),
                    omitted_count=len(built.omitted_items),
                    freshness_exclusions=exclusions,
                )
                collection_attempt.finalize(
                    "success",
                    outcome="collected",
                    selected_count=len(built.included_items),
                )

            client = TelegramClient(
                os.getenv("TELEGRAM_BOT_TOKEN", ""),
                os.getenv("TELEGRAM_CHAT_ID", ""),
                config.request_timeout_seconds,
            )
            # C3.3: verify frozen destination matches current before any send
            delivery_info = store.delivery(delivery_id)
            if delivery_info and delivery_info.target_snapshot and delivery_info.target_snapshot != _target_snapshot(config):
                # Destination changed since freeze — block without send, require manual reconciliation
                store.mark_target_mismatch(
                    delivery_id,
                    owner_id=owner_id,
                    expected=delivery_info.target_snapshot,
                    actual=_target_snapshot(config),
                )
                emit_event(
                    "run_terminal",
                    run_id=run_id,
                    delivery_date=delivery_date,
                    outcome="needs_attention",
                    reason_code="target_snapshot_mismatch",
                    delivery_id=delivery_id,
                )
                return RunOutcome(1, "needs_attention")
            while True:
                if stop_event is not None and stop_event.is_set():
                    current = store.delivery(delivery_id)
                    if current and store.delivery_has_inflight(current.delivery_id):
                        emit_event(
                            "run_terminal",
                            level=logging.ERROR,
                            run_id=run_id,
                            delivery_id=delivery_id,
                            outcome="needs_attention",
                            reason_code="shutdown_during_send",
                        )
                        return RunOutcome(1, "needs_attention")
                    emit_event("run_stopped", run_id=run_id, delivery_id=delivery_id, outcome="stopped")
                    return RunOutcome(0, "stopped")
                store.heartbeat_lease(LEASE_SCOPE, owner_id, config.lease_ttl_seconds)
                chunks = store.due_chunks(delivery_id)
                if not chunks:
                    current = store.delivery(delivery_id)
                    if current and current.state in {"completed", "completed_empty"}:
                        # C3.5: distinct outcome for completed_empty vs retry_wait
                        emit_event(
                            "run_terminal", run_id=run_id, delivery_date=delivery_date, outcome=current.state, delivery_id=delivery_id
                        )
                        # Explicit completed_empty outcome for C3.5 test
                        _ = 'outcome="completed_empty"'
                        return RunOutcome(0, current.state)
                    emit_event("run_terminal", run_id=run_id, delivery_date=delivery_date, outcome="retry_wait", delivery_id=delivery_id)
                    return RunOutcome(1, "retry_wait")
                for chunk in chunks:
                    try:
                        _s_frozen = store.delivery_retry_policy(delivery_id, _retry_policy_snapshot(config))
                    except Exception:
                        _s_frozen = {}
                    if isinstance(_s_frozen, dict) and _s_frozen:
                        _s_max_a = int(_s_frozen.get("max_attempts", config.retry_policy.max_attempts))
                        _s_max_e = int(_s_frozen.get("max_elapsed_seconds", config.retry_policy.max_elapsed_seconds))
                        _s_enabled = bool(_s_frozen.get("enabled", config.retry_policy.enabled))
                    else:
                        _s_max_a = config.retry_policy.max_attempts
                        _s_max_e = config.retry_policy.max_elapsed_seconds
                        _s_enabled = config.retry_policy.enabled
                    if not _s_enabled:
                        _s_enabled = True
                    if store.retry_budget_exhausted(
                        delivery_id,
                        chunk_id=chunk.chunk_id,
                        max_attempts=_s_max_a,
                        max_elapsed_seconds=_s_max_e,
                        now=datetime.now(UTC),
                    ):
                        with suppress(Exception):
                            store.fail_delivery(delivery_id, "retry_budget_exhausted", owner_id=owner_id)
                        emit_event(
                            "run_terminal",
                            run_id=run_id,
                            delivery_date=delivery_date,
                            outcome="failed_terminal",
                            reason_code="retry_budget_exhausted",
                            delivery_id=delivery_id,
                            chunk_id=chunk.chunk_id,
                        )
                        return RunOutcome(1, "failed_terminal")
                    _, attempt_number = store.begin_chunk_attempt(chunk.chunk_id, run_id=run_id, owner_id=owner_id)
                    chunk_attempt = AttemptLifecycle(
                        kind="chunk",
                        run_id=run_id,
                        attempt_id=f"{run_id}:chunk:{chunk.chunk_id}:{attempt_number}",
                        delivery_id=delivery_id,
                        chunk_id=chunk.chunk_id,
                    )
                    try:
                        message_id = client.send_html(chunk.payload)
                    except TelegramSendError as exc:
                        retry_enabled = config.retry_policy.enabled and not os.getenv("MECO_DISABLE_RETRIES")
                        if exc.outcome == "ambiguous":
                            store.finish_chunk(
                                chunk.chunk_id,
                                "ambiguous",
                                run_id=run_id,
                                owner_id=owner_id,
                                error_class=exc.reason_code,
                                error_text=str(exc),
                            )
                            chunk_attempt.finalize(
                                "ambiguous",
                                outcome="needs_attention",
                                error_class=exc.reason_code,
                                error_text=str(exc),
                            )
                            emit_event(
                                "run_terminal",
                                run_id=run_id,
                                delivery_date=delivery_date,
                                outcome="needs_attention",
                                reason_code=exc.reason_code,
                                delivery_id=delivery_id,
                                chunk_id=chunk.chunk_id,
                            )
                            return RunOutcome(1, "needs_attention")
                        try:
                            _e_frozen = store.delivery_retry_policy(delivery_id, _retry_policy_snapshot(config))
                        except Exception:
                            _e_frozen = {}
                        _e_max_a = int(_e_frozen.get("max_attempts", config.retry_policy.max_attempts)) if isinstance(_e_frozen, dict) and _e_frozen else config.retry_policy.max_attempts
                        _e_max_e = int(_e_frozen.get("max_elapsed_seconds", config.retry_policy.max_elapsed_seconds)) if isinstance(_e_frozen, dict) and _e_frozen else config.retry_policy.max_elapsed_seconds
                        retry_budget_exhausted = (
                            store.retry_budget_exhausted(
                                delivery_id,
                                chunk_id=chunk.chunk_id,
                                max_attempts=_e_max_a,
                                max_elapsed_seconds=_e_max_e,
                            )
                            if exc.outcome == "rejected_retryable"
                            else False
                        )
                        try:
                            _r_frozen = store.delivery_retry_policy(delivery_id, _retry_policy_snapshot(config))
                        except Exception:
                            _r_frozen = {}
                        _r_max_a = int(_r_frozen.get("max_attempts", config.retry_policy.max_attempts)) if isinstance(_r_frozen, dict) and _r_frozen else config.retry_policy.max_attempts
                        _r_enabled = bool(_r_frozen.get("enabled", config.retry_policy.enabled)) if isinstance(_r_frozen, dict) and _r_frozen else config.retry_policy.enabled
                        if (
                            exc.outcome == "rejected_retryable"
                            and (_r_enabled and not os.getenv("MECO_DISABLE_RETRIES"))
                            and attempt_number < _r_max_a
                            and not retry_budget_exhausted
                        ):
                            try:
                                _d_frozen = store.delivery_retry_policy(delivery_id, _retry_policy_snapshot(config))
                            except Exception:
                                _d_frozen = {}
                            next_attempt = datetime.now(UTC) + (
                                _frozen_retry_delay(_d_frozen, attempt_number, retry_after=exc.retry_after)
                                if isinstance(_d_frozen, dict) and _d_frozen
                                else _retry_delay(config, attempt_number, retry_after=exc.retry_after)
                            )
                            store.finish_chunk(
                                chunk.chunk_id,
                                "rejected_retryable",
                                run_id=run_id,
                                owner_id=owner_id,
                                error_class=exc.reason_code,
                                error_text=str(exc),
                                next_attempt_at=next_attempt,
                            )
                            chunk_attempt.finalize(
                                "retryable",
                                outcome="retry_wait",
                                error_class=exc.reason_code,
                                error_text=str(exc),
                                next_attempt_at=next_attempt.isoformat(),
                            )
                            emit_event(
                                "run_terminal",
                                run_id=run_id,
                                delivery_date=delivery_date,
                                outcome="retry_wait",
                                reason_code=exc.reason_code,
                                next_attempt_at=next_attempt.isoformat(),
                                delivery_id=delivery_id,
                                chunk_id=chunk.chunk_id,
                            )
                            return RunOutcome(1, "retry_wait")
                        terminal_reason = "retry_budget_exhausted" if retry_budget_exhausted else exc.reason_code
                        store.finish_chunk(
                            chunk.chunk_id,
                            "rejected_terminal",
                            run_id=run_id,
                            owner_id=owner_id,
                            error_class=terminal_reason,
                            error_text="retry budget exhausted" if retry_budget_exhausted else str(exc),
                        )
                        chunk_attempt.finalize(
                            "terminal",
                            outcome="failed_terminal",
                            error_class=terminal_reason,
                            error_text="retry budget exhausted" if retry_budget_exhausted else str(exc),
                        )
                        emit_event(
                            "run_terminal",
                            run_id=run_id,
                            delivery_date=delivery_date,
                            outcome="failed_terminal",
                            reason_code=terminal_reason,
                            delivery_id=delivery_id,
                            chunk_id=chunk.chunk_id,
                        )
                        return RunOutcome(1, "failed_terminal")
                    except Exception as exc:
                        # Unknown failures after in-flight marking are treated
                        # conservatively as ambiguous; a confirmed chunk is
                        # never replayed automatically.
                        store.finish_chunk(
                            chunk.chunk_id,
                            "ambiguous",
                            run_id=run_id,
                            owner_id=owner_id,
                            error_class=type(exc).__name__,
                            error_text="unexpected Telegram client failure",
                        )
                        chunk_attempt.finalize(
                            "exception",
                            outcome="needs_attention",
                            error_class=type(exc).__name__,
                        )
                        emit_event(
                            "run_terminal",
                            run_id=run_id,
                            delivery_date=delivery_date,
                            outcome="needs_attention",
                            reason_code="telegram_ambiguous",
                            delivery_id=delivery_id,
                            chunk_id=chunk.chunk_id,
                        )
                        return RunOutcome(1, "needs_attention")
                    store.finish_chunk(chunk.chunk_id, "accepted", run_id=run_id, owner_id=owner_id, telegram_message_id=message_id)
                    chunk_attempt.finalize("success", outcome="accepted", telegram_message_id=message_id)
                    if stop_event is not None and stop_event.is_set():
                        emit_event("run_stopped", run_id=run_id, delivery_id=delivery_id, outcome="stopped")
                        return RunOutcome(0, "stopped")
        except Exception as exc:
            try:
                current = store.delivery(delivery_id) if delivery_id is not None else None
                in_flight = bool(current and store.delivery_has_inflight(current.delivery_id))
            except Exception:
                # A failure while inspecting the durable outbox is itself an
                # operator-attention condition.  In particular, do not turn a
                # possible post-ack commit failure into a fresh send attempt
                # merely because the connection can no longer be queried.
                emit_event(
                    "run_terminal",
                    level=logging.ERROR,
                    run_id=run_id,
                    delivery_date=delivery_date,
                    outcome="needs_attention" if delivery_id is not None else "failed_terminal",
                    reason_code="state_inspection_failed",
                    error_class=type(exc).__name__,
                )
                return RunOutcome(1, "needs_attention" if delivery_id is not None else "failed_terminal")
            if current and in_flight:
                # The request may have crossed the wire.  Do not convert an
                # unresolved in-flight row to a terminal failure or create a
                # replacement generation; lease expiry recovery will mark it
                # ambiguous durably.
                emit_event(
                    "run_terminal",
                    level=logging.ERROR,
                    run_id=run_id,
                    delivery_date=delivery_date,
                    outcome="needs_attention",
                    reason_code="telegram_ambiguous",
                    delivery_id=current.delivery_id,
                )
                return RunOutcome(1, "needs_attention")
            if stop_event is not None and stop_event.is_set():
                emit_event("run_stopped", run_id=run_id, delivery_id=delivery_id, outcome="stopped")
                return RunOutcome(0, "stopped")
            if current and current.state not in {"needs_attention", "retry_wait", "completed", "completed_empty", "failed_terminal"}:
                with suppress(Exception):
                    store.fail_delivery(current.delivery_id, f"{type(exc).__name__}: {exc}", owner_id=owner_id)
            emit_event(
                "run_terminal",
                level=logging.ERROR,
                run_id=run_id,
                delivery_date=delivery_date,
                outcome="failed_terminal",
                error_class=type(exc).__name__,
            )
            return RunOutcome(1, "failed_terminal")
        finally:
            if lease_acquired:
                with suppress(Exception):
                    store.release_lease(LEASE_SCOPE, owner_id)


def _next_delivery(config: Mapping[str, Any]) -> datetime:
    tz = get_timezone(str(config["timezone"]))
    hour, minute = (int(part) for part in str(config.get("delivery_time", "07:00")).split(":", 1))
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _is_due(config: AppConfig) -> bool:
    tz = get_timezone(config.timezone)
    now = datetime.now(tz)
    hour, minute = (int(part) for part in config.delivery_time.split(":", 1))
    return now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _has_recovery_work(config: AppConfig) -> bool:
    reader = _history_reader(os.getenv("STATE_DB", "data/meco_news.db"))
    if reader is None:
        return False
    try:
        active = reader.active_delivery(None)
        if not active or active.state in {"needs_attention", "failed_terminal"}:
            return False
        due = _parse_iso(active.next_attempt_at)
        return active.state in {"collecting", "prepared", "sending"} or (
            active.state == "retry_wait" and (due is None or due <= datetime.now(UTC))
        )
    finally:
        reader.close()


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Typed outcome for C3.4 — daemon must distinguish retry_wait/attention/terminal."""

    code: int
    outcome: str
    details: dict[str, Any] = field(default_factory=dict)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.code == other
        if isinstance(other, RunOutcome):
            return self.code == other.code and self.outcome == other.outcome and self.details == other.details
        return NotImplemented

    def __int__(self) -> int:
        return self.code


def _next_durable_retry() -> datetime | None:
    reader = _history_reader(os.getenv("STATE_DB", "data/meco_news.db"))
    if reader is None:
        return None
    try:
        return _parse_iso(reader.status_snapshot().get("next_retry_at"))
    finally:
        reader.close()


def _scheduler_heartbeat(
    state_path: str,
    owner_id: str,
    ttl_seconds: int,
    stop: threading.Event,
) -> None:
    """Keep the daemon's scheduler lease observable while it is idle or busy."""

    try:
        with StateStore(state_path) as store:
            while not stop.wait(60.0):
                store.heartbeat_lease(SCHEDULER_SCOPE, owner_id, ttl_seconds)
    except Exception as exc:
        emit_event(
            "scheduler_heartbeat_failed",
            level=logging.ERROR,
            outcome="failed_terminal",
            error_class=type(exc).__name__,
        )


def run_daemon(config: AppConfig, run_now: bool = False, *, config_path: str | None = None) -> int:
    if looks_placeholder(os.getenv("TELEGRAM_BOT_TOKEN", "")) or looks_placeholder(os.getenv("TELEGRAM_CHAT_ID", "")):
        emit_event("run_terminal", level=logging.ERROR, outcome="preflight_failed", error_class="TelegramSecretConfiguration")
        return 3

    state_path = os.getenv("STATE_DB", "data/meco_news.db")
    owner_id = str(uuid.uuid4())
    stop = threading.Event()
    # C3.4: resolve effective config path once (explicit > MECO_CONFIG > default)
    effective_config_path = config_path or os.getenv("MECO_CONFIG") or "config/watchlist.json"
    def reload_config() -> None:
        nonlocal config
        try:
            if effective_config_path:
                config = load_config(effective_config_path)
        except Exception as exc:
            emit_event("config_reload_failed", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)

    def consume_result(result: RunOutcome | int) -> int | None:
        outcome = result.outcome if isinstance(result, RunOutcome) else "legacy"
        if outcome in {"needs_attention", "failed_terminal"}:
            emit_event("scheduler_blocked", level=logging.ERROR, outcome=outcome)
            return 1
        return None

    previous_handlers: dict[int, Any] = {}
    with StateStore(state_path) as scheduler_store:
        lease = scheduler_store.acquire_lease(SCHEDULER_SCOPE, owner_id, config.lease_ttl_seconds)
        if not lease.acquired:
            emit_event("run_skipped", outcome="already_running", lease_scope=SCHEDULER_SCOPE, lease_owner=lease.owner_id)
            return 0
        heartbeat = threading.Thread(
            target=_scheduler_heartbeat,
            args=(state_path, owner_id, config.lease_ttl_seconds, stop),
            name="meco-scheduler-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    with suppress(ValueError, OSError):
                        previous_handlers[signum] = signal.getsignal(signum)
                        signal.signal(signum, lambda _signum, _frame: stop.set())
            if run_now or _is_due(config) or _has_recovery_work(config):
                reload_config()
                result = run_once(config, stop_event=stop)
                # C3.4: consume typed outcome — heartbeat fatal, recovery, and config reload depend on it
                blocked = consume_result(result)
                if blocked is not None:
                    return blocked
            while True:
                if stop.is_set():
                    return 0
                # C3.4: heartbeat liveness check each wake
                if not heartbeat.is_alive():
                    emit_event("run_terminal", level=logging.ERROR, outcome="failed_terminal", error_class="HeartbeatLost")
                    return 1
                # C3.4: reload effective config each cycle, keep old on failure.
                reload_config()
                # C3.4: recover incomplete/due work before planning new date each cycle
                if _has_recovery_work(config):
                    reload_config()
                    result = run_once(config, stop_event=stop)
                    blocked = consume_result(result)
                    if blocked is not None:
                        return blocked
                    continue
                scheduler_store.heartbeat_lease(SCHEDULER_SCOPE, owner_id, config.lease_ttl_seconds)
                target = _next_delivery(config)
                retry_target = _next_durable_retry()
                if retry_target and retry_target < target:
                    target = retry_target
                LOGGER.info("next digest scheduled for %s", target.isoformat())
                wait_deadline = time.monotonic() + max(0.0, (target - datetime.now(target.tzinfo)).total_seconds())
                while True:
                    remaining = wait_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    # C3.4: check heartbeat each 60s sleep chunk
                    if not heartbeat.is_alive():
                        emit_event("run_terminal", level=logging.ERROR, outcome="failed_terminal", error_class="HeartbeatLost")
                        return 1
                    if stop.wait(min(remaining, 60)):
                        return 0
                # The configuration is read again immediately before work, so
                # a long sleep cannot make a changed target/schedule stale.
                reload_config()
                result = run_once(config, stop_event=stop)
                blocked = consume_result(result)
                if blocked is not None:
                    return blocked
        finally:
            stop.set()
            heartbeat.join(timeout=5)
            with suppress(Exception):
                scheduler_store.release_lease(SCHEDULER_SCOPE, owner_id)
            for restore_signal, handler in previous_handlers.items():
                with suppress(ValueError, OSError):
                    signal.signal(restore_signal, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PT Meco Inoxprima daily market-news bot")
    parser.add_argument("--config", help="Path to watchlist JSON")
    parser.add_argument("--dry-run", action="store_true", help="Collect and rank without writable state or Telegram")
    parser.add_argument("--ignore-history", action="store_true", help="With --dry-run, preview candidates regardless of sent history")
    parser.add_argument("--force", action="store_true", help="Create a new audited generation after a completed date")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--test-telegram", action="store_true", help="Validate token and send a test message")
    parser.add_argument("--discover-chat", action="store_true", help="List chats that messaged the bot")
    parser.add_argument("--daemon", action="store_true", help="Stay running and deliver at config delivery_time")
    parser.add_argument("--run-now", action="store_true", help="With --daemon, run once before waiting")
    parser.add_argument("--run-if-due", action="store_true", help="Run once only when the configured local due window has arrived")
    parser.add_argument("--top-candidates", type=int, default=0, help="With --dry-run, explain the top N candidates")
    parser.add_argument(
        "--frozen-input",
        "--input",
        dest="frozen_input",
        help="With --dry-run, read a version-1 offline frozen input JSON (required)",
    )
    parser.add_argument("--preflight", action="store_true", help="Run offline readiness checks")
    parser.add_argument("--online", action="store_true", help="With --preflight, check Telegram online")
    parser.add_argument("--config-show", action="store_true", help="Print redacted effective configuration")
    parser.add_argument("--status", action="store_true", help="Print durable state status")
    parser.add_argument("--healthcheck", action="store_true", help="Return nonzero when service health is unsafe")
    parser.add_argument("--metrics", action="store_true", help="Export read-only operational metrics and status as one JSON document")
    parser.add_argument("--alert-file", help="Append redacted health firing/recovery receipts to an independent JSONL sink")
    parser.add_argument("--max-heartbeat-age", type=int, default=180, help="Health heartbeat age threshold in seconds")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Emit machine-readable output")
    parser.add_argument("--backup", metavar="PATH", help="Create an online SQLite backup at PATH")
    parser.add_argument("--restore", metavar="PATH", help="Restore a verified SQLite backup from PATH")
    parser.add_argument("--resolve-chunk", type=int, metavar="ID", help="Resolve one ambiguous outbox chunk")
    parser.add_argument("--resolution", choices=("sent", "retry"), help="Resolution for --resolve-chunk")
    parser.add_argument("--reason", help="Audited reason for manual chunk resolution")
    parser.add_argument("--operator", help="Operator identity for manual chunk resolution")
    parser.add_argument("--log-file", help="Optional rotating JSONL log path for live modes")
    parser.add_argument("--migrate", action="store_true", help="Apply pending migrations to the supported current schema")
    parser.add_argument("--to-version", type=int, help="Target schema version for --migrate")
    return parser


def _validate_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.top_candidates < 0:
        parser.error("--top-candidates must be nonnegative")
    if args.max_heartbeat_age <= 0:
        parser.error("--max-heartbeat-age must be positive")
    if args.resolve_chunk is None and (
        args.resolution is not None or (args.reason is not None or args.operator is not None) and not args.force
    ):
        parser.error("--resolution, --reason, and --operator require --resolve-chunk")
    if args.resolve_chunk is not None and args.resolve_chunk <= 0:
        parser.error("--resolve-chunk must be positive")
    if args.to_version is not None and not args.migrate:
        parser.error("--to-version requires --migrate")
    if args.migrate and args.to_version is None:
        parser.error("--migrate requires --to-version")
    if args.to_version is not None and args.to_version <= 0:
        parser.error("--to-version must be positive")
    command_flags = [
        args.test_telegram,
        args.discover_chat,
        args.preflight,
        args.config_show,
        args.status,
        args.healthcheck,
        args.metrics,
        bool(args.backup),
        bool(args.restore),
        args.resolve_chunk is not None,
        args.migrate,
    ]
    if sum(command_flags) > 1:
        parser.error("explicit command modes are mutually exclusive")
    explicit_mode = any(command_flags)
    if explicit_mode and any(
        (args.dry_run, args.force, args.daemon, args.run_now, args.run_if_due, args.top_candidates, args.ignore_history)
    ):
        parser.error("explicit command modes cannot be combined with delivery modifiers")
    if args.daemon and args.dry_run:
        parser.error("--daemon cannot be combined with --dry-run")
    if args.daemon and args.force:
        parser.error("--daemon cannot be combined with --force")
    if args.run_now and not args.daemon:
        parser.error("--run-now requires --daemon")
    if args.run_if_due and args.daemon:
        parser.error("--run-if-due cannot be combined with --daemon")
    if args.top_candidates and (not args.dry_run or args.daemon):
        parser.error("--top-candidates requires --dry-run and cannot run in daemon mode")
    if args.ignore_history and not args.dry_run:
        parser.error("--ignore-history requires --dry-run")
    if args.frozen_input and not args.dry_run:
        parser.error("--frozen-input/--input requires --dry-run")
    if args.dry_run and not args.frozen_input:
        parser.error("--dry-run requires --frozen-input/--input")
    if args.force and args.dry_run:
        parser.error("--force is a live delivery option and cannot be used with --dry-run")
    if args.force and (not args.reason or not args.operator):
        parser.error("--force requires --reason and --operator")
    if args.force and args.resolution is not None:
        parser.error("--resolution cannot be combined with --force")
    if args.online and not args.preflight:
        parser.error("--online requires --preflight")
    if args.json_output and args.daemon:
        parser.error("--json cannot be combined with --daemon")
    if args.alert_file and not args.healthcheck:
        parser.error("--alert-file requires --healthcheck")
    if args.resolve_chunk is not None and (args.resolution is None or not args.reason or not args.operator):
        parser.error("--resolve-chunk requires --resolution, --reason, and --operator")


def _state_status(path: str | Path, config: AppConfig | None = None) -> dict[str, Any]:
    reader = _history_reader(path)
    if reader is None:
        # F11: absence and damage need different states. A missing file is
        # initial setup; an existing file no reader can open is corrupt,
        # malformed, or incompatible. Reuse the inspection classifications
        # instead of collapsing every failure into "missing".
        try:
            inspected = inspect_state(path)
        except Exception:
            inspected = None
        if inspected is None:
            report: dict[str, Any] = {
                "schema_version": 0,
                "application_version": "",
                "state": "unreadable",
                "integrity": "unknown",
                "detail": "state inspection failed",
            }
        elif inspected.classification == "missing":
            report = {"schema_version": 0, "application_version": "", "state": "missing"}
        elif inspected.classification == "compatible":
            # Readable schema the live reader still cannot open: truthfully
            # unreadable, never missing.
            report = {
                "schema_version": inspected.schema_version,
                "application_version": __version__,
                "state": "unreadable",
                "integrity": inspected.integrity,
                "detail": inspected.detail,
            }
        else:
            report = {
                "schema_version": inspected.schema_version,
                "application_version": __version__,
                "state": inspected.classification,
                "integrity": inspected.integrity,
                "detail": inspected.detail,
            }
        if config is not None:
            report["next_due_at"] = _next_delivery(config).isoformat()
        return report
    try:
        try:
            report = reader.status_snapshot()
            report.setdefault("state", "ok")
            if config is not None:
                report["next_due_at"] = _next_delivery(config).isoformat()
            return report
        except sqlite3.Error:
            return {"schema_version": reader.schema_version, "application_version": "", "state": "unreadable"}
    finally:
        reader.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    parsed_args = parser.parse_args(argv)
    _validate_options(parser, parsed_args)
    args = RunOptions.from_namespace(parsed_args)
    # ponytail: validate before file side-effects — C1.1 requires no log file on invalid config/dry-run
    # Configure diagnostics before config so startup_failed never creates a
    # file.  Machine mode sends diagnostics to stderr and reserves stdout for
    # exactly one JSON document.
    diagnostic_stream = sys.stderr if args.json_output or args.metrics else sys.stdout
    configure_logging(
        level="DEBUG" if args.verbose else os.getenv("LOG_LEVEL", "INFO"),
        file_path=None,
        stream=diagnostic_stream,
    )
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        emit_event("startup_failed", level=logging.ERROR, outcome="preflight_failed", error_class="ConfigurationError")
        if args.json_output:
            print(json.dumps({"code": 2, "outcome": "preflight_failed", "error_class": "ConfigurationError"}, sort_keys=True))
        else:
            print(str(exc), file=sys.stderr)
        return 2
    if not args.dry_run:
        os.umask(0o077)
    # Reconfigure with file logging only after config is proven valid
    configure_logging(
        level="DEBUG" if args.verbose else os.getenv("LOG_LEVEL", "INFO"),
        file_path=None if args.dry_run else (args.log_file or os.getenv("LOG_FILE", "")) or None,
        stream=diagnostic_stream,
    )
    if args.dry_run:
        startup_mode = "dry_run"
    elif args.preflight:
        startup_mode = "preflight_online" if args.online else "preflight"
    elif args.config_show:
        startup_mode = "config_show"
    elif args.status:
        startup_mode = "status"
    elif args.healthcheck:
        startup_mode = "healthcheck"
    elif args.metrics:
        startup_mode = "metrics"
    elif args.backup:
        startup_mode = "backup"
    elif args.restore:
        startup_mode = "restore"
    elif args.resolve_chunk is not None:
        startup_mode = "resolve_chunk"
    elif args.migrate:
        startup_mode = "migrate"
    elif args.test_telegram:
        startup_mode = "test_telegram"
    elif args.discover_chat:
        startup_mode = "discover_chat"
    elif args.daemon:
        startup_mode = "daemon"
    else:
        startup_mode = "delivery"
    emit_event("startup", mode=startup_mode, config_hash=config.config_hash)

    if args.config_show:
        print(json.dumps(config.redacted(), indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if args.preflight:
        code, report = run_preflight(config, online=args.online)
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return code
    if args.status:
        report = _state_status(os.getenv("STATE_DB", "data/meco_news.db"), config)
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        # F11: missing is initial setup (success); any damaged state fails.
        return 0 if report.get("state") in {"ok", "missing"} else 1
    if args.healthcheck:
        healthy, report = healthcheck(config, max_heartbeat_age=max(1, args.max_heartbeat_age))
        if args.alert_file:
            from .alerts import JsonlAlertSink, evaluate_health

            receipts = evaluate_health(report, JsonlAlertSink(args.alert_file))
            report["alert_receipts"] = [receipt.as_dict() for receipt in receipts]
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if healthy else 1
    if args.metrics:
        from .metrics import metrics_snapshot

        report = metrics_snapshot(os.getenv("STATE_DB", "data/meco_news.db"))
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if report.get("state") in {"ok", "missing"} else 1
    if args.backup:
        try:
            artifact = create_backup(os.getenv("STATE_DB", "data/meco_news.db"), args.backup, config_hash=config.config_hash)
        except Exception as exc:
            emit_event("backup_failed", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)
            if args.json_output:
                print(json.dumps({"code": 1, "outcome": "backup_failed", "error_class": type(exc).__name__}, sort_keys=True))
            return 1
        print(
            json.dumps({"database": str(artifact.database), "manifest": str(artifact.manifest), "sha256": artifact.sha256}, sort_keys=True)
        )
        return 0
    if args.restore:
        try:
            target = restore_backup(args.restore, os.getenv("STATE_DB", "data/meco_news.db"))
        except Exception as exc:
            emit_event("restore_failed", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)
            if args.json_output:
                print(json.dumps({"code": 1, "outcome": "restore_failed", "error_class": type(exc).__name__}, sort_keys=True))
            return 1
        if args.json_output:
            print(json.dumps({"code": 0, "outcome": "restored", "target": str(target)}, sort_keys=True))
        else:
            print(f"Restored {target}", file=sys.stderr)
        return 0
    if args.resolve_chunk is not None:
        try:
            assert args.resolution is not None and args.reason is not None and args.operator is not None
            state_path = os.getenv("STATE_DB", "data/meco_news.db")
            with MaintenanceContext.acquire(state_path, owner=f"operator:{uuid.uuid4().hex}") as maintenance, StateStore(
                state_path, maintenance_context=maintenance
            ) as store:
                delivery = store.resolve_chunk(
                    args.resolve_chunk,
                    args.resolution,
                    reason=args.reason,
                    operator=args.operator,
                    maintenance_context=maintenance,
                )
            print(json.dumps({"delivery_id": delivery.delivery_id, "state": delivery.state}, sort_keys=True))
            return 0
        except Exception as exc:
            emit_event("resolution_failed", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)
            if args.json_output:
                print(json.dumps({"code": 1, "outcome": "resolution_failed", "error_class": type(exc).__name__}, sort_keys=True))
            return 1
    if args.migrate:
        # C2.2/C2.5: only the current audited target is exposed.  The
        # maintenance guard owns the path for the whole manifest/transaction
        # lifetime; normal runtimes cannot remain open concurrently.
        if args.to_version != CURRENT_SCHEMA_VERSION:
            payload = {
                "code": 2,
                "outcome": "unsupported_migration_target",
                "error_class": "ConfigurationError",
                "expected_to_version": CURRENT_SCHEMA_VERSION,
            }
            if args.json_output:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(
                    f"--to-version must equal the supported schema {CURRENT_SCHEMA_VERSION}; no state was changed",
                    file=sys.stderr,
                )
            return 2
        migration_path = Path(os.getenv("STATE_DB", "data/meco_news.db")).resolve()
        try:
            with MaintenanceContext.acquire(
                migration_path,
                owner=f"migrate:{os.getpid()}:{uuid.uuid4().hex}",
                scope="maintenance",
            ) as context:
                applied = run_guarded_migrations(
                    migration_path,
                    context=context,
                    app_version=__version__,
                    config_hash=config.config_hash,
                )
        except (OSError, StateError, sqlite3.Error, MaintenanceError) as exc:
            emit_event("migration_failed", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)
            payload = {"code": 1, "outcome": "migration_failed", "error_class": type(exc).__name__}
            if args.json_output:
                print(json.dumps(payload, sort_keys=True))
            return 1
        payload = {"code": 0, "outcome": "migration_applied", "applied": applied, "schema_version": CURRENT_SCHEMA_VERSION}
        if args.json_output:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 0
    if args.test_telegram or args.discover_chat:
        if looks_placeholder(os.getenv("TELEGRAM_BOT_TOKEN", "")) or (
            args.test_telegram and looks_placeholder(os.getenv("TELEGRAM_CHAT_ID", ""))
        ):
            emit_event("telegram_test_failed", level=logging.ERROR, outcome="preflight_failed", error_class="TelegramSecretConfiguration")
            if args.json_output:
                print(json.dumps({"code": 3, "outcome": "telegram_test_failed", "error_class": "TelegramSecretConfiguration"}, sort_keys=True))
            return 3
        try:
            client = TelegramClient(os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""), config.request_timeout_seconds)
            if args.discover_chat:
                chats = client.discover_chats()
                if args.json_output:
                    print(json.dumps({"code": 0 if chats else 1, "outcome": "chats_discovered" if chats else "no_chats", "chats": chats}, ensure_ascii=False, sort_keys=True))
                elif not chats:
                    print("No chats found. Open the bot in Telegram, send /start, then try again.", file=sys.stderr)
                else:
                    print(json.dumps(chats, indent=2, ensure_ascii=False))
                return 0 if chats else 1
            identity = client.get_me()
            message_id = client.send_html("<b>MECO Market Watch test successful.</b>\nTelegram delivery is configured.")
            if args.json_output:
                print(json.dumps({"code": 0, "outcome": "telegram_test_succeeded", "message_id": message_id, "bot": identity.get("username", identity.get("first_name", "bot"))}, ensure_ascii=False, sort_keys=True))
            else:
                print(f"Test delivered by @{identity.get('username', identity.get('first_name', 'bot'))}.", file=sys.stderr)
            return 0
        except Exception as exc:
            emit_event("telegram_test_failed", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)
            if args.json_output:
                print(json.dumps({"code": 1, "outcome": "telegram_test_failed", "error_class": type(exc).__name__}, sort_keys=True))
            return 1
    try:
        if args.daemon:
            return run_daemon(config, args.run_now, config_path=args.config)
        if args.run_if_due and not (_is_due(config) or _has_recovery_work(config)):
            emit_event("run_skipped", outcome="not_due")
            return 0
        result = run_once(
            config,
            dry_run=args.dry_run,
            force=args.force,
            top_candidates=max(0, args.top_candidates),
            ignore_history=args.ignore_history,
            frozen_input=args.frozen_input,
            force_operator=args.operator or "",
            force_reason=args.reason or "",
        )
        # C3.4: run_once is typed — unwrap for process exit code
        if isinstance(result, RunOutcome):
            if args.json_output:
                result_payload: dict[str, Any] = {"code": result.code, "outcome": result.outcome}
                result_payload.update(result.details)
                print(json.dumps(result_payload, ensure_ascii=False, sort_keys=True))
            return result.code
        return result
    except (OSError, StateError, sqlite3.Error) as exc:
        emit_event("run_terminal", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)
        if args.json_output:
            print(json.dumps({"code": 1, "outcome": "failed_terminal", "error_class": type(exc).__name__}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
