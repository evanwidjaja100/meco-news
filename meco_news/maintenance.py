"""Shared runtime and exclusive maintenance guards for the state database (closure plan C1.2/C2.2).

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
RUNTIME_TTL_SECONDS = DEFAULT_TTL_SECONDS
RUNTIME_DIR_SUFFIX = ".runtime.d"


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
        runtimes = _live_runtime_holders(resolved)
        if runtimes:
            owners = ", ".join(sorted({str(holder["owner"]) for holder in runtimes}))
            raise MaintenanceBusy(f"exclusive maintenance for {resolved} refused: live runtime holder(s): {owners}")
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
    def owner(self) -> str:
        return self._owner

    @property
    def token(self) -> str:
        return self._token

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


def runtime_dir(db_path: str | Path) -> Path:
    """Directory holding one marker file per live runtime holder."""
    return Path(f"{Path(db_path)}" + RUNTIME_DIR_SUFFIX)


def _pid_alive(pid: object) -> bool:
    """Best-effort process liveness; unparseable pids count as dead."""
    if isinstance(pid, bool):
        return False
    if isinstance(pid, int):
        value = pid
    elif isinstance(pid, str):
        try:
            value = int(pid)
        except ValueError:
            return False
    else:
        return False
    if value <= 0:
        return False
    if value == os.getpid():
        return True
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_runtime_holder(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "owner": str(data["owner"]),
            "pid": int(data["pid"]),
            "token": str(data["token"]),
            "acquired_at": datetime.fromisoformat(str(data["acquired_at"])),
            "ttl_seconds": float(data["ttl_seconds"]),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _live_runtime_holders(db_path: str | Path) -> list[dict[str, Any]]:
    """Live runtime holders; stale, dead-pid, and unreadable files never block.

    Stale or dead-pid files are pruned best-effort so a crashed holder cannot
    block maintenance past its TTL. A holder with a valid TTL and a live pid
    always blocks exclusive maintenance (it is never bypassed).
    """
    directory = runtime_dir(Path(db_path).resolve())
    live: list[dict[str, Any]] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.suffix != ".json" or not entry.is_file():
            continue
        holder = _read_runtime_holder(entry)
        if holder is None:
            continue
        if _is_stale(holder, _utc_now()) or not _pid_alive(holder["pid"]):
            with contextlib.suppress(OSError):
                entry.unlink(missing_ok=True)
            continue
        holder["path"] = entry
        live.append(holder)
    return live


class RuntimeContext:
    """Shared process-lifetime runtime hold for one database path.

    Any live runtime holder blocks exclusive maintenance acquire, and a live
    exclusive marker blocks runtime acquire. Multiple runtimes may coexist.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        owner: str,
        token: str,
        acquired_at: datetime,
        ttl_seconds: float,
    ) -> None:
        self._db_path = db_path
        self._owner = owner
        self._token = token
        self._acquired_at = acquired_at
        self._ttl_seconds = ttl_seconds

    @classmethod
    def acquire(
        cls,
        db_path: str | Path,
        *,
        owner: str,
        ttl_seconds: float = RUNTIME_TTL_SECONDS,
    ) -> RuntimeContext:
        resolved = Path(db_path).resolve()
        held, info = is_maintenance_held(resolved)
        if held:
            raise MaintenanceBusy(f"runtime hold for {resolved} refused: exclusive maintenance held by {info.get('owner')}")
        _live_runtime_holders(resolved)
        token = secrets.token_hex(16)
        now = _utc_now()
        payload = {
            "owner": owner,
            "pid": os.getpid(),
            "token": token,
            "acquired_at": now.isoformat(),
            "ttl_seconds": ttl_seconds,
        }
        directory = runtime_dir(resolved)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{token}.json"
        fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink(missing_ok=True)
            raise
        return cls(resolved, owner=owner, token=token, acquired_at=now, ttl_seconds=ttl_seconds)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def token(self) -> str:
        return self._token

    @property
    def live(self) -> bool:
        current = _read_runtime_holder(runtime_dir(self._db_path) / f"{self._token}.json")
        if current is None or current["token"] != self._token:
            return False
        return not _is_stale(current, _utc_now()) and _pid_alive(current["pid"])

    def refresh(self) -> None:
        """Extend a live hold; fails closed when missing or superseded."""
        target = runtime_dir(self._db_path) / f"{self._token}.json"
        current = _read_runtime_holder(target)
        if current is None or current["token"] != self._token:
            raise MaintenanceError("runtime hold is missing or superseded")
        now = _utc_now()
        payload = {
            "owner": self._owner,
            "pid": os.getpid(),
            "token": self._token,
            "acquired_at": now.isoformat(),
            "ttl_seconds": self._ttl_seconds,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink(missing_ok=True)
            raise
        self._acquired_at = now

    def release(self) -> bool:
        target = runtime_dir(self._db_path) / f"{self._token}.json"
        current = _read_runtime_holder(target)
        if current is None or current["token"] != self._token:
            return False
        try:
            target.unlink(missing_ok=True)
        except OSError:
            return False
        with contextlib.suppress(OSError):
            target.parent.rmdir()
        return True

    def __enter__(self) -> RuntimeContext:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def maintenance_fence(context: MaintenanceContext) -> str:
    """Fence token identifying one live exclusive hold for migration (C2.2)."""
    return context.token


def assert_maintenance_fence(context: MaintenanceContext, *, db_path: str | Path, scope: str = DEFAULT_SCOPE) -> str:
    """Require a live exclusive hold for a path/scope; return its fence token.

    This is the fence-check API consumed by the C2.2 migration runner and
    later by C2.3 state capabilities. It fails closed on wrong path, wrong
    scope, or a missing/stale/superseded hold.
    """
    resolved = Path(db_path).resolve()
    if context.db_path != resolved:
        raise MaintenanceError(f"maintenance context is for {context.db_path}, not {resolved}")
    if context.scope != scope:
        raise MaintenanceError(f"maintenance context scope {context.scope!r} does not match {scope!r}")
    if not context.live:
        raise MaintenanceError("maintenance context is missing, stale, or superseded")
    return context.token

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
    "RUNTIME_DIR_SUFFIX",
    "RUNTIME_TTL_SECONDS",
    "RuntimeContext",
    "assert_maintenance_fence",
    "maintenance_fence",
    "runtime_dir",
    "MaintenanceBusy",
    "MaintenanceContext",
    "MaintenanceError",
    "_maintenance_verify",
    "is_maintenance_held",
    "marker_path",
]
