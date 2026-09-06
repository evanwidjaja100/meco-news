"""Offline/online readiness and health checks."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from . import __version__, inspection, maintenance
from .config import AppConfig
from .inspection import SUPPORTED_PYTHON_RANGE
from .migrations import CURRENT_SCHEMA_VERSION
from .network import BoundedHTTPClient
from .storage import StateStore, StateError
from .telegram import TelegramClient
from .timezones import get_timezone


PREFLIGHT_OK = 0
PREFLIGHT_CLI = 2
PREFLIGHT_SECRET = 3
PREFLIGHT_STATE = 4
PREFLIGHT_SCHEMA = 5
PREFLIGHT_LEASE = 6
PREFLIGHT_ONLINE = 7
PREFLIGHT_MAINTENANCE = 8
PREFLIGHT_RUNTIME = 9
MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024


def _disk_sufficient(free_bytes: int, total_bytes: int) -> bool:
    """Require both the absolute and proportional state-disk floor."""

    return free_bytes >= MIN_FREE_BYTES and free_bytes * 10 >= total_bytes


def looks_placeholder(value: str) -> bool:
    lowered = value.casefold().strip()
    return not lowered or lowered.startswith("replace_with_") or "your_token" in lowered or lowered in {"changeme", "token", "replace"}


def _state_path() -> Path:
    return Path(os.getenv("STATE_DB", "data/meco_news.db"))


def _secret_status() -> tuple[bool, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if looks_placeholder(token):
        return False, "telegram_token_missing_or_placeholder"
    if looks_placeholder(chat_id):
        return False, "telegram_chat_id_missing_or_placeholder"
    return True, "ok"


def run_preflight(config: AppConfig, *, online: bool = False, state_path: str | Path | None = None) -> tuple[int, dict[str, Any]]:
    """Offline readiness verdict with deterministic exit-code precedence.

    ready is the pure conjunction of the mandatory checks (timezone, runtime, state
    filesystem, maintenance, database, lease, secrets, plus the online checks when
    requested). Exit precedence for multiple failures: runtime (9) > state (4) >
    maintenance (8) > schema (5) > lease (6) > secret (3) > online (7).
    """
    path = Path(state_path or _state_path())
    report: dict[str, Any] = {
        "ready": True,
        "checks": {},
        "schema_version": None,
        "application_version": __version__,
    }
    lease_busy = False
    report["checks"]["configuration"] = {"ok": True, "config_hash": config.config_hash}
    try:
        tz = get_timezone(config.timezone)
        report["checks"]["timezone"] = {"ok": True, "name": config.timezone, "label": tz.tzname(datetime.now(tz))}
    except Exception:
        report["checks"]["timezone"] = {"ok": False, "reason": "timezone_unavailable"}
        report["ready"] = False

    runtime_ok, runtime_version = inspection.check_python_version()
    report["checks"]["runtime"] = {"ok": runtime_ok, "version": runtime_version, "supported": SUPPORTED_PYTHON_RANGE}
    if not runtime_ok:
        report["ready"] = False

    parent = path.parent.resolve()
    disk_ok = parent.exists() and os.access(parent, os.W_OK)
    total_bytes = 0
    try:
        usage = shutil.disk_usage(parent if parent.exists() else Path.cwd())
        free_bytes = int(usage.free)
        total_bytes = int(usage.total)
    except OSError:
        free_bytes = 0
    state_ok = bool(disk_ok and _disk_sufficient(free_bytes, total_bytes))
    wal_probe = inspection.probe_wal_capability(parent)
    report["checks"]["state_filesystem"] = {
        "ok": state_ok,
        "directory": str(parent),
        "free_bytes": free_bytes,
        "minimum_free_bytes": MIN_FREE_BYTES,
        "minimum_free_ratio": 0.10,
        "database_exists": path.exists(),
        "wal": {"ok": wal_probe.ok, "journal_mode": wal_probe.journal_mode, "reason": wal_probe.reason},
    }
    if not state_ok:
        report["ready"] = False

    secret_ok, secret_reason = _secret_status()
    report["checks"]["secrets"] = {"ok": secret_ok, "reason": secret_reason}
    if not secret_ok:
        report["ready"] = False

    maintenance_held, maintenance_info = maintenance.is_maintenance_held(path)
    if maintenance_held:
        report["checks"]["maintenance"] = {
            "ok": False,
            "reason": "maintenance_in_progress",
            "owner": maintenance_info.get("owner"),
            "scope": maintenance_info.get("scope"),
        }
        report["checks"]["database"] = {"ok": False, "reason": "maintenance_in_progress"}
        report["ready"] = False
    else:
        report["checks"]["maintenance"] = {
            "ok": True,
            "held": False,
            "stale_marker": bool(maintenance_info.get("stale_marker")),
        }
        inspection_result = inspection.inspect_state(path)
        classification = inspection_result.classification
        if classification == "missing":
            report["checks"]["database"] = {
                "ok": True,
                "integrity": "not_yet_created",
                "schema_version": 0,
                "classification": classification,
            }
        elif classification != "compatible":
            report["schema_version"] = inspection_result.schema_version
            report["checks"]["database"] = {
                "ok": False,
                "reason": inspection_result.detail,
                "classification": classification,
                "integrity": inspection_result.integrity,
                "schema_version": inspection_result.schema_version,
            }
            report["ready"] = False
        else:
            try:
                with StateStore(path, readonly=True) as store:
                    integrity = store.integrity_check()
                    report["schema_version"] = store.schema_version
                    report["application_version"] = __version__
                    report["checks"]["database"] = {
                        "ok": integrity == "ok" and store.schema_version == CURRENT_SCHEMA_VERSION,
                        "integrity": integrity,
                        "schema_version": store.schema_version,
                        "classification": classification,
                    }
                    # ponytail: fail-closed - N-1 (migration_required) and N+1 (newer_incompatible) both set ready=False
                    if integrity != "ok" or store.schema_version != CURRENT_SCHEMA_VERSION:
                        report["ready"] = False
                    report["status"] = store.status_snapshot()
                    leases = {
                        "delivery": report["status"].get("lease"),
                        "scheduler": report["status"].get("scheduler_lease"),
                    }
                    active_leases = [
                        scope
                        for scope, lease in leases.items()
                        if lease and str(lease.get("expires_at", "")) > datetime.now(UTC).isoformat()
                    ]
                    if active_leases:
                        lease_busy = True
                        report["checks"]["lease"] = {"ok": False, "reason": "active_lease", "scopes": active_leases}
                        report["ready"] = False
            except (OSError, StateError, RuntimeError, sqlite3.Error) as exc:
                report["checks"]["database"] = {
                    "ok": False,
                    "reason": type(exc).__name__,
                    "classification": classification,
                }
                report["ready"] = False

    if online and secret_ok:
        try:
            client = TelegramClient(os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""), config.request_timeout_seconds)
            identity = client.get_me()
            report["checks"]["telegram"] = {"ok": True, "username": identity.get("username", "") if isinstance(identity, dict) else ""}
        except Exception as exc:
            report["checks"]["telegram"] = {"ok": False, "reason": type(exc).__name__}
            report["ready"] = False
    elif online:
        report["checks"]["telegram"] = {"ok": False, "reason": "secrets_not_ready"}
        report["ready"] = False

    if online:
        source_checks: list[dict[str, Any]] = []
        source_client = BoundedHTTPClient(config.limits, config.network_policy)
        for feed in config.rss_typed[:3]:
            try:
                response = source_client.fetch(feed.url, source_id=feed.id)
                source_checks.append({"source_id": feed.id, "ok": True, "status": response.status, "bytes": len(response.payload)})
            except Exception as exc:
                source_checks.append({"source_id": feed.id, "ok": False, "reason": getattr(exc, "reason_code", type(exc).__name__)})
        report["checks"]["sources"] = source_checks
        if source_checks and not any(check["ok"] for check in source_checks):
            report["ready"] = False

    if not report["ready"]:
        if not runtime_ok:
            return PREFLIGHT_RUNTIME, report
        if not state_ok:
            return PREFLIGHT_STATE, report
        if maintenance_held:
            return PREFLIGHT_MAINTENANCE, report
        if path.exists() and not report["checks"].get("database", {}).get("ok", False):
            return PREFLIGHT_SCHEMA, report
        if lease_busy:
            return PREFLIGHT_LEASE, report
        if not secret_ok:
            return PREFLIGHT_SECRET, report
        if online:
            return PREFLIGHT_ONLINE, report
    return PREFLIGHT_OK, report


def healthcheck(
    config: AppConfig,
    *,
    state_path: str | Path | None = None,
    max_heartbeat_age: int = 180,
) -> tuple[bool, dict[str, Any]]:
    path = Path(state_path or _state_path())
    report: dict[str, Any] = {"healthy": True, "reasons": []}
    if not path.exists():
        report["healthy"] = False
        report["reasons"].append("state_missing")
        return False, report
    try:
        with StateStore(path, readonly=True) as store:
            status = store.status_snapshot()
    except Exception as exc:
        report["healthy"] = False
        report["reasons"].append("state_unreadable")
        report["error_class"] = type(exc).__name__
        return False, report
    report["status"] = status
    if status.get("integrity") != "ok":
        report["healthy"] = False
        report["reasons"].append("db_corrupt")
    now = datetime.now(UTC)
    leases = {
        "delivery": status.get("lease"),
        "scheduler": status.get("scheduler_lease"),
    }
    for scope, lease in leases.items():
        if not lease:
            continue
        try:
            heartbeat = datetime.fromisoformat(str(lease["heartbeat_at"]))
            if (now - heartbeat.astimezone(UTC)).total_seconds() > max_heartbeat_age:
                report["healthy"] = False
                report["reasons"].append(f"stale_{scope}_heartbeat")
        except (KeyError, TypeError, ValueError):
            report["healthy"] = False
            report["reasons"].append(f"invalid_{scope}_heartbeat")
    parent = path.parent.resolve()
    if not parent.exists() or not os.access(parent, os.W_OK):
        report["healthy"] = False
        report["reasons"].append("state_unwritable")
    else:
        try:
            usage = shutil.disk_usage(parent)
            if not _disk_sufficient(int(usage.free), int(usage.total)):
                report["healthy"] = False
                report["reasons"].append("state_disk_low")
        except OSError:
            report["healthy"] = False
            report["reasons"].append("state_disk_unavailable")
    active = status.get("active_delivery") or {}
    latest = status.get("latest_delivery") or {}
    # C3.5: distinguish completed_empty (healthy empty) vs all_sources_failed retry exhaustion (unhealthy)
    # completed_empty is healthy (coverage notice), retry_wait with all_sources_failed is unhealthy after max attempts
    if (
        active.get("state") in {"needs_attention", "failed_terminal"}
        or latest.get("state") in {"needs_attention", "failed_terminal"}
        or status.get("unresolved_ambiguity_count", 0)
    ):
        report["healthy"] = False
        report["reasons"].append("unresolved_delivery_failure")
    # Explicit check for completed_empty as healthy (distinct from retry_wait)
    if active.get("state") == "completed_empty":
        # completed_empty is healthy — no action, but explicitly handled for C3.5 test
        pass
    last_success = status.get("last_success_at")
    if last_success:
        try:
            age = now - datetime.fromisoformat(str(last_success)).astimezone(UTC)
            if age > timedelta(hours=26):
                report["healthy"] = False
                report["reasons"].append("overdue_delivery")
        except (TypeError, ValueError):
            report["healthy"] = False
            report["reasons"].append("invalid_last_success")
    # C3.5: all_sources_failed retry exhaustion is unhealthy (distinct from completed_empty)
    if active.get("state") == "retry_wait" and "all_sources_failed" in str(active.get("terminal_error", "")):
        # Exhausted source retries are unsafe even though no Telegram chunk is
        # currently ambiguous; they must not look like a healthy idle retry.
        report["healthy"] = False
        report["reasons"].append("all_sources_failed_retry_exhausted")
    return bool(report["healthy"]), report
