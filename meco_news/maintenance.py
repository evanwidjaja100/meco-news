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
import ctypes
import json
import os
import sqlite3
import secrets
import sys
import tempfile
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import msvcrt as _msvcrt
else:  # pragma: no cover - exercised on Linux CI
    _msvcrt = None

from .inspection import InspectionResult, WalProbeResult, inspect_state, probe_wal_capability

DEFAULT_SCOPE = "maintenance"
DEFAULT_TTL_SECONDS = 3600.0
RUNTIME_TTL_SECONDS = DEFAULT_TTL_SECONDS
RUNTIME_DIR_SUFFIX = ".runtime.d"
GUARD_LOCK_SUFFIX = ".guard.lock"


class _GuardFile:
    def __init__(self, path: Path, handle: Any, *, exclusive: bool, overlapped: Any = None) -> None:
        self.path = path
        self.handle = handle
        self.exclusive = exclusive
        self.overlapped = overlapped

    def release(self) -> None:
        if self.handle is None:
            return
        if sys.platform == "win32":
            if self.overlapped is not None:
                ctypes.windll.kernel32.UnlockFileEx(
                    _msvcrt.get_osfhandle(self.handle.fileno()), 0, 1, 0, ctypes.byref(self.overlapped)
                )
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def _guard_lock_path(db_path: Path) -> Path:
    return Path(f"{db_path}{GUARD_LOCK_SUFFIX}")


def _acquire_guard_file(db_path: Path, *, exclusive: bool) -> _GuardFile:
    path = _guard_lock_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    try:
        if sys.platform == "win32":
            class _Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_void_p),
                    ("InternalHigh", ctypes.c_void_p),
                    ("Offset", ctypes.c_ulong),
                    ("OffsetHigh", ctypes.c_ulong),
                    ("hEvent", ctypes.c_void_p),
                ]

            overlapped = _Overlapped()
            flags = 0x00000002 if exclusive else 0
            ok = ctypes.windll.kernel32.LockFileEx(
                _msvcrt.get_osfhandle(handle.fileno()), flags | 0x00000001, 0, 1, 0, ctypes.byref(overlapped)
            )
            if not ok:
                raise OSError(32, "guard lock is busy")
            return _GuardFile(path, handle, exclusive=exclusive, overlapped=overlapped)
        import fcntl

        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
        return _GuardFile(path, handle, exclusive=exclusive)
    except BaseException:
        handle.close()
        raise


_LOCAL_GUARDS: dict[str, _GuardFile] = {}
_LOCAL_SHARED_GUARDS: dict[str, tuple[_GuardFile, int]] = {}


_REPLACE_ATTEMPTS = 10
_REPLACE_RETRY_DELAY_SECONDS = 0.05


def _durable_replace(temporary: str | Path, target: str | Path) -> None:
    """Publish a staged file, tolerating transient Windows file locks.
    Real-time scanners can briefly lock a brand-new temp file so the publish
    fails with PermissionError even though no rival holder exists. Retry
    briefly, then raise: a persistent failure still fails closed.
    """
    last_error: PermissionError | None = None
    for _ in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, target)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS)
    assert last_error is not None
    raise last_error


def _advance_durable_fence(db_path: Path, payload: dict[str, Any]) -> None:
    """Publish a small, atomic fence record for cooperating writers.

    The marker remains the human-readable lease.  This separate record lets a
    state writer compare the fence token without depending on a partially
    written JSON marker.  It is always created next to the database and is
    removed only by the holder that owns the token.
    """

    target = Path(f"{db_path}.maintenance.fence")
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"token": payload["token"], "owner": payload["owner"], "pid": payload["pid"], "process_identity": payload.get("process_identity", "")}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(temporary).unlink(missing_ok=True)
        raise


def _clear_durable_fence(db_path: Path, token: str) -> None:
    target = Path(f"{db_path}.maintenance.fence")
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if isinstance(current, dict) and current.get("token") == token:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)


def _release_database_fence(db_path: Path, token: str) -> None:
    """Mark a matching database fence released without deleting its epoch."""

    if not db_path.is_file():
        return
    connection = sqlite3.connect(db_path, timeout=5)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_fences'"
        ).fetchone()
        if table is None:
            return
        with connection:
            connection.execute(
                "UPDATE maintenance_fences SET released_at=? WHERE fence_id=1 AND token=? AND released_at IS NULL",
                (_utc_now().isoformat(), token),
            )
    finally:
        connection.close()


