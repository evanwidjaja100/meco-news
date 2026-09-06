"""Offline manifested migration runner (closure plan C2.2b).

C2.2 migrates only under the exclusive maintenance guard. This module is the
offline half: a pre-migration artifact (backup plus manifest) created and
verified before BEGIN, and a fenced runner that applies the immutable catalog
from migrations.py over a raw SQLite connection.

The public migrate command stays disabled until C2.2e wires this runner to
audited CLI grammar. StateStore still refuses migration-required opens, and
create_backup still refuses them (it opens StateStore writable), so this
module never auto-migrates behind a live runtime: callers must first hold a
live MaintenanceContext for the same database path and scope.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, UTC
from hashlib import sha256
from pathlib import Path
from typing import Any

from .maintenance import MaintenanceContext, assert_maintenance_fence
from .migrations import CURRENT_SCHEMA_VERSION, ledger_contiguity_issue, migration_checksum, verify_catalog
from .storage import (
    StateError,
    _adopt_legacy_rows,
    _apply_migration_sql,
    _apply_schema_to,
    _ledger_versions,
    _record_migration_to,
)

MANIFEST_SUFFIX = ".manifest.json"
PRE_MIGRATE_MARKER = ".pre-migrate-"

# Mirrors the completeness check in StateStore._ensure_schema: a database is
# current only when its ledger is exactly 1..CURRENT with valid checksums,
# every v2 table survived, and the v3 deliveries column is present.
REQUIRED_CURRENT_TABLES = frozenset(
    {
        "run_leases",
        "deliveries",
        "delivery_attempts",
        "delivery_items",
        "outbox_chunks",
        "article_history",
        "source_results",
        "delivery_resolutions",
    }
)


@dataclass(frozen=True, slots=True)
class MigrationBackup:
    """Pre-migration artifact preserved for rollback (C2.2)."""

    backup: Path
    manifest: Path
    backup_id: str
    db_sha256: str
    backup_sha256: str
    integrity: str
    schema_version: int
    app_version: str
    config_hash: str
    created_at: str


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ro_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def _read_source_state(path: Path) -> tuple[str, int]:
    """Read integrity and ledger version without changing a byte.

    A missing schema_migrations table reports version 0 (legacy v1); anything
    unreadable raises StateError before any artifact file is created.
    """
    try:
        connection = _ro_connection(path)
    except sqlite3.Error as exc:
        raise StateError(f"migration source cannot be opened read-only: {exc}") from exc
    try:
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.Error as exc:
            raise StateError(f"migration source integrity probe failed: {exc}") from exc
        if integrity != "ok":
            raise StateError(f"migration source integrity is not ok: {integrity}")
        try:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as exc:
            raise StateError(f"migration source schema probe failed: {exc}") from exc
        if "schema_migrations" not in tables:
            return integrity, 0
        try:
            rows = connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
        except sqlite3.Error as exc:
            raise StateError(f"migration ledger cannot be read: {exc}") from exc
        versions = _ledger_versions(rows)
        return integrity, (versions[-1] if versions else 0)
    finally:
        connection.close()


def create_migration_manifest(db_path: str | Path, *, app_version: str, config_hash: str) -> MigrationBackup:
    """Copy the source database and describe it in a manifest; change nothing.

    Reads the source read-only (so legacy migration-required databases are
    accepted and garbage fails closed), copies it with the SQLite backup API,
    verifies the copy, then writes the manifest. The manifest records the
    backup ID, source and backup SHA-256, UTC time, integrity, schema and
    application versions, and the already-hashed config identity.
    """
    source = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(f"migration source does not exist: {source}")
    db_sha = _sha256_file(source)
    integrity, schema_version = _read_source_state(source)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = source.with_name(f"{source.name}.pre-migrate-{stamp}.bak")
    counter = 1
    while backup.exists() or backup.with_suffix(backup.suffix + MANIFEST_SUFFIX).exists():
        backup = source.with_name(f"{source.name}.pre-migrate-{stamp}-{counter}.bak")
        counter += 1
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        origin = _ro_connection(source)
    except sqlite3.Error as exc:
        raise StateError(f"migration source cannot be opened read-only: {exc}") from exc
    destination = sqlite3.connect(backup)
    try:
        with destination:
            origin.backup(destination)
    except sqlite3.Error as exc:
        destination.close()
        origin.close()
        backup.unlink(missing_ok=True)
        raise StateError(f"pre-migration backup failed: {exc}") from exc
    finally:
        destination.close()
        origin.close()
    try:
        check = _ro_connection(backup)
    except sqlite3.Error as exc:
        backup.unlink(missing_ok=True)
        raise StateError(f"pre-migration backup cannot be opened read-only: {exc}") from exc
    try:
        backup_integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.Error as exc:
        check.close()
        backup.unlink(missing_ok=True)
        raise StateError(f"pre-migration backup integrity probe failed: {exc}") from exc
    finally:
        check.close()
    if backup_integrity != "ok":
        backup.unlink(missing_ok=True)
        raise StateError(f"pre-migration backup integrity is not ok: {backup_integrity}")
    backup_sha = _sha256_file(backup)
    os.chmod(backup, 0o600)
    manifest = backup.with_suffix(backup.suffix + MANIFEST_SUFFIX)
    payload: dict[str, Any] = {
        "backup_id": uuid.uuid4().hex,
        "database": source.name,
        "backup": backup.name,
        "db_sha256": db_sha,
        "backup_sha256": backup_sha,
        "integrity": integrity,
        "schema_version": schema_version,
        "app_version": app_version,
        "config_hash": config_hash,
        "created_at": datetime.now(UTC).isoformat(),
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(manifest, 0o600)
    return MigrationBackup(
        backup=backup,
        manifest=manifest,
        backup_id=str(payload["backup_id"]),
        db_sha256=db_sha,
        backup_sha256=backup_sha,
        integrity=integrity,
        schema_version=schema_version,
        app_version=app_version,
        config_hash=config_hash,
        created_at=str(payload["created_at"]),
    )


def verify_migration_manifest(db_path: str | Path, manifest: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Re-verify a manifest against live bytes; fail closed on any mismatch.

    Must be called after create_migration_manifest and before BEGIN. A missing
    file, unreadable manifest, changed source, failed integrity, missing
    backup, or checksum mismatch raises before any schema change.
    """
    source = Path(db_path)
    if isinstance(manifest, Mapping):
        data: dict[str, Any] = dict(manifest)
        manifest_path: Path | None = None
    else:
        manifest_path = Path(manifest)
        if not manifest_path.is_file():
            raise FileNotFoundError(f"migration manifest does not exist: {manifest_path}")
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StateError(f"migration manifest cannot be parsed: {exc}") from exc
        if not isinstance(parsed, dict):
            raise StateError("migration manifest is not a JSON object")
        data = dict(parsed)
    for field in (
        "backup_id",
        "database",
        "backup",
        "db_sha256",
        "backup_sha256",
        "integrity",
        "schema_version",
        "app_version",
        "config_hash",
        "created_at",
    ):
        if field not in data:
            raise StateError(f"migration manifest is missing {field!r}")
    if data["integrity"] != "ok":
        raise StateError("migration manifest integrity is not ok")
    if not source.is_file():
        raise FileNotFoundError(f"migration source does not exist: {source}")
    if _sha256_file(source) != data["db_sha256"]:
        raise StateError("migration source changed since its manifest was created")
    integrity, _ = _read_source_state(source)
    if integrity != "ok":
        raise StateError(f"migration source integrity is not ok: {integrity}")
    backup_name = str(data["backup"])
    backup_path = (manifest_path.parent / backup_name) if manifest_path is not None else (source.parent / backup_name)
    if not backup_path.is_file():
        raise FileNotFoundError(f"pre-migration backup is missing: {backup_path}")
    if _sha256_file(backup_path) != data["backup_sha256"]:
        raise StateError("pre-migration backup checksum mismatch")
    return data


