"""Read-only state-database inspection for preflight (closure plan C1.2).

Implements the ADR-C04 interim inspection contract: every state path
classifies as exactly one of missing, migration_required, compatible,
newer_incompatible, malformed, or corrupt.

Read-only by construction: the live database is only ever opened through a
SQLite ``mode=ro`` URI connection, so inspection cannot create WAL/SHM/
journal sidecars, migrate schema, or change a single byte. It also verifies
the exact supported Python range and probes directory WAL capability with a
throwaway file that never touches the live database.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .migrations import CURRENT_SCHEMA_VERSION, ledger_contiguity_issue, migration_checksum

SchemaClassification = Literal[
    "missing",
    "migration_required",
    "compatible",
    "newer_incompatible",
    "malformed",
    "corrupt",
]

MIN_PYTHON = (3, 12)
MAX_PYTHON_EXCLUSIVE = (3, 15)
SUPPORTED_PYTHON_RANGE = ">=3.12,<3.15"

REQUIRED_TABLES = frozenset(
    {
        "schema_migrations",
        "sent_articles",
        "runs",
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

REQUIRED_INDEXES = frozenset(
    {
        "idx_article_history_url",
        "idx_article_history_title",
        "idx_deliveries_date_state",
        "idx_chunks_due",
        "idx_source_results_delivery",
    }
)

LEGACY_TABLES = frozenset({"sent_articles", "runs"})


@dataclass(frozen=True, slots=True)
class InspectionResult:
    classification: SchemaClassification
    schema_version: int
    integrity: str
    detail: str


@dataclass(frozen=True, slots=True)
class WalProbeResult:
    ok: bool
    journal_mode: str
    reason: str


def python_in_range(info: tuple[int, ...]) -> bool:
    major, minor = int(info[0]), int(info[1])
    return (MIN_PYTHON[0], MIN_PYTHON[1]) <= (major, minor) < (MAX_PYTHON_EXCLUSIVE[0], MAX_PYTHON_EXCLUSIVE[1])


def check_python_version(info: tuple[int, ...] | None = None) -> tuple[bool, str]:
    resolved = tuple(sys.version_info[:2]) if info is None else tuple(info)
    padded = tuple(list(resolved) + [0] * (3 - len(resolved)))
    version = ".".join(str(part) for part in padded[:3])
    return python_in_range(resolved), version


def _open_ro(target: Path) -> sqlite3.Connection:
    # immutable=1 keeps even the read path from creating -shm/-wal sidecars; any
    # staleness this risks (ignoring an uncheckpointed -wal) fails closed because
    # writers only ever move the ledger forward under a lease this same preflight
    # reports on, and a ledger that reads older than supported is non-ready.
    uri = f"file:{target.resolve().as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    return connection


def inspect_state(path: str | Path) -> InspectionResult:
    target = Path(path)
    if not target.exists():
        return InspectionResult("missing", 0, "not_yet_created", "state database does not exist yet")
    try:
        connection = _open_ro(target)
    except sqlite3.Error:
        return InspectionResult("corrupt", 0, "open_failed", "state database cannot be opened read-only")
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            return InspectionResult("corrupt", 0, integrity, f"integrity_check failed: {integrity}")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows: list[sqlite3.Row] = []
        if "schema_migrations" in tables:
            rows = list(connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall())
        if not rows:
            if LEGACY_TABLES & tables:
                return InspectionResult("migration_required", 0, "ok", "legacy v1 database without a migration ledger")
            return InspectionResult("malformed", 0, "ok", "migration ledger schema_migrations is absent or empty")
        versions: list[int] = []
        for row in rows:
            try:
                versions.append(int(row[0]))
            except (TypeError, ValueError):
                return InspectionResult("malformed", 0, "ok", "migration ledger holds a non-integer version")
            if versions[-1] <= CURRENT_SCHEMA_VERSION and row[1] != migration_checksum(versions[-1]):
                return InspectionResult("malformed", versions[-1], "ok", f"migration checksum mismatch at version {versions[-1]}")
        contiguity = ledger_contiguity_issue(versions)
        if contiguity is not None:
            return InspectionResult("malformed", versions[-1], "ok", f"migration ledger is invalid: {contiguity}")
        current = versions[-1]
        if current < CURRENT_SCHEMA_VERSION:
            return InspectionResult(
                "migration_required", current, "ok", f"schema version {current} predates supported {CURRENT_SCHEMA_VERSION}"
            )
        if current > CURRENT_SCHEMA_VERSION:
            return InspectionResult(
                "newer_incompatible",
                current,
                "ok",
                f"schema version {current} is newer than supported {CURRENT_SCHEMA_VERSION}",
            )
        missing_tables = sorted(REQUIRED_TABLES - tables)
        if missing_tables:
            return InspectionResult("malformed", current, "ok", f"structural signature is missing table(s): {', '.join(missing_tables)}")
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        missing_indexes = sorted(REQUIRED_INDEXES - indexes)
        if missing_indexes:
            return InspectionResult("malformed", current, "ok", f"structural signature is missing index(es): {', '.join(missing_indexes)}")
        return InspectionResult("compatible", current, "ok", "schema version and structural signature match")
    except sqlite3.Error:
        return InspectionResult("corrupt", 0, "unreadable", "state database is not a readable SQLite database")
    finally:
        connection.close()


def wal_mode_supported(mode: str) -> bool:
    return mode.strip().lower() == "wal"


def probe_wal_capability(directory: str | Path) -> WalProbeResult:
    target = Path(directory)
    if not target.is_dir():
        return WalProbeResult(False, "", "state directory is absent")
    try:
        fd, name = tempfile.mkstemp(prefix=".wal-probe-", suffix=".db", dir=target)
    except OSError:
        return WalProbeResult(False, "", "state directory is not writable")
    os.close(fd)
    probe = Path(name)
    try:
        connection = sqlite3.connect(probe, timeout=2.0)
        try:
            mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        finally:
            connection.close()
        if not wal_mode_supported(mode):
            return WalProbeResult(False, mode.lower(), "WAL journal mode is unavailable")
        return WalProbeResult(True, mode.lower(), "ok")
    except sqlite3.Error:
        return WalProbeResult(False, "", "WAL probe database failed")
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            with contextlib.suppress(OSError):
                Path(str(probe) + suffix).unlink(missing_ok=True)


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "LEGACY_TABLES",
    "MAX_PYTHON_EXCLUSIVE",
    "MIN_PYTHON",
    "REQUIRED_INDEXES",
    "REQUIRED_TABLES",
    "SUPPORTED_PYTHON_RANGE",
    "InspectionResult",
    "SchemaClassification",
    "WalProbeResult",
    "check_python_version",
    "inspect_state",
    "probe_wal_capability",
    "python_in_range",
    "wal_mode_supported",
]