def _assert_database_fence(context: MaintenanceContext) -> None:
    """Check the durable fence row when this schema supports one."""

    if context.database_epoch is None or not context.db_path.is_file():
        return
    connection = sqlite3.connect(context.db_path, timeout=5)
    try:
        row = connection.execute(
            "SELECT epoch,token,released_at FROM maintenance_fences WHERE fence_id=1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise MaintenanceError("maintenance database fence cannot be read") from exc
    finally:
        connection.close()
    if row is None or int(row[0]) != context.database_epoch or str(row[1]) != context.token or row[2] is not None:
        raise MaintenanceError("maintenance database fence is missing, stale, or superseded")


def ensure_database_fence(connection: sqlite3.Connection, context: MaintenanceContext) -> int | None:
    """Upsert the current maintenance fence inside an existing SQL transaction.

    Acquiring an otherwise-unused maintenance guard must be observationally
    side-effect free.  Callers that are about to commit an operator or schema
    mutation invoke this helper while their transaction is already open.
    """

    if not context.live:
        raise MaintenanceError("maintenance context is missing, stale, or superseded")
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_fences'"
    ).fetchone()
    if table is None:
        return None
    row = connection.execute("SELECT epoch,token,released_at FROM maintenance_fences WHERE fence_id=1").fetchone()
    if row is not None and str(row[1]) == context.token and row[2] is None:
        epoch = int(row[0])
    else:
        epoch = int(row[0]) + 1 if row is not None else 1
        if row is None:
            connection.execute(
                "INSERT INTO maintenance_fences(fence_id,epoch,token,owner,scope,acquired_at,released_at) VALUES (1,?,?,?,?,?,NULL)",
                (epoch, context.token, context.owner, context.scope, context._acquired_at.isoformat()),
            )
        else:
            connection.execute(
                "UPDATE maintenance_fences SET epoch=?,token=?,owner=?,scope=?,acquired_at=?,released_at=NULL WHERE fence_id=1",
                (epoch, context.token, context.owner, context.scope, context._acquired_at.isoformat()),
            )
    context._database_epoch = epoch
    return epoch


def _remember_guard(token: str, guard: _GuardFile) -> None:
    _LOCAL_GUARDS[token] = guard


def _release_local_guard(token: str) -> None:
    guard = _LOCAL_GUARDS.pop(token, None)
    if guard is not None:
        with contextlib.suppress(OSError):
            guard.release()


class MaintenanceError(Exception):
    """Maintenance guard or verification failure."""


class MaintenanceBusy(MaintenanceError):
    """The maintenance guard is already held by a live holder."""


def marker_path(db_path: str | Path) -> Path:
    return Path(f"{Path(db_path)}" + ".maintenance.json")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _process_identity(pid: int) -> str:
    """Return a best-effort process-creation identity for stale-holder checks."""
    if pid <= 0:
        return ""
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            creation = ctypes.c_ulonglong()
            exit_time = ctypes.c_ulonglong()
            kernel_time = ctypes.c_ulonglong()
            user_time = ctypes.c_ulonglong()
            ok = ctypes.windll.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )
            return f"windows:{creation.value}" if ok else f"windows:{pid}"
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return f"linux:{fields[21]}" if len(fields) > 21 else f"pid:{pid}"
    except (OSError, IndexError, ValueError):
        return f"pid:{pid}"


