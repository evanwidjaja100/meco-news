"""Verified WAL-consistent SQLite backup and fail-closed restore helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from hashlib import sha256
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import re
from typing import Any
import uuid

from . import __version__
from .maintenance import MaintenanceContext
from .migrations import CURRENT_SCHEMA_VERSION
from .storage import StateError, StateStore


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    database: Path
    manifest: Path
    sha256: str


_BACKUP_MANIFEST_FIELDS = {
    "backup_id",
    "database",
    "source_database",
    "sha256",
    "schema_version",
    "application_version",
    "created_at",
    "config_hash",
    "integrity",
}


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _path_exists(path: Path) -> bool:
    """Return true for existing paths, including broken symlinks."""

    return path.exists() or path.is_symlink()


def _reserve_file(path: Path) -> bool:
    """Atomically reserve a publication path with a private placeholder."""

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _sqlite_sidecars(path: Path) -> tuple[Path, ...]:
    """Return SQLite's journal/WAL companions for a database path."""

    return tuple(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))


def _new_backup_path(output: Path) -> Path:
    if output.suffix.casefold() in {".db", ".sqlite", ".backup"}:
        output.parent.mkdir(parents=True, exist_ok=True)
        manifest = output.with_suffix(output.suffix + ".manifest.json")
        if _path_exists(output) or _path_exists(manifest):
            raise FileExistsError(f"backup destination already exists: {output}")
        if not _reserve_file(output):
            raise FileExistsError(f"backup destination already exists: {output}")
        return output
    output.mkdir(parents=True, exist_ok=True)
    # The UUID makes collisions unlikely; O_EXCL makes the publication safe
    # even when two independent backup jobs choose at the same instant.
    for _ in range(5):
        candidate = output / f"meco_news-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}.db"
        manifest = candidate.with_suffix(candidate.suffix + ".manifest.json")
        if _path_exists(manifest):
            continue
        if _reserve_file(candidate):
            return candidate
    raise StateError("could not reserve a unique backup destination")


