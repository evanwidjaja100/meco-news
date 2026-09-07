"""Read-only operational metrics and status export.

The state database is the source of truth for the counters that can be
reconstructed after a restart.  This module deliberately opens it through a
read-only SQLite URI: exporting metrics must not create a database, migrate a
schema, checkpoint WAL, or acquire a runtime authority.
"""

from __future__ import annotations

from datetime import datetime, UTC
import shutil
from pathlib import Path
import sqlite3
from typing import Any

from . import __version__
from .storage import StateError, StateStore


METRICS_SCHEMA_VERSION = 1

_COUNTER_NAMES = (
    "runs_total",
    "runs_completed",
    "runs_completed_empty",
    "runs_failed_terminal",
    "runs_needs_attention",
    "runs_retry_wait",
    "chunks_total",
    "chunks_sent",
    "chunks_retry_wait",
    "chunks_ambiguous",
    "chunks_failed_terminal",
    "chunk_attempts_total",
    "chunk_duration_seconds_total",
    "source_requests_total",
    "source_failures_total",
    "source_items_total",
    "source_quarantined_total",
    "source_bytes_total",
    "source_deadlines_total",
    "url_rejects_total",
    "redirect_rejects_total",
    "ssrf_rejects_total",
    "dedup_postings_total",
    "dedup_pairs_total",
    "dedup_similarity_total",
    "dedup_budget_exhausted_total",
    "db_errors_total",
    "lease_errors_total",
    "unresolved_count",
)


def _empty_counters() -> dict[str, int | float]:
    return {name: 0 for name in _COUNTER_NAMES}


def _scalar(connection: Any, query: str, parameters: tuple[Any, ...] = ()) -> int:
    row = connection.execute(query, parameters).fetchone()
    if not row or row[0] is None:
        return 0
    try:
        return max(0, int(row[0]))
    except (TypeError, ValueError, OverflowError):
        return 0


def _seconds(connection: Any, query: str) -> float:
    row = connection.execute(query).fetchone()
    if not row or row[0] is None:
        return 0.0
    try:
        return max(0.0, float(row[0]))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _status_for_missing(path: Path, generated_at: str) -> dict[str, Any]:
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "generated_at": generated_at,
        "application_version": __version__,
        "state": "missing",
        "status": {"state": "missing", "path": str(path)},
        "metrics": _empty_counters(),
        "disk": _disk_snapshot(path.parent),
    }


def _disk_snapshot(directory: Path) -> dict[str, int | float | bool]:
    try:
        usage = shutil.disk_usage(directory if directory.exists() else Path.cwd())
    except OSError:
        return {"available": False, "free_bytes": 0, "total_bytes": 0, "free_ratio": 0.0}
    total = int(usage.total)
    free = int(usage.free)
    return {
        "available": True,
        "free_bytes": free,
        "total_bytes": total,
        "free_ratio": (free / total) if total else 0.0,
    }


