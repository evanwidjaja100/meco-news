"""Independent, durable alert receipts for health and recovery signals.

Logging is intentionally not used as the alert transport.  A sink receives
small redacted records with stable IDs and can be replaced by an SMTP, ticket,
or incident-management adapter without changing the health evaluator.  The
JSONL sink is a local, fsync'd reference implementation for scheduled checks
and deterministic tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, UTC
import contextlib
import json
import os
from pathlib import Path
import uuid
from typing import Any, Protocol, cast

from .observability import redact


@dataclass(frozen=True, slots=True)
class AlertRecord:
    alert_id: str
    severity: str
    threshold: str
    first_seen_at: str
    state: str
    dedup_key: str
    timestamp: str
    receipt_id: str
    escalation_level: int = 1
    recovery_of: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AlertSink(Protocol):
    def publish(self, record: AlertRecord) -> AlertRecord: ...

    def reconcile(self, active_keys: set[str], *, now: datetime | None = None) -> list[AlertRecord]: ...


def _time(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _stable_id(value: object, prefix: str = "alert") -> str:
    text = "".join(char if char.isalnum() or char in "._:-" else "_" for char in str(value).casefold())
    text = text.strip("._:-")[:100] or "unknown"
    return f"meco.{prefix}.{text}"


def _safe_details(details: object) -> dict[str, Any]:
    cleaned = redact(details)
    return cleaned if isinstance(cleaned, dict) else {"detail": cleaned}


class MemoryAlertSink:
    """A deterministic fake sink that records firing and recovery receipts."""

    def __init__(self) -> None:
        self.records: list[AlertRecord] = []
        self._last: dict[str, AlertRecord] = {}

    def publish(self, record: AlertRecord) -> AlertRecord:
        sanitized = AlertRecord(**{**record.as_dict(), "details": _safe_details(record.details)})
        previous = self._last.get(sanitized.dedup_key)
        if previous and previous.state == sanitized.state and sanitized.state == "firing":
            return AlertRecord(**{**sanitized.as_dict(), "receipt_id": previous.receipt_id, "first_seen_at": previous.first_seen_at})
        self.records.append(sanitized)
        self._last[sanitized.dedup_key] = sanitized
        return sanitized

    def reconcile(self, active_keys: set[str], *, now: datetime | None = None) -> list[AlertRecord]:
        return self.reconcile_scoped(active_keys, now=now)

    def reconcile_scoped(
        self,
        active_keys: set[str],
        *,
        now: datetime | None = None,
        scope: str | None = None,
        only_keys: set[str] | None = None,
    ) -> list[AlertRecord]:
        recovered: list[AlertRecord] = []
        for key, previous in list(self._last.items()):
            if (scope and not key.startswith(scope)) or (only_keys is not None and key not in only_keys):
                continue
            if key in active_keys or previous.state != "firing":
                continue
            recovery = AlertRecord(
                alert_id=previous.alert_id,
                severity=previous.severity,
                threshold=previous.threshold,
                first_seen_at=previous.first_seen_at,
                state="recovery",
                dedup_key=key,
                timestamp=_time(now),
                receipt_id=uuid.uuid4().hex,
                escalation_level=previous.escalation_level,
                recovery_of=previous.receipt_id,
                details=previous.details,
            )
            self.records.append(recovery)
            self._last[key] = recovery
            recovered.append(recovery)
        return recovered

    def reconcile_one(self, key: str, *, now: datetime | None = None) -> list[AlertRecord]:
        return self.reconcile_scoped(set(), now=now, only_keys={key})


class JsonlAlertSink:
    """Append-only local alert receipt sink with stateful deduplication."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last = self._read_last()

    def _read_last(self) -> dict[str, AlertRecord]:
        last: dict[str, AlertRecord] = {}
        if not self.path.is_file():
            return last
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-10_000:]
        except OSError:
            return last
        for line in lines:
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict) or not isinstance(raw.get("dedup_key"), str):
                    continue
                last[raw["dedup_key"]] = AlertRecord(
                    alert_id=str(raw["alert_id"]),
                    severity=str(raw["severity"]),
                    threshold=str(raw["threshold"]),
                    first_seen_at=str(raw["first_seen_at"]),
                    state=str(raw["state"]),
                    dedup_key=str(raw["dedup_key"]),
                    timestamp=str(raw["timestamp"]),
                    receipt_id=str(raw["receipt_id"]),
                    escalation_level=int(raw.get("escalation_level", 1)),
                    recovery_of=str(raw.get("recovery_of", "")),
                    details=_safe_details(raw.get("details", {})),
                )
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return last

    def publish(self, record: AlertRecord) -> AlertRecord:
        previous = self._last.get(record.dedup_key)
        if previous and previous.state == record.state == "firing":
            return AlertRecord(**{**record.as_dict(), "receipt_id": previous.receipt_id, "first_seen_at": previous.first_seen_at})
        sanitized = AlertRecord(**{**record.as_dict(), "details": _safe_details(record.details)})
        line = json.dumps(sanitized.as_dict(), ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        self._last[sanitized.dedup_key] = sanitized
        return sanitized

    def reconcile(self, active_keys: set[str], *, now: datetime | None = None) -> list[AlertRecord]:
        return self.reconcile_scoped(active_keys, now=now)

    def reconcile_scoped(
        self,
        active_keys: set[str],
        *,
        now: datetime | None = None,
        scope: str | None = None,
        only_keys: set[str] | None = None,
    ) -> list[AlertRecord]:
        recovered: list[AlertRecord] = []
        for key, previous in list(self._last.items()):
            if (scope and not key.startswith(scope)) or (only_keys is not None and key not in only_keys):
                continue
            if key in active_keys or previous.state != "firing":
                continue
            recovered_record = AlertRecord(
                alert_id=previous.alert_id,
                severity=previous.severity,
                threshold=previous.threshold,
                first_seen_at=previous.first_seen_at,
                state="recovery",
                dedup_key=key,
                timestamp=_time(now),
                receipt_id=uuid.uuid4().hex,
                escalation_level=previous.escalation_level,
                recovery_of=previous.receipt_id,
                details=previous.details,
            )
            recovered.append(self.publish(recovered_record))
        return recovered

    def reconcile_one(self, key: str, *, now: datetime | None = None) -> list[AlertRecord]:
        return self.reconcile_scoped(set(), now=now, only_keys={key})


_HEALTH_RULES: dict[str, tuple[str, str, str, int]] = {
    "state_missing": ("state", "critical", "state database is missing", 1),
    "state_unreadable": ("state", "critical", "state database cannot be read", 1),
    "db_corrupt": ("integrity", "critical", "SQLite integrity check failed", 1),
    "incompatible_schema": ("schema", "critical", "state schema is incompatible", 1),
    "maintenance_in_progress": ("maintenance", "warning", "exclusive maintenance is active", 1),
    "unresolved_delivery_failure": ("delivery", "critical", "delivery requires operator attention", 1),
    "all_sources_failed": ("sources", "critical", "all configured sources failed", 1),
    "all_sources_failed_retry_exhausted": ("sources", "critical", "source retry budget is exhausted", 2),
    "abandoned_delivery": ("delivery", "critical", "active delivery work is abandoned", 1),
    "chunk_retry_exhausted": ("delivery", "critical", "chunk retry budget is exhausted", 2),
    "overdue_delivery": ("schedule", "critical", "scheduled delivery is overdue", 1),
    "stale_delivery_heartbeat": ("lease", "critical", "delivery heartbeat is stale", 1),
    "stale_scheduler_heartbeat": ("lease", "critical", "scheduler heartbeat is stale", 1),
    "state_disk_low": ("resources", "critical", "state disk is below the safety floor", 1),
    "state_disk_unavailable": ("resources", "critical", "state disk usage is unavailable", 1),
    "state_unwritable": ("resources", "critical", "state directory is not writable", 1),
}


def evaluate_health(
    report: dict[str, Any],
    sink: AlertSink,
    *,
    now: datetime | None = None,
) -> list[AlertRecord]:
    """Publish stable firing/recovery receipts for one health report."""

    timestamp = _time(now)
    active: set[str] = set()
    receipts: list[AlertRecord] = []
    reasons = report.get("reasons", []) if isinstance(report, dict) else []
    for reason in reasons if isinstance(reasons, list) else []:
        key = str(reason)
        rule = _HEALTH_RULES.get(key)
        if rule is None:
            rule = ("health", "critical", "unclassified health failure", 1)
        category, severity, threshold, escalation = rule
        dedup_key = f"health:{key}"
        active.add(dedup_key)
        record = AlertRecord(
            alert_id=_stable_id(key, "health"),
            severity=severity,
            threshold=threshold,
            first_seen_at=timestamp,
            state="firing",
            dedup_key=dedup_key,
            timestamp=timestamp,
            receipt_id=uuid.uuid4().hex,
            escalation_level=escalation,
            details={"reason": key, "category": category},
        )
        receipts.append(sink.publish(record))
    scoped_reconcile = getattr(sink, "reconcile_scoped", None)
    if callable(scoped_reconcile):
        receipts.extend(scoped_reconcile(active, now=now, scope="health:"))
    else:
        receipts.extend(sink.reconcile(active, now=now))
    return receipts


def evaluate_backup(
    *,
    success: bool,
    sink: AlertSink,
    detail: str = "",
    now: datetime | None = None,
) -> list[AlertRecord]:
    """Emit a durable backup failure or recovery receipt."""

    timestamp = _time(now)
    key = "backup:verification"
    if success:
        reconcile_one = getattr(sink, "reconcile_one", None)
        if callable(reconcile_one):
            return cast(list[AlertRecord], reconcile_one(key, now=now))
        return []
    return [
        sink.publish(
            AlertRecord(
                alert_id="meco.backup.verification",
                severity="critical",
                threshold="verified backup required",
                first_seen_at=timestamp,
                state="firing",
                dedup_key=key,
                timestamp=timestamp,
                receipt_id=uuid.uuid4().hex,
                escalation_level=2,
                details={"detail": detail[:500]},
            )
        )
    ]


__all__ = ["AlertRecord", "AlertSink", "JsonlAlertSink", "MemoryAlertSink", "evaluate_backup", "evaluate_health"]