def _write_manifest_atomic(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(temporary).unlink(missing_ok=True)
        raise


def create_backup(
    state_path: str | Path,
    output: str | Path,
    *,
    config_hash: str = "",
) -> BackupArtifact:
    """Publish one complete, uniquely named, verified recovery artifact."""

    state = Path(state_path).resolve()
    if not state.is_file():
        raise FileNotFoundError(f"backup source does not exist: {state}")
    database = _new_backup_path(Path(output).resolve())
    manifest = database.with_suffix(database.suffix + ".manifest.json")
    if not _reserve_file(manifest):
        with contextlib.suppress(OSError):
            database.unlink(missing_ok=True)
        raise FileExistsError(f"backup manifest destination already exists: {manifest}")
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{database.name}.", suffix=".tmp", dir=database.parent)
    os.close(temp_fd)
    temporary = Path(temp_name)
    published_manifest = False
    try:
        with StateStore(state, readonly=True) as source:
            if source.schema_version != CURRENT_SCHEMA_VERSION:
                raise StateError(
                    f"backup source schema {source.schema_version} is not the supported schema {CURRENT_SCHEMA_VERSION}"
                )
            if source.integrity_check() != "ok":
                raise StateError("backup source integrity check failed")
            source.backup_to(temporary)

        with StateStore(temporary, readonly=True, offline=True) as verified:
            integrity = verified.integrity_check()
            schema_version = verified.schema_version
            if integrity != "ok":
                raise StateError("backup artifact integrity check failed")
            if schema_version != CURRENT_SCHEMA_VERSION:
                raise StateError("backup artifact schema does not match the current schema")
        checksum = _sha256(temporary)
        _fsync_file(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, database)
        _fsync_directory(database.parent)
        payload: dict[str, Any] = {
            "backup_id": uuid.uuid4().hex,
            "database": database.name,
            "source_database": state.name,
            "sha256": checksum,
            "schema_version": schema_version,
            "application_version": __version__,
            "created_at": datetime.now(UTC).isoformat(),
            "config_hash": config_hash,
            "integrity": integrity,
        }
        _write_manifest_atomic(manifest, payload)
        published_manifest = True
        return BackupArtifact(database, manifest, checksum)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        if not published_manifest:
            with contextlib.suppress(OSError):
                database.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                manifest.unlink(missing_ok=True)


def _load_manifest(manifest: Path, backup: Path) -> dict[str, Any]:
    if not manifest.is_file():
        raise FileNotFoundError(f"backup manifest is missing: {manifest}")
    try:
        data = json.loads(
            manifest.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, ValueError) as exc:
        raise StateError(f"backup manifest cannot be parsed: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError("backup manifest is not a JSON object")
    missing = sorted(_BACKUP_MANIFEST_FIELDS - set(data))
    unknown = sorted(set(data) - _BACKUP_MANIFEST_FIELDS)
    if missing:
        raise StateError("backup manifest is missing required field(s): " + ", ".join(missing))
    if unknown:
        raise StateError("backup manifest contains unsupported field(s): " + ", ".join(unknown))
    if not backup.is_file():
        raise FileNotFoundError(f"backup database is missing: {backup}")
    if data["database"] != backup.name or not _safe_filename(data["database"]):
        raise StateError("backup manifest database name does not match the artifact")
    if not isinstance(data["source_database"], str) or not _safe_filename(data["source_database"]):
        raise StateError("backup manifest source database name is invalid")
    if not isinstance(data["backup_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", data["backup_id"]):
        raise StateError("backup manifest backup_id is invalid")
    if not isinstance(data["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
        raise StateError("backup manifest checksum is invalid")
    if data["sha256"] != _sha256(backup):
        raise StateError("backup checksum mismatch")
    if data["integrity"] != "ok":
        raise StateError("backup manifest integrity is not ok")
    if isinstance(data["schema_version"], bool) or data["schema_version"] != CURRENT_SCHEMA_VERSION:
        raise StateError("backup manifest schema is not supported")
    for field in ("application_version", "created_at", "config_hash"):
        if not isinstance(data[field], str) or len(data[field]) > 2048:
            raise StateError(f"backup manifest {field} is invalid")
    try:
        created_at = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateError("backup manifest created_at is invalid") from exc
    if created_at.tzinfo is None:
        raise StateError("backup manifest created_at must include a timezone")
    return data


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _safe_filename(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value not in {".", ".."} and Path(value).name == value and not any(
        char in value for char in ("/", "\\", "\x00")
    )


def _target_recovery_state(target: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return target leases and all unsafely replaceable work."""

    if not target.is_file():
        return [], []
    try:
        connection = sqlite3.connect(f"file:{target.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        if target.stat().st_size == 0:
            return [], []
        raise StateError(f"target database cannot be inspected safely: {exc}") from exc
    try:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            if target.stat().st_size == 0:
                return [], []
            raise StateError("target database has no readable schema; restore refused")
        if "run_leases" not in tables or "outbox_chunks" not in tables or "deliveries" not in tables:
            raise StateError("target database schema is incomplete; restore refused")
        leases = [dict(row) for row in connection.execute("SELECT * FROM run_leases").fetchall()]
        unresolved = [
            dict(row)
            for row in connection.execute(
                "SELECT chunk_id,delivery_id,state,payload_hash FROM outbox_chunks WHERE state <> 'sent'"
            ).fetchall()
        ]
        unresolved.extend(
            dict(row)
            for row in connection.execute(
                "SELECT delivery_id,NULL AS chunk_id,state,'' AS payload_hash FROM deliveries "
                "WHERE state NOT IN ('completed','completed_empty')"
            ).fetchall()
        )
        return leases, unresolved
    except sqlite3.Error as exc:
        raise StateError(f"target recovery state cannot be read safely: {exc}") from exc
    finally:
        connection.close()


def _active_leases(leases: list[dict[str, Any]]) -> list[str]:
    active: list[str] = []
    now = datetime.now(UTC)
    for lease in leases:
        try:
            if datetime.fromisoformat(str(lease["expires_at"])).astimezone(UTC) > now:
                active.append(str(lease.get("scope", "unknown")))
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError("target lease metadata is invalid; restore refused") from exc
    return active


def _restore_previous_path(target: Path) -> Path:
    """Choose a collision-free rollback name, including SQLite sidecars."""

    candidate = target.with_suffix(target.suffix + ".pre-restore.bak")
    for _ in range(20):
        if not any(_path_exists(path) for path in (candidate, *_sqlite_sidecars(candidate))):
            return candidate
        candidate = target.with_suffix(
            target.suffix
            + f".pre-restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.bak"
        )
    raise StateError("could not reserve a rollback path for the existing database")


def _restore_orphan_sidecar_path(target: Path, suffix: str) -> Path:
    """Choose a non-pairing quarantine name for a sidecar without a database."""

    for _ in range(20):
        candidate = target.with_name(f"{target.name}.pre-restore-orphan-{uuid.uuid4().hex[:12]}{suffix}")
        if not _path_exists(candidate):
            return candidate
    raise StateError("could not reserve a quarantine path for an orphaned SQLite sidecar")


def _merge_post_backup_history(target: Path, restored: Path) -> int:
    """Preserve target sends as a recovery-only history generation."""

    if not target.is_file():
        return 0
    source_con = sqlite3.connect(f"file:{target.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0)
    source_con.row_factory = sqlite3.Row
    target_con = sqlite3.connect(restored)
    try:
        tables = {str(row[0]) for row in source_con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "article_history" not in tables:
            return 0
        rows = source_con.execute(
            "SELECT url_key,title_key,fingerprint,title,url,source,published_at,sent_at FROM article_history ORDER BY history_id"
        ).fetchall()
        if not rows:
            return 0
        known = {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in target_con.execute("SELECT url_key,title_key,fingerprint FROM article_history").fetchall()
        }
        additions = [row for row in rows if (str(row[0]), str(row[1]), str(row[2])) not in known]
        if not additions:
            return 0
        recovery_date = f"recovery-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC).isoformat()
        target_con.execute(
            "INSERT INTO deliveries(delivery_date,generation,kind,state,run_id,config_hash,started_at,prepared_at,completed_at,terminal_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (recovery_date, 0, "empty", "completed_empty", f"restore-{uuid.uuid4().hex}", "recovery", now, now, now, "post_backup_history_reconciled"),
        )
        delivery_id = int(target_con.execute("SELECT last_insert_rowid()").fetchone()[0])
        for row in additions:
            target_con.execute(
                "INSERT INTO article_history(url_key,title_key,fingerprint,delivery_id,chunk_id,title,url,source,published_at,sent_at) VALUES (?,?,?,?,NULL,?,?,?,?,?)",
                (row[0], row[1], row[2], delivery_id, row[3], row[4], row[5], row[6], row[7]),
            )
        target_con.commit()
        return len(additions)
    except sqlite3.Error as exc:
        target_con.rollback()
        raise StateError(f"post-backup history reconciliation failed: {exc}") from exc
    finally:
        source_con.close()
        target_con.close()


def restore_backup(
    backup_path: str | Path,
    target_path: str | Path,
    *,
    manifest_path: str | Path | None = None,
) -> Path:
    """Restore a verified artifact under an exclusive maintenance lifetime."""

    backup = Path(backup_path).resolve()
    target = Path(target_path).resolve()
    if backup == target:
        raise StateError("restore source and target must differ")
    if not backup.is_file():
        raise FileNotFoundError(f"backup database is missing: {backup}")
    manifest = Path(manifest_path).resolve() if manifest_path else backup.with_suffix(backup.suffix + ".manifest.json")
    _load_manifest(manifest, backup)
    with StateStore(backup, readonly=True, offline=True) as store:
        if store.integrity_check() != "ok":
            raise StateError("backup integrity check failed")
        if store.schema_version != CURRENT_SCHEMA_VERSION:
            raise StateError("backup schema is not supported")

    leases, unresolved = _target_recovery_state(target)
    active = _active_leases(leases)
    if active:
        raise StateError(f"cannot restore over active {active[0]} lease")
    if unresolved:
        raise StateError("cannot restore over unresolved target work; reconcile in-flight/ambiguous chunks first")

    target.parent.mkdir(parents=True, exist_ok=True)
    if _path_exists(target) and not target.is_file():
        raise StateError(f"restore target is not a regular database file: {target}")
    with MaintenanceContext.acquire(target, owner=f"restore:{os.getpid()}:{uuid.uuid4().hex}", scope="maintenance"):
        # Close the inspection/acquire race: a process may have published work
        # after the first read but before the exclusive guard was obtained.
        guarded_leases, guarded_unresolved = _target_recovery_state(target)
        guarded_active = _active_leases(guarded_leases)
        if guarded_active:
            raise StateError(f"cannot restore over active {guarded_active[0]} lease")
        if guarded_unresolved:
            raise StateError("cannot restore over unresolved target work; reconcile it before restore")
        temporary_fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore", dir=target.parent)
        os.close(temporary_fd)
        temporary = Path(temporary_name)
        previous: Path | None = None
        previous_mode = target.stat().st_mode & 0o777 if target.exists() else None
        moved_sidecars: list[tuple[Path, Path]] = []
        quarantined_sidecars: list[tuple[Path, Path]] = []
        installed = False
        try:
            with StateStore(backup, readonly=True, offline=True) as source:
                source.backup_to(temporary)
            with StateStore(temporary, readonly=True, offline=True) as restored:
                if restored.integrity_check() != "ok" or restored.schema_version != CURRENT_SCHEMA_VERSION:
                    raise StateError("restored database failed compatibility verification")
            _merge_post_backup_history(target, temporary)
            with StateStore(temporary, readonly=True, offline=True) as restored:
                if restored.integrity_check() != "ok":
                    raise StateError("reconciled restore failed integrity verification")
            _fsync_file(temporary)
            os.chmod(temporary, previous_mode or 0o600)
            if target.exists():
                previous = _restore_previous_path(target)
                os.replace(target, previous)
                for sidecar in _sqlite_sidecars(target):
                    if _path_exists(sidecar):
                        destination = Path(f"{previous}{sidecar.name[len(target.name):]}")
                        os.replace(sidecar, destination)
                        moved_sidecars.append((sidecar, destination))
            else:
                # A sidecar without its main database must never be left at
                # target: SQLite could attach it to the newly restored file.
                for sidecar in _sqlite_sidecars(target):
                    if _path_exists(sidecar):
                        suffix = sidecar.name[len(target.name) :]
                        destination = _restore_orphan_sidecar_path(target, suffix)
                        os.replace(sidecar, destination)
                        quarantined_sidecars.append((sidecar, destination))
            os.replace(temporary, target)
            installed = True
            _fsync_directory(target.parent)
        except BaseException:
            # Roll back the complete SQLite file set.  A main database moved
            # without its WAL/SHM/journal is not a safe rollback state.
            if installed and _path_exists(target):
                with contextlib.suppress(OSError):
                    target.unlink()
            for original_sidecar, previous_sidecar in reversed(moved_sidecars):
                if _path_exists(previous_sidecar) and not _path_exists(original_sidecar):
                    with contextlib.suppress(OSError):
                        os.replace(previous_sidecar, original_sidecar)
            for original_sidecar, quarantine_sidecar in reversed(quarantined_sidecars):
                if _path_exists(quarantine_sidecar) and not _path_exists(original_sidecar):
                    with contextlib.suppress(OSError):
                        os.replace(quarantine_sidecar, original_sidecar)
            if previous is not None and _path_exists(previous) and not _path_exists(target):
                with contextlib.suppress(OSError):
                    os.replace(previous, target)
            raise
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
    return target


__all__ = ["BackupArtifact", "create_backup", "restore_backup"]
