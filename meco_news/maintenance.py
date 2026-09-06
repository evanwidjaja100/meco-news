"""Exclusive maintenance guard for the state database (closure plan C1.2).

While the guard is held, normal preflight reports ``ready=false`` with
``maintenance_in_progress`` and a nonzero exit instead of inspecting the
live database. Maintenance work itself uses :func:`_maintenance_verify`,
which requires the live :class:`MaintenanceContext`, reports
``verified_for_maintenance`` rather than ``ready``, and runs the
integrity/schema/storage checks needed for a temporary or swapped database.

The guard is a cooperative marker file written atomically next to the
database (``<name>.maintenance.json``); it is portable across Linux and
Windows. A stale marker (holder crashed or TTL elapsed) is treated as not
held so a dead holder can never block operations forever, and it may be
taken over by a new acquirer.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from .inspection import InspectionResult, WalProbeResult, inspect_state, probe_wal_capability

DEFAULT_SCOPE = "maintenance"
DEFAULT_TTL_SECONDS = 3600.0


class MaintenanceError(Exception):
    """Maintenance guard or verification failure."""


class MaintenanceBusy(MaintenanceError):
    """The maintenance guard is already held by a live holder."""


def marker_path(db_path: str | Path) -> Path:
    return Path(f"{Path(db_path)}" + ".maintenance.json")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _read_marker(marker: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return {
            "owner": str(data["owner"]),
            "scope": str(data["scope"]),
            "pid": int(data["pid"]),
            "token": str(data["token"]),
            "acquired_at": datetime.fromisoformat(str(data["acquired_at"])),
            "ttl_seconds": float(data["ttl_seconds"]),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _is_stale(marker: dict[str, Any], now: datetime) -> bool:
    acquired: datetime = marker["acquired_at"]
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=UTC)
    return (now - acquired).total_seconds() > float(marker["ttl_seconds"])


def is_maintenance_held(db_path: str | Path) -> tuple[bool, dict[str, Any]]:
    marker = _read_marker(marker_path(db_path))
    if marker is None:
        return False, {"held": False}
    if _is_stale(marker, _utc_now()):
        return False, {"held": False, "stale_marker": True, "owner": marker["owner"]}
    return True, {
        "held": True,
        "owner": marker["owner"],
        "scope": marker["scope"],
        "since": marker["acquired_at"].isoformat(),
    }


class MaintenanceContext:
    """Live handle on the exclusive maintenance guard for one database path."""

    def __init__(
        self,
        db_path: Path,
        *,
        owner: str,
        scope: str,
        token: str,
        acquired_at: datetime,
        ttl_seconds: float,
    ) -> None:
        self._db_path = db_path
        self._owner = owner
        self._scope = scope
        self._token = token
        self._acquired_at = acquired_at
        self._ttl_seconds = ttl_seconds

    @classmethod
    def acquire(
        cls,
        db_path: str | Path,
        *,
        owner: str,
        scope: str = DEFAULT_SCOPE,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> MaintenanceContext:
        resolved = Path(db_path).resolve()
        marker = marker_path(resolved)
        now = _utc_now()
        existing = _read_marker(marker)
        if existing is not None and not _is_stale(existing, now):
            raise MaintenanceBusy(f"maintenance guard for {resolved} is held by {existing['owner']} (scope {existing['scope']})")
        token = secrets.token_hex(16)
        payload = {
            "owner": owner,
            "scope": scope,
            "pid": os.getpid(),
            "token": token,
            "acquired_at": now.isoformat(),
            "ttl_seconds": ttl_seconds,
        }
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=marker.name + ".", dir=marker.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, marker)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink(missing_ok=True)
            raise
        return cls(resolved, owner=owner, scope=scope, token=token, acquired_at=now, ttl_seconds=ttl_seconds)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def live(self) -> bool:
        current = _read_marker(marker_path(self._db_path))
        if current is None or current["token"] != self._token or current["scope"] != self._scope:
            return False
        return not _is_stale(current, _utc_now())

    def release(self) -> bool:
        marker = marker_path(self._db_path)
        current = _read_marker(marker)
        if current is None or current["token"] != self._token:
            return False
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def __enter__(self) -> MaintenanceContext:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _maintenance_verify(db_path: str | Path, context: MaintenanceContext, *, scope: str = DEFAULT_SCOPE) -> dict[str, Any]:
    """Verify a database for maintenance work; never reports ``ready``.

    Requires the live guard for the same database path and scope, so normal
    callers cannot mistake this report for a preflight readiness verdict.
    """

    resolved = Path(db_path).resolve()
    if context.db_path != resolved:
        raise MaintenanceError(f"maintenance context is for {context.db_path}, not {resolved}")
    if context.scope != scope:
        raise MaintenanceError(f"maintenance context scope {context.scope!r} does not match {scope!r}")
    if not context.live:
        raise MaintenanceError("maintenance context is missing, stale, or superseded")
    inspection: InspectionResult = inspect_state(resolved)
    storage: WalProbeResult = probe_wal_capability(resolved.parent)
    return {
        "verified_for_maintenance": True,
        "classification": inspection.classification,
        "schema_version": inspection.schema_version,
        "integrity": inspection.integrity,
        "detail": inspection.detail,
        "wal": {"ok": storage.ok, "journal_mode": storage.journal_mode, "reason": storage.reason},
        "context": {"owner": is_maintenance_held(resolved)[1].get("owner"), "scope": context.scope},
    }


__all__ = [
    "DEFAULT_SCOPE",
    "DEFAULT_TTL_SECONDS",
    "MaintenanceBusy",
    "MaintenanceContext",
    "MaintenanceError",
    "_maintenance_verify",
    "is_maintenance_held",
    "marker_path",
]