def _pid_alive(pid: object, identity: object = "") -> bool:
    """Use a process query and, when available, reject PID reuse."""
    if isinstance(pid, bool):
        return False
    if isinstance(pid, int):
        value = pid
    elif isinstance(pid, str) and pid.strip().isdigit():
        value = int(pid.strip())
    else:
        return False
    if value <= 0:
        return False
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, value)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            live = exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(value, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            live = True
        except OSError:
            return False
        else:
            live = True
    expected = str(identity or "")
    return live and (not expected or expected == _process_identity(value))


def _read_marker(marker: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return {
            "owner": str(data["owner"]),
            "scope": str(data["scope"]),
            "pid": int(data["pid"]),
            "process_identity": str(data.get("process_identity", "")),
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
    if (now - acquired).total_seconds() <= float(marker["ttl_seconds"]):
        return False
    # A positive TTL is only stale when the recorded process has gone away.
    # Non-positive TTLs are reserved for deterministic dead-holder fixtures.
    return float(marker["ttl_seconds"]) <= 0 or not _pid_alive(marker["pid"], marker.get("process_identity", ""))


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
        guard: _GuardFile,
        database_epoch: int | None = None,
    ) -> None:
        self._db_path = db_path
        self._owner = owner
        self._scope = scope
        self._token = token
        self._acquired_at = acquired_at
        self._ttl_seconds = ttl_seconds
        self._guard = guard
        self._database_epoch = database_epoch

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
        try:
            guard = _acquire_guard_file(resolved, exclusive=True)
        except (OSError, PermissionError) as exc:
            # A stale marker created by this process may still have its OS
            # lock held.  Release only that exact local owner and retry; a
            # live marker or a lock owned by another process remains busy.
            existing = _read_marker(marker)
            if existing is not None and _is_stale(existing, _utc_now()):
                token = str(existing.get("token", ""))
                if token in _LOCAL_GUARDS:
                    _release_local_guard(token)
                else:
                    raise MaintenanceBusy(f"maintenance guard for {resolved} is already held") from exc
            else:
                # A stale runtime marker can still retain a shared lock when
                # it was created by this process.  Prune it, then release the
                # local shared handle before retrying the OS-level upgrade.
                key = str(resolved)
                if key not in _LOCAL_SHARED_GUARDS or _live_runtime_holders(resolved):
                    raise MaintenanceBusy(f"maintenance guard for {resolved} is already held") from exc
                shared_guard, _ = _LOCAL_SHARED_GUARDS.pop(key)
                with contextlib.suppress(OSError):
                    shared_guard.release()
            try:
                guard = _acquire_guard_file(resolved, exclusive=True)
            except (OSError, PermissionError) as retry_exc:
                raise MaintenanceBusy(f"maintenance guard for {resolved} is already held") from retry_exc
        now = _utc_now()
        existing = _read_marker(marker)
        created_token: str | None = None
        database_epoch: int | None = None
        created_marker = False
        try:
            if existing is not None and not _is_stale(existing, now):
                raise MaintenanceBusy(f"maintenance guard for {resolved} is held by {existing['owner']} (scope {existing['scope']})")
            if existing is not None:
                _release_local_guard(str(existing.get("token", "")))
                with contextlib.suppress(OSError):
                    marker.unlink(missing_ok=True)
            runtimes = _live_runtime_holders(resolved)
            if runtimes:
                owners = ", ".join(sorted({str(holder["owner"]) for holder in runtimes}))
                raise MaintenanceBusy(f"exclusive maintenance for {resolved} refused: live runtime holder(s): {owners}")
            token = secrets.token_hex(16)
            created_token = token
            payload = {
                "owner": owner,
                "scope": scope,
                "pid": os.getpid(),
                "process_identity": _process_identity(os.getpid()),
                "token": token,
                "acquired_at": now.isoformat(),
                "ttl_seconds": ttl_seconds,
            }
            marker.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=marker.name + ".", dir=marker.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            _durable_replace(tmp_name, marker)
            created_marker = True
            _advance_durable_fence(resolved, payload)
            _remember_guard(token, guard)
            return cls(
                resolved,
                owner=owner,
                scope=scope,
                token=token,
                acquired_at=now,
                ttl_seconds=ttl_seconds,
                guard=guard,
                database_epoch=database_epoch,
            )
        except BaseException:
            if "tmp_name" in locals():
                with contextlib.suppress(OSError):
                    Path(tmp_name).unlink(missing_ok=True)
            if created_marker and created_token is not None:
                current = _read_marker(marker)
                if current is not None and current.get("token") == created_token:
                    with contextlib.suppress(OSError):
                        marker.unlink(missing_ok=True)
                _clear_durable_fence(resolved, created_token)
            guard.release()
            raise

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
    def database_epoch(self) -> int | None:
        return self._database_epoch

    @property
    def live(self) -> bool:
        current = _read_marker(marker_path(self._db_path))
        if current is None or current["token"] != self._token or current["scope"] != self._scope:
            return False
        return not _is_stale(current, _utc_now())

    def release(self) -> bool:
        marker = marker_path(self._db_path)
        current = _read_marker(marker)
        owns_marker = current is not None and current["token"] == self._token
        released = owns_marker
        try:
            if owns_marker:
                with contextlib.suppress(Exception):
                    _release_database_fence(self._db_path, self._token)
                _clear_durable_fence(self._db_path, self._token)
                marker.unlink(missing_ok=True)
        except OSError:
            released = False
        finally:
            # A filesystem failure must not keep the kernel lock held until
            # process exit.  The marker may remain for stale-holder recovery,
            # but this context no longer has authority after release is called.
            _LOCAL_GUARDS.pop(self._token, None)
            with contextlib.suppress(OSError):
                self._guard.release()
        return released

    def __enter__(self) -> MaintenanceContext:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def runtime_dir(db_path: str | Path) -> Path:
    """Directory holding one marker file per live runtime holder."""
    # Resolved so 8.3 short-name aliases on Windows share one directory:
    # otherwise writers and readers could use different sibling dirs and
    # mutual exclusion would silently stop working.
    return Path(f"{Path(db_path).resolve()}" + RUNTIME_DIR_SUFFIX)

def _read_runtime_holder(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "owner": str(data["owner"]),
            "pid": int(data["pid"]),
            "process_identity": str(data.get("process_identity", "")),
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
        if _is_stale(holder, _utc_now()) or not _pid_alive(holder["pid"], holder.get("process_identity", "")):
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
        guard: _GuardFile,
    ) -> None:
        self._db_path = db_path
        self._owner = owner
        self._token = token
        self._acquired_at = acquired_at
        self._ttl_seconds = ttl_seconds
        self._guard = guard

    @classmethod
    def acquire(
        cls,
        db_path: str | Path,
        *,
        owner: str,
        ttl_seconds: float = RUNTIME_TTL_SECONDS,
    ) -> RuntimeContext:
        resolved = Path(db_path).resolve()
        key = str(resolved)
        shared_entry = _LOCAL_SHARED_GUARDS.get(key)
        owns_shared_guard = shared_entry is None
        if shared_entry is None:
            try:
                guard = _acquire_guard_file(resolved, exclusive=False)
            except (OSError, PermissionError) as exc:
                raise MaintenanceBusy(f"runtime hold for {resolved} refused: exclusive maintenance is active") from exc
            _LOCAL_SHARED_GUARDS[key] = (guard, 1)
        else:
            guard, count = shared_entry
            _LOCAL_SHARED_GUARDS[key] = (guard, count + 1)
        try:
            held, info = is_maintenance_held(resolved)
            if held:
                raise MaintenanceBusy(f"runtime hold for {resolved} refused: exclusive maintenance held by {info.get('owner')}")
            _live_runtime_holders(resolved)
            token = secrets.token_hex(16)
            now = _utc_now()
            payload = {
                "owner": owner,
                "pid": os.getpid(),
                "process_identity": _process_identity(os.getpid()),
                "token": token,
                "acquired_at": now.isoformat(),
                "ttl_seconds": ttl_seconds,
            }
            directory = runtime_dir(resolved)
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{token}.json"
            fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            _durable_replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                if "tmp_name" in locals():
                    Path(tmp_name).unlink(missing_ok=True)
            if owns_shared_guard:
                entry = _LOCAL_SHARED_GUARDS.pop(key, None)
                if entry is not None:
                    with contextlib.suppress(OSError):
                        entry[0].release()
            else:
                entry = _LOCAL_SHARED_GUARDS.get(key)
                if entry is not None:
                    current_guard, count = entry
                    if count <= 1:
                        _LOCAL_SHARED_GUARDS.pop(key, None)
                        with contextlib.suppress(OSError):
                            current_guard.release()
                    else:
                        _LOCAL_SHARED_GUARDS[key] = (current_guard, count - 1)
            raise
        return cls(resolved, owner=owner, token=token, acquired_at=now, ttl_seconds=ttl_seconds, guard=guard)

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
        return not _is_stale(current, _utc_now()) and _pid_alive(current["pid"], current.get("process_identity", ""))

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
            "process_identity": _process_identity(os.getpid()),
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
            _durable_replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink(missing_ok=True)
            raise
        self._acquired_at = now

    def release(self) -> bool:
        target = runtime_dir(self._db_path) / f"{self._token}.json"
        current = _read_runtime_holder(target)
        owns_marker = current is not None and current["token"] == self._token
        released = owns_marker
        try:
            if owns_marker:
                target.unlink(missing_ok=True)
        except OSError:
            released = False
        with contextlib.suppress(OSError):
            target.parent.rmdir()
        key = str(self._db_path)
        entry = _LOCAL_SHARED_GUARDS.get(key)
        if entry is not None:
            guard, count = entry
            if count <= 1:
                _LOCAL_SHARED_GUARDS.pop(key, None)
                guard.release()
            else:
                _LOCAL_SHARED_GUARDS[key] = (guard, count - 1)
        return released

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
    _assert_database_fence(context)
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
