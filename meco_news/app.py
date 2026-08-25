"""CLI and scheduler integration for the production state machine."""

from __future__ import annotations

import argparse
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import uuid
from typing import Any
from collections.abc import Mapping

from .backup import create_backup, restore_backup
from .collectors import CollectionResult, collect_all
from .config import AppConfig, ConfigurationError, load_config, load_dotenv
from .observability import configure_logging, emit_event
from .preflight import healthcheck, looks_placeholder, run_preflight
from .ranking import deduplicate, filter_fresh, rank_item, select_digest
from .storage import StateError, StateStore
from .telegram import TelegramClient, TelegramSendError, build_digest
from .timezones import get_timezone


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
    preflight: bool
    online: bool
    config_show: bool
    status: bool
    healthcheck: bool
    max_heartbeat_age: int
    json_output: bool
    backup: str | None
    restore: str | None
    resolve_chunk: int | None
    resolution: str | None
    reason: str | None
    operator: str | None
    log_file: str | None

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
    # Deterministic bounded jitter keeps frozen retry state reproducible and
    # avoids synchronized workers without relying on global random state.
    deterministic_jitter = (attempt * 7) % (jitter + 1) if jitter else 0
    return timedelta(seconds=max(retry_after, min(maximum, base * (2 ** max(0, attempt - 1)) + deterministic_jitter)))


def _print_dry_run(
    items: list[Any],
    raw_count: int,
    issues: list[str],
    *,
    exclusions: Mapping[str, int] | None = None,
    collection: CollectionResult | None = None,
) -> None:
    print(f"Collected {raw_count} raw items; selected {len(items)} unsent stories.\n")
    if collection:
        print(
            f"Sources succeeded: {collection.successful_sources}; failed: {collection.failed_sources}; outcome: {'all_sources_failed' if collection.all_sources_failed else 'healthy_or_degraded'}"
        )
    if exclusions:
        print("Freshness exclusions: " + ", ".join(f"{key}={value}" for key, value in sorted(exclusions.items())))
    for index, item in enumerate(items, 1):
        published = item.published_at.isoformat() if item.published_at else "unknown date"
        print(f"{index}. [{item.score}] {item.title}")
        print(f"   {item.topic_label} | {item.source} | {published}")
        print(f"   matches: {', '.join(item.matches) or 'none'}")
        print(f"   {item.url}\n")
    if issues:
        print("Source issues:")
        for issue in issues:
            print(f"- {issue}")