def _is_current_and_complete(path: Path) -> bool:
    """Report whether a database is already at the supported schema.

    Only an exact 1..CURRENT ledger with valid checksums, every required
    table, and the v3 deliveries column counts as current; anything else
    needs the manifested migration path. Unreadable bytes raise StateError.
    """
    try:
        connection = _ro_connection(path)
    except sqlite3.Error as exc:
        raise StateError(f"migration source cannot be opened read-only: {exc}") from exc
    try:
        try:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as exc:
            raise StateError(f"migration source schema probe failed: {exc}") from exc
        if "schema_migrations" not in tables:
            return False
        try:
            rows = connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
        except sqlite3.Error as exc:
            raise StateError(f"migration ledger cannot be read: {exc}") from exc
        versions = _ledger_versions(rows)
        if versions != list(range(1, CURRENT_SCHEMA_VERSION + 1)):
            return False
        for row in rows:
            if int(row[0]) <= CURRENT_SCHEMA_VERSION and str(row[1]) != migration_checksum(int(row[0])):
                return False
        if not REQUIRED_CURRENT_TABLES.issubset(tables):
            return False
        try:
            columns = {str(item[1]) for item in connection.execute("PRAGMA table_info(deliveries)").fetchall()}
        except sqlite3.Error as exc:
            raise StateError(f"migration source column probe failed: {exc}") from exc
        return "target_snapshot" in columns
    finally:
        connection.close()


