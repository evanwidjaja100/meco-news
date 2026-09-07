"""Operator-safe backup scheduling, retention, replication receipts, and pruning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
import contextlib
import json
import os
from pathlib import Path
import tempfile
import uuid
from typing import Any

from .backup import BackupArtifact, _load_manifest, _sha256, create_backup
from .maintenance import _pid_alive, _process_identity
from .storage import StateError, StateStore
from .maintenance import MaintenanceContext


class BackupBusy(StateError):
    """A backup job is already running for the configured state path."""


@dataclass(frozen=True, slots=True)
class BackupInventoryItem:
    database: Path
    manifest: Path
    created_at: datetime
    backup_id: str
    sha256: str
    verified: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    keep: tuple[Path, ...]
    delete: tuple[Path, ...]
    invalid: tuple[Path, ...]
    latest_verified: Path | None
    referenced: tuple[Path, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "keep": [str(path) for path in self.keep],
            "delete": [str(path) for path in self.delete],
            "invalid": [str(path) for path in self.invalid],
            "latest_verified": str(self.latest_verified) if self.latest_verified else None,
            "referenced": [str(path) for path in self.referenced],
        }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                pass
            else:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(temporary).unlink(missing_ok=True)
        raise


class BackupJobLock:
    """Cross-process non-overlap lock for scheduled backup jobs."""

    def __init__(self, path: str | Path, *, owner: str | None = None) -> None:
        self.path = Path(path).resolve()
        self.owner = owner or f"backup:{os.getpid()}:{uuid.uuid4().hex}"
        self._held = False

    def __enter__(self) -> BackupJobLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "owner": self.owner,
            "pid": os.getpid(),
            "process_identity": _process_identity(os.getpid()),
            "started_at": datetime.now(UTC).isoformat(),
        }
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            if not isinstance(exc, FileExistsError) and (self.path.is_symlink() or not self.path.is_file()):
                raise BackupBusy("backup lock path is not a regular file") from exc
            if not isinstance(exc, FileExistsError):
                raise
            expected: bytes = b""
            with contextlib.suppress(OSError):
                expected = self.path.read_bytes()
            existing: dict[str, Any] = {}
            with contextlib.suppress(OSError, ValueError, TypeError, json.JSONDecodeError):
                existing = json.loads(self.path.read_text(encoding="utf-8"))
            pid = existing.get("pid") if isinstance(existing, dict) else None
            identity = existing.get("process_identity", "") if isinstance(existing, dict) else ""
            if _pid_alive(pid, identity):
                raise BackupBusy(f"backup job is already held by {existing.get('owner', 'unknown')}") from exc
            # A stale marker is recoverable only when it is a regular file in
            # the exact lock location.  Do not follow links or remove a
            # directory supplied as a lock path.
            if self.path.is_symlink() or not self.path.is_file():
                raise BackupBusy("backup lock is not a recoverable regular file") from exc
            # Compare-and-swap on the stale marker: only remove the exact
            # bytes inspected above. A contender that published or recovered
            # after our read owns the lock now; unlinking its marker would
            # hand both contenders a held lock.
            try:
                current = self.path.read_bytes()
            except OSError:
                current = b""
            if current != expected:
                raise BackupBusy("backup lock changed while recovering a stale marker") from exc
            try:
                self.path.unlink()
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except (FileNotFoundError, FileExistsError) as race:
                # Another contender either recovered the stale marker first
                # or published a fresh marker after our inspection.  Never
                # leak a raw filesystem race as a successful lock attempt.
                raise BackupBusy("backup lock changed while recovering a stale marker") from race
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            self._held = True
            return self
        except BaseException:
            # If fdopen itself fails, ownership of the raw descriptor has not
            # transferred to a file object.  Close it before unlinking so the
            # failed lock publication is recoverable on Windows as well as
            # POSIX.
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                self.path.unlink(missing_ok=True)
            raise

    def __exit__(self, *_: object) -> None:
        if not self._held:
            return
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            current = {}
        if isinstance(current, dict) and current.get("owner") == self.owner:
            with contextlib.suppress(OSError):
                self.path.unlink(missing_ok=True)
        self._held = False


def inventory_backups(directory: str | Path) -> tuple[list[BackupInventoryItem], list[Path]]:
    """Verify every candidate and refuse to classify malformed artifacts as valid."""

    root = Path(directory).resolve()
    if not root.is_dir():
        return [], []
    valid: list[BackupInventoryItem] = []
    invalid: list[Path] = []
    for database in sorted(root.glob("*.db")):
        manifest = database.with_suffix(database.suffix + ".manifest.json")
        if database.is_symlink() or manifest.is_symlink():
            invalid.append(database)
            continue
        try:
            data = _load_manifest(manifest, database)
            created_at = datetime.fromisoformat(str(data["created_at"]).replace("Z", "+00:00")).astimezone(UTC)
            valid.append(
                BackupInventoryItem(
                    database=database,
                    manifest=manifest,
                    created_at=created_at,
                    backup_id=str(data["backup_id"]),
                    sha256=str(data["sha256"]),
                    verified=True,
                )
            )
        except (OSError, ValueError, StateError, KeyError, TypeError):
            invalid.append(database)
    valid.sort(key=lambda item: (item.created_at, item.backup_id), reverse=True)
    return valid, invalid


def _calendar_key(value: datetime, period: str) -> str:
    if period == "day":
        return value.date().isoformat()
    if period == "week":
        year, week, _ = value.isocalendar()
        return f"{year:04d}-W{week:02d}"
    return f"{value.year:04d}-{value.month:02d}"


def build_retention_plan(
    directory: str | Path,
    *,
    referenced: set[str | Path] | None = None,
    daily: int = 7,
    weekly: int = 4,
    monthly: int = 12,
) -> RetentionPlan:
    """Build a preview-only retention plan from verified manifests.

    Each calendar period keeps its newest verified artifact.  The newest
    verified artifact overall and explicitly referenced artifacts are always
    retained.  Invalid or manifest-less files are never deleted automatically.
    """

    valid, invalid = inventory_backups(directory)
    if min(daily, weekly, monthly) < 1:
        raise ValueError("retention periods must be positive")
    keep: set[Path] = set()
    by_period: dict[str, set[str]] = {"day": set(), "week": set(), "month": set()}
    for item in valid:
        for period, limit in (("day", daily), ("week", weekly), ("month", monthly)):
            period_key = _calendar_key(item.created_at, period)
            if len(by_period[period]) < limit and period_key not in by_period[period]:
                by_period[period].add(period_key)
                keep.add(item.database)
    latest = valid[0].database if valid else None
    if latest:
        keep.add(latest)
    root = Path(directory).resolve()
    referenced_paths: set[Path] = set()
    for value in referenced or set():
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if candidate.parent == root:
            referenced_paths.add(candidate)
            keep.add(candidate)
    delete = tuple(item.database for item in valid if item.database not in keep)
    keep_ordered = tuple(item.database for item in valid if item.database in keep)
    return RetentionPlan(keep_ordered, delete, tuple(invalid), latest, tuple(sorted(referenced_paths)))


def apply_retention_plan(plan: RetentionPlan, *, directory: str | Path) -> dict[str, Any]:
    """Delete only the exact verified artifacts named by a prior plan."""

    root = Path(directory).resolve()
    deleted: list[str] = []
    for database in plan.delete:
        candidate = Path(database)
        if candidate.parent.resolve() != root or candidate.suffix.casefold() != ".db" or candidate.is_symlink():
            raise StateError(f"retention target is outside the backup directory: {candidate}")
        target = candidate.resolve()
        if target.parent != root:
            raise StateError(f"retention target is outside the backup directory: {target}")
        manifest = target.with_suffix(target.suffix + ".manifest.json")
        # Reverify immediately before deletion so a preview cannot delete a
        # file that was replaced or tampered with in the meantime.
        _load_manifest(manifest, target)
        target.unlink()
        manifest.unlink()
        deleted.append(str(target))
    return {"deleted": deleted, "kept": [str(path) for path in plan.keep], "invalid": [str(path) for path in plan.invalid]}


def write_replication_receipt(
    artifact: BackupArtifact,
    receipt_path: str | Path,
    *,
    destination: str,
    transport_receipt: str = "",
    confirmed: bool = False,
    now: datetime | None = None,
) -> Path:
    """Record off-host replication evidence without pretending to upload data."""

    if not destination.strip():
        raise ValueError("replication destination is required")
    current_hash = _sha256(artifact.database)
    if current_hash != artifact.sha256:
        raise StateError("backup changed before replication receipt")
    payload = {
        "receipt_id": uuid.uuid4().hex,
        "backup": artifact.database.name,
        "manifest": artifact.manifest.name,
        "sha256": current_hash,
        "destination": destination[:256],
        "transport_receipt": transport_receipt[:512],
        "state": "confirmed" if confirmed and transport_receipt else "pending_external_confirmation",
        "created_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
    }
    path = Path(receipt_path).resolve()
    _atomic_json(path, payload)
    return path


def scheduled_backup(
    state_path: str | Path,
    output: str | Path,
    *,
    lock_path: str | Path | None = None,
    receipt_path: str | Path | None = None,
    config_hash: str = "",
) -> tuple[BackupArtifact, dict[str, Any]]:
    """Run one verified, non-overlapping backup and publish a local receipt."""

    state = Path(state_path).resolve()
    lock = Path(lock_path).resolve() if lock_path else Path(f"{state}.backup.lock")
    with BackupJobLock(lock):
        artifact = create_backup(state, output, config_hash=config_hash)
        manifest = _load_manifest(artifact.manifest, artifact.database)
        receipt = {
            "receipt_id": uuid.uuid4().hex,
            "state": "verified",
            "backup": str(artifact.database),
            "manifest": str(artifact.manifest),
            "backup_id": manifest["backup_id"],
            "sha256": artifact.sha256,
            "verified_at": datetime.now(UTC).isoformat(),
        }
        if receipt_path:
            receipt_file = Path(receipt_path).resolve()
            _atomic_json(receipt_file, receipt)
            receipt["receipt_file"] = str(receipt_file)
        return artifact, receipt


def prune_state_history(
    state_path: str | Path,
    *,
    apply: bool = False,
    attempt_retention_days: int = 90,
    article_retention_days: int = 365,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the storage retention operation under the exclusive guard."""

    target = Path(state_path).resolve()
    with MaintenanceContext.acquire(target, owner=f"prune:{os.getpid()}:{uuid.uuid4().hex}", scope="maintenance") as context, StateStore(
        target, maintenance_context=context
    ) as store:
        return store.prune_history(
            now=now,
            attempt_retention_days=attempt_retention_days,
            article_retention_days=article_retention_days,
            apply=apply,
        )


__all__ = [
    "BackupBusy",
    "BackupInventoryItem",
    "BackupJobLock",
    "RetentionPlan",
    "apply_retention_plan",
    "build_retention_plan",
    "inventory_backups",
    "prune_state_history",
    "scheduled_backup",
    "write_replication_receipt",
]