def _collect_counters(store: StateStore) -> dict[str, int | float]:
    connection = store.connection
    counters: dict[str, int | float] = _empty_counters()
    state_counts = {
        str(row[0]): _scalar(connection, "SELECT COUNT(*) FROM deliveries WHERE state=?", (row[0],))
        for row in connection.execute("SELECT DISTINCT state FROM deliveries").fetchall()
    }
    counters.update(
        {
            "runs_total": _scalar(connection, "SELECT COUNT(*) FROM deliveries"),
            "runs_completed": state_counts.get("completed", 0),
            "runs_completed_empty": state_counts.get("completed_empty", 0),
            "runs_failed_terminal": state_counts.get("failed_terminal", 0),
            "runs_needs_attention": state_counts.get("needs_attention", 0),
            "runs_retry_wait": state_counts.get("retry_wait", 0),
            "chunks_total": _scalar(connection, "SELECT COUNT(*) FROM outbox_chunks"),
            "chunks_sent": _scalar(connection, "SELECT COUNT(*) FROM outbox_chunks WHERE state='sent'"),
            "chunks_retry_wait": _scalar(connection, "SELECT COUNT(*) FROM outbox_chunks WHERE state='retry_wait'"),
            "chunks_ambiguous": _scalar(connection, "SELECT COUNT(*) FROM outbox_chunks WHERE state='ambiguous'"),
            "chunks_failed_terminal": _scalar(connection, "SELECT COUNT(*) FROM outbox_chunks WHERE state='failed_terminal'"),
            "chunk_attempts_total": _scalar(connection, "SELECT COUNT(*) FROM delivery_attempts WHERE chunk_id IS NOT NULL"),
            "source_requests_total": _scalar(connection, "SELECT COUNT(*) FROM source_results"),
            "source_failures_total": _scalar(connection, "SELECT COUNT(*) FROM source_results WHERE outcome <> 'succeeded'"),
            "source_items_total": _scalar(connection, "SELECT COALESCE(SUM(accepted_count),0) FROM source_results"),
            "source_quarantined_total": _scalar(connection, "SELECT COALESCE(SUM(quarantined_count),0) FROM source_results"),
            "source_bytes_total": _scalar(connection, "SELECT COALESCE(SUM(bytes_read),0) FROM source_results"),
            "source_deadlines_total": _scalar(
                connection,
                "SELECT COUNT(*) FROM source_results WHERE reason_code IN ('source_deadline_exceeded','cycle_deadline_exceeded')",
            ),
            "url_rejects_total": _scalar(connection, "SELECT COUNT(*) FROM source_results WHERE reason_code IN ('invalid_url','url_rejected')"),
            "redirect_rejects_total": _scalar(connection, "SELECT COUNT(*) FROM source_results WHERE reason_code LIKE 'redirect%'"),
            "ssrf_rejects_total": _scalar(connection, "SELECT COUNT(*) FROM source_results WHERE reason_code LIKE '%ssrf%' OR reason_code='dns_resolution_failed'"),
            "dedup_postings_total": _scalar(connection, "SELECT COUNT(*) FROM delivery_items"),
            "dedup_pairs_total": 0,
            "dedup_similarity_total": 0,
            "dedup_budget_exhausted_total": _scalar(connection, "SELECT COUNT(*) FROM source_results WHERE reason_code='dedup_budget_exhausted'"),
            "db_errors_total": _scalar(connection, "SELECT COUNT(*) FROM delivery_attempts WHERE error_class LIKE '%sqlite%' OR error_class LIKE '%Database%'"),
            "lease_errors_total": _scalar(connection, "SELECT COUNT(*) FROM state_transitions WHERE reason LIKE '%lease%' OR actor_type='lease_error'"),
            "unresolved_count": _scalar(connection, "SELECT COUNT(*) FROM outbox_chunks WHERE state='ambiguous'"),
        }
    )
    counters["chunk_duration_seconds_total"] = _seconds(
        connection,
        "SELECT COALESCE(SUM((julianday(ended_at)-julianday(started_at))*86400.0),0) "
        "FROM delivery_attempts WHERE chunk_id IS NOT NULL AND ended_at IS NOT NULL",
    )
    return counters


def metrics_snapshot(path: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Return a bounded, redaction-safe metrics/status document.

    ``state`` is one of ``missing``, ``ok`` or ``unreadable``.  Unreadable
    databases never yield a false zero-valued healthy report.
    """

    target = Path(path).resolve()
    generated_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    if not target.is_file():
        return _status_for_missing(target, generated_at)
    try:
        with StateStore(target, readonly=True) as store:
            status = store.status_snapshot()
            counters = _collect_counters(store)
            return {
                "schema_version": METRICS_SCHEMA_VERSION,
                "generated_at": generated_at,
                "application_version": __version__,
                "state": "ok",
                "status": status,
                "metrics": counters,
                "disk": _disk_snapshot(target.parent),
            }
    except (OSError, StateError, RuntimeError, ValueError, TypeError, sqlite3.Error):
        return {
            "schema_version": METRICS_SCHEMA_VERSION,
            "generated_at": generated_at,
            "application_version": __version__,
            "state": "unreadable",
            "status": {"state": "unreadable", "path": str(target)},
            "metrics": _empty_counters(),
            "disk": _disk_snapshot(target.parent),
        }


def metrics_schema() -> dict[str, Any]:
    """Describe units and cardinality for operators and monitoring systems."""

    counters: dict[str, dict[str, Any]] = {
        name: {"type": "counter", "unit": "seconds" if name.endswith("seconds_total") else "items", "labels": []}
        for name in _COUNTER_NAMES
    }
    counters["chunk_duration_seconds_total"]["unit"] = "seconds"
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "application_version": __version__,
        "cardinality": "fixed field set; no source/title/URL labels",
        "counters": counters,
        "gauges": {"disk.free_ratio": "ratio", "unresolved_count": "items"},
    }


__all__ = ["METRICS_SCHEMA_VERSION", "metrics_schema", "metrics_snapshot"]