def run_guarded_migrations(
    db_path: str | Path, *, context: MaintenanceContext, app_version: str, config_hash: str
) -> int:
    """Apply pending catalog migrations under a live exclusive guard.

    Requires the fence first (wrong path, wrong scope, or a missing, stale,
    or superseded hold raises MaintenanceError with no state change), then
    returns 0 without writing any file when the database is already current,
    otherwise manifests, verifies, and migrates atomically. A current
    database applies nothing and writes nothing; any failure before commit
    rolls back and leaves the source at its pre-migration bytes.
    """
    resolved = Path(db_path).resolve()
    assert_maintenance_fence(context, db_path=resolved)
    if not resolved.is_file():
        raise FileNotFoundError(f"migration source does not exist: {resolved}")
    if _is_current_and_complete(resolved):
        return 0
    artifact = create_migration_manifest(resolved, app_version=app_version, config_hash=config_hash)
    verify_migration_manifest(resolved, artifact.manifest)
    report = verify_catalog()
    if not report.ok:
        raise StateError(f"migration catalog is invalid: {'; '.join(report.issues)}")
    connection = sqlite3.connect(str(resolved), timeout=30.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise StateError("database integrity check failed before schema inspection")
        if "schema_migrations" not in tables:
            legacy = bool({"sent_articles", "runs"} & tables)
            if tables and not legacy:
                raise StateError("schema migration ledger is absent and no legacy v1 tables were found")
            connection.execute("BEGIN IMMEDIATE")
            try:
                _apply_schema_to(connection)
                if legacy:
                    _adopt_legacy_rows(connection)
                for version in range(1, CURRENT_SCHEMA_VERSION + 1):
                    _record_migration_to(connection, version, app_version)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            applied = CURRENT_SCHEMA_VERSION
        else:
            rows = connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
            versions = _ledger_versions(rows)
            contiguity = ledger_contiguity_issue(versions)
            if contiguity is not None:
                raise StateError(f"schema migration ledger is invalid: {contiguity}")
            for row in rows:
                if int(row[0]) <= CURRENT_SCHEMA_VERSION and str(row[1]) != migration_checksum(int(row[0])):
                    raise StateError(f"schema migration checksum mismatch at version {row[0]}")
            current = versions[-1]
            if current > CURRENT_SCHEMA_VERSION:
                raise StateError(
                    f"database schema {current} is newer than application schema {CURRENT_SCHEMA_VERSION}"
                )
            if current >= CURRENT_SCHEMA_VERSION:
                applied = 0
            else:
                pending = list(range(current + 1, CURRENT_SCHEMA_VERSION + 1))
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for version in pending:
                        _apply_migration_sql(connection, version)
                        _record_migration_to(connection, version, app_version)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                applied = len(pending)
        if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise StateError("database integrity check failed after migration")
        return applied
    finally:
        connection.close()


__all__ = [
    "MANIFEST_SUFFIX",
    "PRE_MIGRATE_MARKER",
    "REQUIRED_CURRENT_TABLES",
    "MigrationBackup",
    "create_migration_manifest",
    "run_guarded_migrations",
    "verify_migration_manifest",
]