def _collect_rank_select(
    config: AppConfig,
    collection: CollectionResult,
    store: StateStore | None,
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


def run_once(
    config: AppConfig,
    dry_run: bool = False,
    force: bool = False,
    top_candidates: int = 0,
    *,
    ignore_history: bool = False,
) -> int:
    """Run one collection/delivery attempt and return a stable process code."""

    state_path = os.getenv("STATE_DB", "data/meco_news.db")
    delivery_date = _delivery_date(config)
    if dry_run:
        history = _history_reader(state_path) if not ignore_history else None
        try:
            collection = collect_all(config)
            selected, exclusions, _, _ = _collect_rank_select(
                config,
                collection,
                history,
                ignore_history=ignore_history,
                top_candidates=top_candidates,
            )
            _print_dry_run(selected, len(collection.items), collection.issues, exclusions=exclusions, collection=collection)
            return 0
        finally:
            if history:
                history.close()

    if looks_placeholder(os.getenv("TELEGRAM_BOT_TOKEN", "")) or looks_placeholder(os.getenv("TELEGRAM_CHAT_ID", "")):
        emit_event("run_terminal", level=logging.ERROR, outcome="preflight_failed", error_class="TelegramSecretConfiguration")
        return 3

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
                return 0
            lease_acquired = True

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
                return 1
            if active and active.state == "needs_attention":
                emit_event(
                    "run_terminal", run_id=run_id, delivery_date=delivery_date, outcome="needs_attention", delivery_id=active.delivery_id
                )
                return 1
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
                    return 1
                # A content retry resumes its frozen chunk. Only a collection
                # retry (or a delivery with no outbox yet) reopens collection.
                if active.kind == "collection_retry" or not store.due_chunks(active.delivery_id):
                    active = store.reopen_for_collection(active.delivery_id)
            if active is None:
                if store.already_completed(delivery_date) and not force:
                    emit_event("run_skipped", run_id=run_id, delivery_date=delivery_date, outcome="already_completed")
                    return 0
                if force and store.status_snapshot().get("unresolved_ambiguity_count", 0):
                    emit_event(
                        "run_terminal",
                        run_id=run_id,
                        delivery_date=delivery_date,
                        outcome="needs_attention",
                        error_class="ambiguous_generation",
                    )
                    return 1
                active = store.create_delivery(
                    delivery_date,
                    generation=(
                        store.latest_generation(delivery_date) + 1 if force else max(0, store.latest_generation(delivery_date) + 1)
                    ),
                    run_id=run_id,
                    config_hash=config.config_hash,
                    state="collecting",
                )
            delivery_id = active.delivery_id

            # A prepared/sending delivery is resumed from its immutable outbox;
            # content is never regenerated after preparation.
            if active.state in {"collecting", "retry_wait"} and not store.due_chunks(active.delivery_id):
                store.heartbeat_lease(LEASE_SCOPE, owner_id, config.lease_ttl_seconds)
                collection = collect_all(config)
                store.record_source_results(active.delivery_id, collection.source_results)
                if collection.all_sources_failed:
                    retry_enabled = config.retry_policy.enabled and not os.getenv("MECO_DISABLE_RETRIES")
                    retry_number = store.record_collection_attempt(
                        active.delivery_id, run_id=run_id, error="all_sources_failed", outcome="collection_retry"
                    )
                    if not retry_enabled or retry_number >= config.retry_policy.max_attempts:
                        store.fail_delivery(active.delivery_id, "all_sources_failed_retry_exhausted")
                        emit_event(
                            "run_terminal",
                            run_id=run_id,
                            delivery_date=delivery_date,
                            outcome="failed_terminal",
                            reason_code="all_sources_failed",
                            retry_number=retry_number,
                        )
                    else:
                        next_attempt = datetime.now(UTC) + _retry_delay(config, retry_number)
                        store.set_collection_retry(active.delivery_id, next_attempt_at=next_attempt, error="all_sources_failed")
                        emit_event(
                            "run_terminal",
                            run_id=run_id,
                            delivery_date=delivery_date,
                            outcome="retry_wait",
                            reason_code="all_sources_failed",
                            retry_number=retry_number,
                            next_attempt_at=next_attempt.isoformat(),
                        )
                    return 1
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
                store.prepare_delivery(
                    active.delivery_id,
                    built.included_items,
                    built.messages,
                    item_chunk_indexes=built.item_chunk_indexes,
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

            client = TelegramClient(
                os.getenv("TELEGRAM_BOT_TOKEN", ""),
                os.getenv("TELEGRAM_CHAT_ID", ""),
                config.request_timeout_seconds,
            )
            while True:
                store.heartbeat_lease(LEASE_SCOPE, owner_id, config.lease_ttl_seconds)
                chunks = store.due_chunks(delivery_id)
                if not chunks:
                    current = store.delivery(delivery_id)
                    if current and current.state in {"completed", "completed_empty"}:
                        emit_event(
                            "run_terminal", run_id=run_id, delivery_date=delivery_date, outcome=current.state, delivery_id=delivery_id
                        )
                        return 0
                    emit_event("run_terminal", run_id=run_id, delivery_date=delivery_date, outcome="retry_wait", delivery_id=delivery_id)
                    return 1
                for chunk in chunks:
                    _, attempt_number = store.begin_chunk_attempt(chunk.chunk_id, run_id=run_id, owner_id=owner_id)
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
                            emit_event(
                                "run_terminal",
                                run_id=run_id,
                                delivery_date=delivery_date,
                                outcome="needs_attention",
                                reason_code=exc.reason_code,
                                delivery_id=delivery_id,
                                chunk_id=chunk.chunk_id,
                            )
                            return 1
                        if exc.outcome == "rejected_retryable" and retry_enabled and attempt_number < config.retry_policy.max_attempts:
                            next_attempt = datetime.now(UTC) + _retry_delay(config, attempt_number, retry_after=exc.retry_after)
                            store.finish_chunk(
                                chunk.chunk_id,
                                "rejected_retryable",
                                run_id=run_id,
                                owner_id=owner_id,
                                error_class=exc.reason_code,
                                error_text=str(exc),
                                next_attempt_at=next_attempt,
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
                            return 1
                        store.finish_chunk(
                            chunk.chunk_id,
                            "rejected_terminal",
                            run_id=run_id,
                            owner_id=owner_id,
                            error_class=exc.reason_code,
                            error_text=str(exc),
                        )
                        emit_event(
                            "run_terminal",
                            run_id=run_id,
                            delivery_date=delivery_date,
                            outcome="failed_terminal",
                            reason_code=exc.reason_code,
                            delivery_id=delivery_id,
                            chunk_id=chunk.chunk_id,
                        )
                        return 1
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
                        emit_event(
                            "run_terminal",
                            run_id=run_id,
                            delivery_date=delivery_date,
                            outcome="needs_attention",
                            reason_code="telegram_ambiguous",
                            delivery_id=delivery_id,
                            chunk_id=chunk.chunk_id,
                        )
                        return 1
                    store.finish_chunk(chunk.chunk_id, "accepted", run_id=run_id, owner_id=owner_id, telegram_message_id=message_id)
        except Exception as exc:
            current = store.delivery(delivery_id) if delivery_id is not None else None
            if current and current.state not in {"needs_attention", "retry_wait", "completed", "completed_empty", "failed_terminal"}:
                with suppress(Exception):
                    store.fail_run(delivery_date, f"{type(exc).__name__}: {exc}")
            emit_event(
                "run_terminal",
                level=logging.ERROR,
                run_id=run_id,
                delivery_date=delivery_date,
                outcome="failed_terminal",
                error_class=type(exc).__name__,
            )
            return 1
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
            if run_now or _is_due(config) or _has_recovery_work(config):
                run_once(config)
            while True:
                if config_path:
                    config = load_config(config_path)
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
                    time.sleep(min(remaining, 60))
                run_once(config)
        finally:
            stop.set()
            heartbeat.join(timeout=5)
            with suppress(Exception):
                scheduler_store.release_lease(SCHEDULER_SCOPE, owner_id)


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
    parser.add_argument("--preflight", action="store_true", help="Run offline readiness checks")
    parser.add_argument("--online", action="store_true", help="With --preflight, check Telegram online")
    parser.add_argument("--config-show", action="store_true", help="Print redacted effective configuration")
    parser.add_argument("--status", action="store_true", help="Print durable state status")
    parser.add_argument("--healthcheck", action="store_true", help="Return nonzero when service health is unsafe")
    parser.add_argument("--max-heartbeat-age", type=int, default=180, help="Health heartbeat age threshold in seconds")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Emit machine-readable output")
    parser.add_argument("--backup", metavar="PATH", help="Create an online SQLite backup at PATH")
    parser.add_argument("--restore", metavar="PATH", help="Restore a verified SQLite backup from PATH")
    parser.add_argument("--resolve-chunk", type=int, metavar="ID", help="Resolve one ambiguous outbox chunk")
    parser.add_argument("--resolution", choices=("sent", "retry"), help="Resolution for --resolve-chunk")
    parser.add_argument("--reason", help="Audited reason for manual chunk resolution")
    parser.add_argument("--operator", help="Operator identity for manual chunk resolution")
    parser.add_argument("--log-file", help="Optional rotating JSONL log path for live modes")
    return parser


def _validate_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.top_candidates < 0:
        parser.error("--top-candidates must be nonnegative")
    if args.max_heartbeat_age <= 0:
        parser.error("--max-heartbeat-age must be positive")
    if args.resolve_chunk is not None and args.resolve_chunk <= 0:
        parser.error("--resolve-chunk must be positive")
    command_flags = [
        args.test_telegram,
        args.discover_chat,
        args.preflight,
        args.config_show,
        args.status,
        args.healthcheck,
        bool(args.backup),
        bool(args.restore),
        args.resolve_chunk is not None,
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
    if args.force and args.dry_run:
        parser.error("--force is a live delivery option and cannot be used with --dry-run")
    if args.online and not args.preflight:
        parser.error("--online requires --preflight")
    if args.json_output and not (args.preflight or args.config_show or args.status or args.healthcheck):
        parser.error("--json is supported for preflight, config-show, status, and healthcheck")
    if args.resolve_chunk is not None and (args.resolution is None or not args.reason or not args.operator):
        parser.error("--resolve-chunk requires --resolution, --reason, and --operator")


def _state_status(path: str | Path, config: AppConfig | None = None) -> dict[str, Any]:
    reader = _history_reader(path)
    if reader is None:
        report: dict[str, Any] = {"schema_version": 0, "application_version": "", "state": "missing"}
        if config is not None:
            report["next_due_at"] = _next_delivery(config).isoformat()
        return report
    try:
        try:
            report = reader.status_snapshot()
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
    # Configure stdout-only before config so startup_failed never creates a file
    configure_logging(level="DEBUG" if args.verbose else os.getenv("LOG_LEVEL", "INFO"), file_path=None)
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        emit_event("startup_failed", level=logging.ERROR, outcome="preflight_failed", error_class="ConfigurationError")
        print(str(exc), file=sys.stderr)
        return 2
    if not args.dry_run:
        os.umask(0o077)
    # Reconfigure with file logging only after config is proven valid
    configure_logging(
        level="DEBUG" if args.verbose else os.getenv("LOG_LEVEL", "INFO"),
        file_path=None if args.dry_run else (args.log_file or os.getenv("LOG_FILE", "")) or None,
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
    elif args.backup:
        startup_mode = "backup"
    elif args.restore:
        startup_mode = "restore"
    elif args.resolve_chunk is not None:
        startup_mode = "resolve_chunk"
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
        return 0
    if args.healthcheck:
        healthy, report = healthcheck(config, max_heartbeat_age=max(1, args.max_heartbeat_age))
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if healthy else 1
    if args.backup:
        try:
            artifact = create_backup(os.getenv("STATE_DB", "data/meco_news.db"), args.backup, config_hash=config.config_hash)
        except Exception as exc:
            emit_event("backup_failed", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)
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
            return 1
        print(f"Restored {target}")
        return 0
    if args.resolve_chunk is not None:
        try:
            assert args.resolution is not None and args.reason is not None and args.operator is not None
            with StateStore(os.getenv("STATE_DB", "data/meco_news.db")) as store:
                delivery = store.resolve_chunk(args.resolve_chunk, args.resolution, reason=args.reason, operator=args.operator)
            print(json.dumps({"delivery_id": delivery.delivery_id, "state": delivery.state}, sort_keys=True))
            return 0
        except Exception as exc:
            emit_event("resolution_failed", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)
            return 1
    if args.test_telegram or args.discover_chat:
        if looks_placeholder(os.getenv("TELEGRAM_BOT_TOKEN", "")) or (
            args.test_telegram and looks_placeholder(os.getenv("TELEGRAM_CHAT_ID", ""))
        ):
            emit_event("telegram_test_failed", level=logging.ERROR, outcome="preflight_failed", error_class="TelegramSecretConfiguration")
            return 3
        try:
            client = TelegramClient(os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""), config.request_timeout_seconds)
            if args.discover_chat:
                chats = client.discover_chats()
                if not chats:
                    print("No chats found. Open the bot in Telegram, send /start, then try again.")
                    return 1
                print(json.dumps(chats, indent=2, ensure_ascii=False))
                return 0
            identity = client.get_me()
            client.send_html("<b>MECO Market Watch test successful.</b>\nTelegram delivery is configured.")
            print(f"Test delivered by @{identity.get('username', identity.get('first_name', 'bot'))}.")
            return 0
        except Exception as exc:
            emit_event("telegram_test_failed", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)
            return 1
    try:
        if args.daemon:
            return run_daemon(config, args.run_now, config_path=args.config)
        if args.run_if_due and not (_is_due(config) or _has_recovery_work(config)):
            emit_event("run_skipped", outcome="not_due")
            return 0
        return run_once(
            config, dry_run=args.dry_run, force=args.force, top_candidates=max(0, args.top_candidates), ignore_history=args.ignore_history
        )
    except (OSError, StateError, sqlite3.Error) as exc:
        emit_event("run_terminal", level=logging.ERROR, outcome="failed_terminal", error_class=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
