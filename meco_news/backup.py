"""Online SQLite backup, checksum manifest, and verified restore helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from hashlib import sha256
import json
from pathlib import Path
import os
import tempfile
from typing import Any

from . import __version__
from .storage import StateError, StateStore


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    database: Path
    manifest: Path
    sha256: str


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_backup(
    state_path: str | Path,
    output: str | Path,
    *,
    config_hash: str = "",
) -> BackupArtifact:
    state = Path(state_path)
    output_path = Path(output)
    if output_path.suffix.casefold() in {".db", ".sqlite", ".backup"}:
        database = output_path
        database.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path.mkdir(parents=True, exist_ok=True)
        database = output_path / f"meco_news-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
    with StateStore(state) as store:
        store.backup_to(database)
        with StateStore(database, readonly=True) as verified:
            schema_version = verified.schema_version
            integrity = verified.integrity_check()
    checksum = _sha256(database)
    os.chmod(database, 0o600)
    manifest = database.with_suffix(database.suffix + ".manifest.json")
    payload: dict[str, Any] = {
        "database": database.name,
        "sha256": checksum,
        "schema_version": schema_version,
        "application_version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "config_hash": config_hash,
        "integrity": integrity,
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(manifest, 0o600)
    return BackupArtifact(database, manifest, checksum)


def restore_backup(
    backup_path: str | Path,
    target_path: str | Path,
    *,
    manifest_path: str | Path | None = None,
) -> Path:
    backup = Path(backup_path)
    target = Path(target_path)
    manifest = Path(manifest_path) if manifest_path else backup.with_suffix(backup.suffix + ".manifest.json")
    if not backup.is_file() or not manifest.is_file():
        raise FileNotFoundError("backup database or manifest is missing")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if metadata.get("sha256") != _sha256(backup):
        raise ValueError("backup checksum mismatch")
    if metadata.get("integrity") not in {None, "ok"}:
        raise ValueError("backup manifest integrity is not ok")
    with StateStore(backup, readonly=True) as store:
        if store.integrity_check() != "ok":
            raise ValueError("backup integrity check failed")
        if store.schema_version < 2:
            raise ValueError("backup schema is not supported")
        if metadata.get("schema_version") not in {None, store.schema_version}:
            raise ValueError("backup schema version does not match its manifest")
    if target.exists():
        # Replacing a live target would discard the scheduler's durable view
        # while it may still be sending.  Require the operator to stop it or
        # wait for the lease to expire before an atomic restore.
        with StateStore(target, readonly=True) as current:
            for scope in ("delivery", "scheduler"):
                lease = current.lease_info(scope)
                if not lease:
                    continue
                try:
                    active = datetime.fromisoformat(lease["expires_at"]).astimezone(UTC) > datetime.now(UTC)
                except (KeyError, TypeError, ValueError) as exc:
                    raise StateError("target lease metadata is invalid; restore refused") from exc
                if active:
                    raise StateError(f"cannot restore over active {scope} lease")
    target.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = target.stat().st_mode & 0o777 if target.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore", dir=target.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with StateStore(backup, readonly=True) as source:
            source.backup_to(temp_path)
        with StateStore(temp_path, readonly=True) as restored:
            if restored.integrity_check() != "ok":
                raise ValueError("restored database integrity check failed")
        if target.exists():
            previous = target.with_suffix(target.suffix + ".pre-restore.bak")
            if previous.exists():
                previous = target.with_suffix(target.suffix + f".pre-restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.bak")
            os.replace(target, previous)
        os.replace(temp_path, target)
        if previous_mode is not None:
            os.chmod(target, previous_mode)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return target
