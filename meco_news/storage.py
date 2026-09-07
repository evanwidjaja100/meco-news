"""Versioned SQLite state, leases, immutable deliveries, and outbox chunks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from hashlib import sha256
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Any
from collections.abc import Iterable, Mapping, Sequence

from . import __version__
from .migrations import (
    CURRENT_SCHEMA_VERSION,
    LEGACY_FENCE_TRIGGERS,
    MIGRATION_SQL,
    SCHEMA_SQL,
    MigrationGuard,
    MigrationNotPermitted,
    ledger_contiguity_issue,
    migration_checksum,
    verify_catalog,
)
from .maintenance import MaintenanceBusy, MaintenanceContext, RuntimeContext, ensure_database_fence
from .maintenance import is_maintenance_held
from .observability import redact as _redact_log_value
from .models import NewsItem
from .urls import canonical_url


class StateError(RuntimeError):
    """A durable state operation could not be completed safely."""


class InvalidTransition(StateError):
    pass


class LeaseLost(StateError):
    pass


class DatabaseReadOnly(StateError):
    pass


class RetryNotDue(StateError):
    pass


MIGRATION_REQUIRED_MESSAGE = (
    "database schema requires migration (migration_required); normal startup never migrates, "
    "use the audited current-schema migrate command"
)


class MigrationRequiredError(StateError):
    """A state database needs migration and must not be auto-migrated (C2.1)."""


@dataclass(frozen=True, slots=True)
class LeaseAcquire:
    acquired: bool
    status: str
    owner_id: str
    expires_at: str = ""


@dataclass(frozen=True, slots=True)
class DeliveryInfo:
    delivery_id: int
    delivery_date: str
    generation: int
    kind: str
    state: str
    run_id: str
    config_hash: str
    target_snapshot: str = ""
    next_attempt_at: str = ""
    terminal_error: str = ""


@dataclass(frozen=True, slots=True)
class ChunkInfo:
    chunk_id: int
    delivery_id: int
    sequence: int
    payload: str
    payload_hash: str
    state: str
    attempt_count: int
    telegram_message_id: str = ""


def _utc(value: datetime | None = None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).isoformat()


def _sanitize_error(value: object, limit: int = 1000) -> str:
    cleaned = _redact_log_value(value, limit=limit)
    if not isinstance(cleaned, str):
        cleaned = " ".join(str(cleaned).split())[:limit]
    return cleaned


def _key(value: str) -> str:
    return sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _split_sql_statements(sql: str) -> list[str]:
    # Split canonical SQL on statement boundaries while keeping trigger bodies,
    # quoted strings, and comments intact (C2.2c old-writer fence). A semicolon
    # ends a statement only outside strings and comments and outside any
    # BEGIN...END block; BEGIN and END match case-insensitively.
    squote = chr(39)
    dquote = chr(34)
    statements: list[str] = []
    chunk: list[str] = []
    depth = 0
    pos = 0
    total = len(sql)
    in_single = False
    in_double = False
    in_line = False
    in_block = False
    while pos < total:
        char = sql[pos]
        nxt = sql[pos + 1] if pos + 1 < total else ''
        if in_line:
            chunk.append(char)
            if char == chr(10):
                in_line = False
            pos += 1
            continue
        if in_block:
            chunk.append(char)
            if char == '*' and nxt == '/':
                chunk.append(nxt)
                pos += 2
                in_block = False
            else:
                pos += 1
            continue
        if in_single:
            chunk.append(char)
            if char == squote:
                if nxt == squote:
                    chunk.append(nxt)
                    pos += 2
                else:
                    in_single = False
                    pos += 1
            else:
                pos += 1
            continue
        if in_double:
            chunk.append(char)
            if char == dquote:
                if nxt == dquote:
                    chunk.append(nxt)
                    pos += 2
                else:
                    in_double = False
                    pos += 1
            else:
                pos += 1
            continue
        if char == '-' and nxt == '-':
            in_line = True
            chunk.append(char)
            chunk.append(nxt)
            pos += 2
            continue
        if char == '/' and nxt == '*':
            in_block = True
            chunk.append(char)
            chunk.append(nxt)
            pos += 2
            continue
        if char == squote:
            in_single = True
            chunk.append(char)
            pos += 1
            continue
        if char == dquote:
            in_double = True
            chunk.append(char)
            pos += 1
            continue
        if char == ';' and depth == 0:
            statements.append(''.join(chunk))
            chunk = []
            pos += 1
            continue
        if char.isalpha() or char == '_':
            start = pos
            while pos < total and (sql[pos].isalnum() or sql[pos] == '_'):
                pos += 1
            word = sql[start:pos]
            upper = word.upper()
            if upper == 'BEGIN':
                depth += 1
            elif upper == 'END' and depth:
                depth -= 1
            chunk.append(word)
            continue
        chunk.append(char)
        pos += 1
    statements.append(''.join(chunk))
    return [statement for statement in statements if statement.strip()]


def _apply_schema_to(connection: sqlite3.Connection) -> None:
    # The schema file contains only standalone CREATE statements. Executing
    # them individually preserves the surrounding transaction; SQLite's
    # executescript helper would implicitly commit before running it.
    for statement in _split_sql_statements(SCHEMA_SQL):
        connection.execute(statement)


def _record_migration_to(connection: sqlite3.Connection, version: int, app_version: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (?, ?, ?, ?)",
        (version, migration_checksum(version), _iso(), app_version),
    )


def _configure_authoritative_connection(connection: sqlite3.Connection) -> None:
    """Apply and verify the durability contract on every state writer."""

    mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
    if mode not in {"wal", "memory"}:
        raise StateError(f"authoritative state requires WAL, got {mode or 'unknown'}")
    connection.execute("PRAGMA synchronous=FULL")
    effective = int(connection.execute("PRAGMA synchronous").fetchone()[0])
    if effective != 2:
        raise StateError(f"authoritative state requires synchronous=FULL, got {effective}")


def _apply_migration_sql(connection: sqlite3.Connection, version: int) -> None:
    """Execute one catalog migration canonical statements in the caller transaction.

    Split-execution (rather than executescript) preserves a surrounding
    BEGIN IMMEDIATE so the whole migration stays atomic. Shared by the C2.1
    test-guarded runner and the C2.2 fenced offline runner; behavior stays
    identical for both callers.
    """
    for statement in _split_sql_statements(MIGRATION_SQL[version]):
        connection.execute(statement)


def _adopt_legacy_rows(connection: sqlite3.Connection) -> None:
    article_rows = connection.execute(
        "SELECT fingerprint, title, url, source, topic, score, sent_at, delivery_date FROM sent_articles"
    ).fetchall()
    run_rows = connection.execute(
        "SELECT delivery_date, started_at, completed_at, status, item_count, error FROM runs"
    ).fetchall()
    dates = {str(row[0]) for row in run_rows} | {str(row[7]) for row in article_rows}
    for delivery_date in sorted(dates):
        run = next((row for row in run_rows if row[0] == delivery_date), None)
        items = [row for row in article_rows if row[7] == delivery_date]
        if run and run[3] == "completed":
            state = "completed_empty" if not items else "completed"
            terminal_error = ""
        else:
            # Legacy running/failed rows are recorded for audit but are
            # never treated as content that may be replayed automatically.
            state = "failed_terminal"
            terminal_error = _sanitize_error((run[5] if run else "legacy incomplete run") or "legacy incomplete run")
        started_at = str(run[1]) if run else _iso()
        completed_at = str(run[2]) if run and run[2] else (_iso() if state.startswith("completed") else None)
        connection.execute(
            "INSERT OR IGNORE INTO deliveries(delivery_date,generation,kind,state,run_id,config_hash,started_at,prepared_at,completed_at,terminal_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                delivery_date,
                0,
                "empty" if not items else "content",
                state,
                f"legacy-{delivery_date}",
                "legacy",
                started_at,
                completed_at,
                completed_at,
                terminal_error,
            ),
        )
        delivery_id = connection.execute(
            "SELECT delivery_id FROM deliveries WHERE delivery_date=? AND generation=0", (delivery_date,)
        ).fetchone()[0]
        for position, row in enumerate(items):
            fingerprint, title, url, source, topic, score, sent_at, _ = row
            bounded_title = _sanitize_error(title, 512)
            bounded_url = _sanitize_error(url, 2048)
            url_key = _key(canonical_url(bounded_url))
            connection.execute(
                "INSERT OR IGNORE INTO delivery_items(delivery_id,position,fingerprint,url_key,title_key,title,url,source,score,topic,chunk_index) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (
                    delivery_id,
                    position,
                    fingerprint,
                    url_key,
                    fingerprint,
                    bounded_title,
                    bounded_url,
                    _sanitize_error(source, 160),
                    score,
                    _sanitize_error(topic, 160),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO article_history(url_key,title_key,fingerprint,delivery_id,chunk_id,title,url,source,sent_at) VALUES (?,?,?,?,NULL,?,?,?,?)",
                (
                    url_key,
                    fingerprint,
                    fingerprint,
                    delivery_id,
                    bounded_title,
                    bounded_url,
                    _sanitize_error(source, 160),
                    str(sent_at),
                ),
            )


def _ledger_versions(rows: Sequence[sqlite3.Row]) -> list[int]:
    """Parse ledger versions; a non-integer version is malformed (C2.1)."""
    versions: list[int] = []
    for row in rows:
        try:
            versions.append(int(row[0]))
        except (TypeError, ValueError):
            raise StateError("migration ledger holds a non-integer version") from None
    return versions


def run_catalog_migrations(
    connection: sqlite3.Connection, *, guard: MigrationGuard | None, app_version: str
) -> int:
    """Apply pending catalog migrations under an explicit guard; return versions applied.

    C2.1 admits only the test guard. The public migrate command never builds
    one and fails closed with maintenance_unavailable until C2.2 supplies the
    exclusive maintenance guard. A repeat run over a current database applies
    nothing and writes nothing.
    """
    if not isinstance(guard, MigrationGuard) or guard.scope != "tests":
        raise MigrationNotPermitted("catalog migration requires an explicit test or maintenance guard")
    report = verify_catalog()
    if not report.ok:
        raise StateError(f"migration catalog is invalid: {'; '.join(report.issues)}")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables and connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
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
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StateError("database integrity check failed after migration")
        return CURRENT_SCHEMA_VERSION
    rows = connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
    versions = _ledger_versions(rows)
    contiguity = ledger_contiguity_issue(versions)
    if contiguity is not None:
        raise StateError(f"schema migration ledger is invalid: {contiguity}")
    for row in rows:
        if int(row[0]) <= CURRENT_SCHEMA_VERSION and row[1] != migration_checksum(int(row[0])):
            raise StateError(f"schema migration checksum mismatch at version {row[0]}")
    current = versions[-1]
    if current > CURRENT_SCHEMA_VERSION:
        raise StateError(f"database schema {current} is newer than application schema {CURRENT_SCHEMA_VERSION}")
    if current >= CURRENT_SCHEMA_VERSION:
        return 0
    pending = list(range(current + 1, CURRENT_SCHEMA_VERSION + 1))
    connection.execute("BEGIN IMMEDIATE")
    try:
        for version in pending:
            _apply_migration_sql(connection, version)
            _record_migration_to(connection, version, app_version)
        # Adopt legacy rows in the same transaction so pre-fence old-writer
        # content survives the v4 old-writer fence (C2.2c).
        _adopt_legacy_rows(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise StateError("database integrity check failed after migration")
    return len(pending)


class StateStore:
    """A short-transaction state boundary.

    The connection is process-local.  Network calls must happen outside every
    method that changes a delivery or chunk state.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        readonly: bool = False,
        offline: bool = False,
        busy_timeout_ms: int = 5_000,
        maintenance_context: MaintenanceContext | None = None,
    ):
        memory_only = str(path) == ":memory:"
        self.readonly = readonly and not memory_only
        self.offline = bool(offline and self.readonly)
        self.path = None if memory_only else Path(path)
        self._runtime_context: RuntimeContext | None = None
        self._maintenance_context = maintenance_context
        self._closed = False
        if self.path and not self.readonly:
            if maintenance_context is not None and (
                maintenance_context.db_path != self.path.resolve() or not maintenance_context.live
            ):
                raise MaintenanceBusy("maintenance context is missing, stale, or for another database")
            # Reject an existing incompatible database before creating the
            # runtime guard sidecar.  A failed ordinary startup must be
            # observationally side-effect free; an empty newly-created file
            # is still allowed to go through the fresh-schema path below.
            if maintenance_context is None and self.path.is_file() and self.path.stat().st_size > 0:
                from .inspection import inspect_state

                # Avoid sidecars for a quiescent refusal, but switch to the
                # WAL-aware live reader whenever an active writer has left
                # frames that the main file does not contain yet.
                has_wal_sidecar = any(Path(f"{self.path}{suffix}").exists() for suffix in ("-wal", "-shm"))
                existing = inspect_state(self.path, offline=not has_wal_sidecar)
                if existing.classification == "migration_required":
                    raise MigrationRequiredError(MIGRATION_REQUIRED_MESSAGE)
                if existing.classification != "compatible":
                    raise StateError(f"state database is not writable: {existing.classification}: {existing.detail}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if maintenance_context is None:
                held, hold_info = is_maintenance_held(self.path.resolve())
                if held:
                    raise MaintenanceBusy(f"exclusive maintenance in progress for {self.path.resolve()} (owner {hold_info.get('owner')}); runtime startup refused")
                self._runtime_context = RuntimeContext.acquire(
                    self.path,
                    owner=f"state-writer:{os.getpid()}:{uuid.uuid4().hex}",
                )
        if self.readonly:
            if not self.path or not self.path.exists():
                raise FileNotFoundError(str(self.path))
            # URI mode keeps a dry-run from creating or migrating a database.
            # Live readers must not use immutable=1: that flag can ignore a
            # committed WAL.  It is reserved for explicitly verified,
            # quiescent offline artifacts.
            query = "mode=ro&immutable=1" if self.offline else "mode=ro"
            self.connection = sqlite3.connect(
                f"file:{self.path.resolve().as_posix()}?{query}", uri=True, timeout=busy_timeout_ms / 1000
            )
        else:
            database_path = ":memory:" if memory_only else self.path
            if database_path is None:
                raise StateError("state database path is not available")
            try:
                self.connection = sqlite3.connect(database_path, timeout=busy_timeout_ms / 1000)
            except BaseException:
                if self._runtime_context is not None:
                    self._runtime_context.release()
                    self._runtime_context = None
                raise
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            if self.readonly:
                self.connection.execute("PRAGMA query_only=ON")
            if not self.readonly:
                # C2.1: verify (read-only SELECTs) before enabling WAL, so a
                # refused open cannot change a single byte of the database.
                existing_tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not existing_tables:
                    # A brand-new database has no application state to refuse;
                    # establish the authoritative journal/durability policy
                    # before the first schema transaction.
                    _configure_authoritative_connection(self.connection)
                self._ensure_schema()
                if existing_tables:
                    _configure_authoritative_connection(self.connection)
            else:
                # Read-only callers are inspectors as well as query clients.
                # Validate the exact catalog before exposing a connection so a
                # malformed or migration-required file cannot masquerade as a
                # usable status/backup source.
                self._ensure_schema()
        except Exception:
            # A refused open must not leak a locked connection (C2.1).
            self.connection.close()
            if self._runtime_context is not None:
                self._runtime_context.release()
                self._runtime_context = None
            raise

    def _ensure_schema(self) -> None:
        tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and self.integrity_check() != "ok":
            raise StateError("database integrity check failed before schema inspection")
        if "schema_migrations" not in tables:
            if tables:
                # Legacy v1 or partial state: never auto-migrate at open (C2.1).
                raise MigrationRequiredError(MIGRATION_REQUIRED_MESSAGE)
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                _apply_schema_to(self.connection)
                for version in range(1, CURRENT_SCHEMA_VERSION + 1):
                    _record_migration_to(self.connection, version, __version__)
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            if self.integrity_check() != "ok":
                raise StateError("database integrity check failed after migration")
            return

        rows = self.connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
        if not rows:
            raise MigrationRequiredError(MIGRATION_REQUIRED_MESSAGE)
        versions = _ledger_versions(rows)
        contiguity = ledger_contiguity_issue(versions)
        if contiguity is not None:
            raise StateError(f"schema migration ledger is invalid: {contiguity}")
        for row in rows:
            expected = migration_checksum(int(row[0]))
            if row[1] != expected:
                raise StateError(f"schema migration checksum mismatch at version {row[0]}")
        current = versions[-1]
        if current > CURRENT_SCHEMA_VERSION:
            raise StateError(f"database schema {current} is newer than application schema {CURRENT_SCHEMA_VERSION}")
        if current >= CURRENT_SCHEMA_VERSION:
            required = {
                "run_leases",
                "deliveries",
                "delivery_attempts",
                "delivery_items",
                "outbox_chunks",
                "article_history",
                "source_results",
                "delivery_resolutions",
                "state_transitions",
                "force_audits",
                "maintenance_fences",
            }
            missing = sorted(required - tables)
            if missing:
                raise StateError(f"database schema is missing required table(s): {', '.join(missing)}")
            cols = {r[1] for r in self.connection.execute("PRAGMA table_info(deliveries)").fetchall()}
            if "target_snapshot" not in cols:
                # A current-ledger database without the v3 column needs the audited migration path.
                raise MigrationRequiredError(MIGRATION_REQUIRED_MESSAGE)
            required_delivery_columns = {
                "retry_policy_json",
                "retry_first_at",
                "retry_last_at",
                "retry_attempt_high_water",
                "retry_deadline_at",
                "retry_elapsed_seconds",
                "force_operator",
                "force_reason",
                "predecessor_delivery_id",
            }
            missing_delivery_columns = sorted(required_delivery_columns - {str(value) for value in cols})
            chunk_cols = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(outbox_chunks)").fetchall()}
            missing_delivery_columns.extend(sorted({"first_attempt_at", "last_attempt_at", "retry_deadline_at"} - chunk_cols))
            if missing_delivery_columns:
                raise MigrationRequiredError(MIGRATION_REQUIRED_MESSAGE)
            fences = {row[0] for row in self.connection.execute('SELECT name FROM sqlite_master WHERE type=\'trigger\'').fetchall()}
            missing_fences = sorted(set(LEGACY_FENCE_TRIGGERS) - {str(name) for name in fences})
            if missing_fences:
                raise StateError('database schema is missing legacy fence trigger(s): ' + ', '.join(missing_fences))
            return
        raise MigrationRequiredError(MIGRATION_REQUIRED_MESSAGE)

    def _ensure_writable(self) -> None:
        if self.readonly:
            raise DatabaseReadOnly("state store is read-only")
        if self.path is None:
            return
        if self._maintenance_context is not None:
            if not self._maintenance_context.live:
                raise MaintenanceBusy("exclusive maintenance context is no longer live")
            return
        runtime = self._runtime_context
        if runtime is None or not runtime.live:
            raise StateError("writable state runtime authority is missing, stale, or superseded")
        held, info = is_maintenance_held(self.path)
        if held:
            raise MaintenanceBusy(
                f"exclusive maintenance is active for {self.path} (owner {info.get('owner')}); state mutation refused"
            )

    @staticmethod
    def _require_runtime_owner(owner_id: str | None) -> str:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise LeaseLost("runtime owner capability is required for this mutation")
        return owner_id

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.connection.close()
        finally:
            if self._runtime_context is not None:
                self._runtime_context.release()
                self._runtime_context = None

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        try:
            row = self.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0] or 0)

    def integrity_check(self) -> str:
        row = self.connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else ""

    def backup_to(self, target: str | Path) -> Path:
        target_path = Path(target)
        if self.path and target_path.resolve() == self.path.resolve():
            raise StateError("backup target must differ from the active database")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(target_path)
        began_snapshot = False
        try:
            destination.execute("PRAGMA synchronous=FULL")
            # The backup API copies a consistent snapshot, but explicitly
            # pinning the source in a read transaction makes that guarantee
            # visible and prevents a concurrent writer from changing the
            # logical recovery point during the copy.
            if not self.connection.in_transaction:
                self.connection.execute("BEGIN")
                began_snapshot = True
            with destination:
                self.connection.backup(destination)
            if began_snapshot:
                self.connection.rollback()
                began_snapshot = False
        finally:
            if began_snapshot:
                with contextlib.suppress(sqlite3.Error):
                    self.connection.rollback()
            destination.close()
        return target_path

    def sent_fingerprints(self, items: Iterable[NewsItem]) -> set[str]:
        """Legacy title-key query retained for callers on the v1 API."""
        fingerprints = [item.fingerprint for item in items]
        if not fingerprints:
            return set()
        found: set[str] = set()
        for start in range(0, len(fingerprints), 500):
            chunk = fingerprints[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT title_key FROM article_history WHERE title_key IN ({placeholders}) UNION SELECT fingerprint FROM sent_articles WHERE fingerprint IN ({placeholders})",
                chunk + chunk,
            )
            found.update(str(row[0]) for row in rows)
        return found

    def identity_keys(
        self,
        items: Iterable[NewsItem],
        *,
        now: datetime | None = None,
        title_dedupe_days: int = 14,
        url_retention_days: int = 365,
    ) -> tuple[set[str], set[str]]:
        item_list = list(items)
        if not item_list:
            return set(), set()
        url_keys = [item.url_key for item in item_list]
        title_keys = [item.title_key for item in item_list]
        url_found: set[str] = set()
        title_found: set[str] = set()
        url_cutoff = _iso(_utc(now) - timedelta(days=url_retention_days))
        title_cutoff = _iso(_utc(now) - timedelta(days=title_dedupe_days))
        for start in range(0, len(item_list), 400):
            urls = url_keys[start : start + 400]
            titles = title_keys[start : start + 400]
            up = ",".join("?" for _ in urls)
            tp = ",".join("?" for _ in titles)
            url_found.update(
                row[0]
                for row in self.connection.execute(
                    f"SELECT url_key FROM article_history WHERE sent_at >= ? AND url_key IN ({up})", [url_cutoff, *urls]
                )
            )
            if title_dedupe_days > 0:
                title_found.update(
                    row[0]
                    for row in self.connection.execute(
                        f"SELECT title_key FROM article_history WHERE sent_at >= ? AND title_key IN ({tp})", [title_cutoff, *titles]
                    )
                )
        return url_found, title_found

    def already_completed(self, delivery_date: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM deliveries WHERE delivery_date=? AND state IN ('completed','completed_empty') LIMIT 1",
            (delivery_date,),
        ).fetchone()
        if row:
            return True
        row = self.connection.execute("SELECT status FROM runs WHERE delivery_date=?", (delivery_date,)).fetchone()
        return bool(row and row[0] == "completed")

    def latest_generation(self, delivery_date: str) -> int:
        row = self.connection.execute("SELECT MAX(generation) FROM deliveries WHERE delivery_date=?", (delivery_date,)).fetchone()
        # ponytail: explicit None check — 0 is valid generation and must not become -1
        return int(row[0] if row[0] is not None else -1)

    def active_delivery(self, delivery_date: str | None) -> DeliveryInfo | None:
        if delivery_date is None:
            return self._latest_active()
        row = self.connection.execute(
            "SELECT delivery_id,delivery_date,generation,kind,state,run_id,config_hash,target_snapshot,next_attempt_at,terminal_error FROM deliveries WHERE delivery_date=? AND state NOT IN ('completed','completed_empty','failed_terminal') ORDER BY generation DESC LIMIT 1",
            (delivery_date,),
        ).fetchone()
        return self._delivery(row) if row else None

    def delivery(self, delivery_id: int) -> DeliveryInfo | None:
        row = self.connection.execute(
            "SELECT delivery_id,delivery_date,generation,kind,state,run_id,config_hash,target_snapshot,next_attempt_at,terminal_error FROM deliveries WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        return self._delivery(row) if row else None

    @staticmethod
    def _delivery(row: sqlite3.Row | None) -> DeliveryInfo | None:
        if row is None:
            return None
        # Handle both v2 (9 cols) and v3 (10 cols) for backward compat during migration
        if len(row) == 9:
            return DeliveryInfo(
                int(row[0]),
                str(row[1]),
                int(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                "",
                str(row[7] or ""),
                str(row[8] or ""),
            )
        return DeliveryInfo(
            int(row[0]),
            str(row[1]),
            int(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7] or ""),
            str(row[8] or ""),
            str(row[9] or ""),
        )

    def _record_transition(
        self,
        entity_type: str,
        entity_id: int,
        from_state: str,
        to_state: str,
        *,
        actor_type: str,
        actor_id: str,
        reason: str = "",
        run_id: str = "",
    ) -> None:
        """Append one bounded state transition to the durable audit trail."""

        self.connection.execute(
            "INSERT INTO state_transitions(entity_type,entity_id,from_state,to_state,actor_type,actor_id,reason,run_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                entity_type[:64],
                int(entity_id),
                from_state[:64],
                to_state[:64],
                actor_type[:64],
                _sanitize_error(actor_id, 160),
                _sanitize_error(reason, 500),
                _sanitize_error(run_id, 160),
                _iso(),
            ),
        )

    @staticmethod
    def _retry_policy_json(retry_policy: Mapping[str, Any] | None) -> str:
        if retry_policy is None:
            return "{}"
        if not isinstance(retry_policy, Mapping):
            raise StateError("retry policy must be a mapping")
        allowed = {
            "enabled",
            "max_attempts",
            "base_delay_seconds",
            "max_delay_seconds",
            "jitter_seconds",
            "max_elapsed_seconds",
        }
        unknown = set(retry_policy) - allowed
        if unknown:
            raise StateError("retry policy contains unsupported fields")
        normalized: dict[str, Any] = {}
        for key, value in retry_policy.items():
            if key == "enabled":
                if not isinstance(value, bool):
                    raise StateError("retry policy enabled must be boolean")
                normalized[key] = value
            else:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 31_536_000:
                    raise StateError(f"retry policy {key} must be a bounded integer")
                normalized[key] = value
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def create_delivery(
        self,
        delivery_date: str,
        *,
        kind: str = "content",
        generation: int | None = None,
        run_id: str | None = None,
        config_hash: str = "",
        target_snapshot: str = "",
        state: str = "collecting",
        owner_id: str | None = None,
        retry_policy: Mapping[str, Any] | None = None,
        predecessor_delivery_id: int | None = None,
        force_operator: str = "",
        force_reason: str = "",
    ) -> DeliveryInfo:
        self._ensure_writable()
        owner_id = self._require_runtime_owner(owner_id)
        self._assert_lease_owner(owner_id)
        with self._lock:
            if generation is None:
                generation = self.latest_generation(delivery_date) + 1
            run_id = run_id or str(uuid.uuid4())
            now = _iso()
            retry_policy_json = self._retry_policy_json(retry_policy)
            if predecessor_delivery_id is None and (force_operator or force_reason):
                raise StateError("forced delivery audit requires a predecessor")
            if predecessor_delivery_id is not None and (not force_operator.strip() or not force_reason.strip()):
                raise StateError("forced delivery requires an operator and reason")
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                if generation is None or int(generation) < 0:
                    raise StateError("delivery generation must be a nonnegative integer")
                if kind not in {"content", "empty", "collection_retry"}:
                    raise StateError("unsupported delivery kind")
                if predecessor_delivery_id is not None:
                    predecessor = self.connection.execute(
                        "SELECT delivery_date,generation,state FROM deliveries WHERE delivery_id=?",
                        (predecessor_delivery_id,),
                    ).fetchone()
                    if not predecessor:
                        raise InvalidTransition("forced delivery predecessor does not exist")
                    if str(predecessor[0]) != delivery_date:
                        raise InvalidTransition("forced delivery predecessor has a different delivery date")
                    if str(predecessor[2]) not in {"completed", "completed_empty"}:
                        raise InvalidTransition("forced delivery predecessor is not completed")
                    if int(generation) <= int(predecessor[1]):
                        raise InvalidTransition("forced delivery generation must be newer than its predecessor")
                self.connection.execute(
                    "INSERT INTO deliveries(delivery_date,generation,kind,state,run_id,config_hash,target_snapshot,started_at,retry_policy_json,force_operator,force_reason,predecessor_delivery_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        delivery_date,
                        generation,
                        kind,
                        state,
                        run_id,
                        config_hash,
                        target_snapshot,
                        now,
                        retry_policy_json,
                        _sanitize_error(force_operator, 160),
                        _sanitize_error(force_reason, 500),
                        predecessor_delivery_id,
                    ),
                )
                delivery_id = int(self.connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                self._record_transition(
                    "delivery",
                    delivery_id,
                    "",
                    state,
                    actor_type="runtime",
                    actor_id=owner_id,
                    reason="forced_generation" if predecessor_delivery_id is not None else "created",
                    run_id=run_id,
                )
                if predecessor_delivery_id is not None:
                    self.connection.execute(
                        "INSERT INTO force_audits(delivery_id,predecessor_delivery_id,operator,reason,config_hash,target_snapshot,created_at) VALUES (?,?,?,?,?,?,?)",
                        (
                            delivery_id,
                            predecessor_delivery_id,
                            _sanitize_error(force_operator, 160),
                            _sanitize_error(force_reason, 500),
                            _sanitize_error(config_hash, 160),
                            _sanitize_error(target_snapshot, 160),
                            now,
                        ),
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def start_run(self, delivery_date: str, *, owner_id: str | None = None) -> None:
        """Create a collecting delivery for older callers.

        This adapter only creates the durable collecting generation. Completion
        must flow through the frozen outbox and acknowledgement methods.
        """
        self._ensure_writable()
        owner_id = self._require_runtime_owner(owner_id)
        self._assert_lease_owner(owner_id)
        active = self.active_delivery(delivery_date)
        if active:
            return
        if self.already_completed(delivery_date):
            return
        self.create_delivery(
            delivery_date,
            generation=max(0, self.latest_generation(delivery_date) + 1),
            state="collecting",
            owner_id=owner_id,
        )

    def complete_run(self, delivery_date: str, items: Iterable[NewsItem], *, owner_id: str | None = None) -> None:
        """Reject the unsafe pre-outbox completion compatibility path."""
        self._ensure_writable()
        del delivery_date, items, owner_id
        raise StateError("direct completion is removed; prepare and acknowledge the outbox instead")

    def fail_run(self, delivery_date: str, error: str, *, owner_id: str | None = None) -> None:
        """Reject the unsafe pre-outbox failure compatibility path."""
        self._ensure_writable()
        del delivery_date, error, owner_id
        raise StateError("direct failure is removed; use an explicit outbox transition")

    def acquire_lease(
        self,
        scope: str,
        owner_id: str,
        ttl_seconds: int = 180,
        *,
        now: datetime | None = None,
    ) -> LeaseAcquire:
        self._ensure_writable()
        current = _utc(now)
        now_text = _iso(current)
        expiry_text = _iso(current + timedelta(seconds=ttl_seconds))
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_runtime_authority_in_transaction()
                row = self.connection.execute("SELECT owner_id,expires_at FROM run_leases WHERE scope=?", (scope,)).fetchone()
                if row and str(row[0]) != owner_id and str(row[1]) > now_text:
                    self.connection.commit()
                    return LeaseAcquire(False, "already_running", str(row[0]), str(row[1]))
                if row:
                    self.connection.execute(
                        "UPDATE run_leases SET owner_id=?,acquired_at=?,heartbeat_at=?,expires_at=? WHERE scope=?",
                        (owner_id, now_text, now_text, expiry_text, scope),
                    )
                else:
                    self.connection.execute(
                        "INSERT INTO run_leases(scope,owner_id,acquired_at,heartbeat_at,expires_at) VALUES (?,?,?,?,?)",
                        (scope, owner_id, now_text, now_text, expiry_text),
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return LeaseAcquire(True, "acquired", owner_id, expiry_text)

    def heartbeat_lease(self, scope: str, owner_id: str, ttl_seconds: int = 180, *, now: datetime | None = None) -> str:
        self._ensure_writable()
        current = _utc(now)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id, scope, now=current)
                cursor = self.connection.execute(
                    "UPDATE run_leases SET heartbeat_at=?,expires_at=? WHERE scope=? AND owner_id=?",
                    (_iso(current), _iso(current + timedelta(seconds=ttl_seconds)), scope, owner_id),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost("lease owner is no longer active")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return _iso(current + timedelta(seconds=ttl_seconds))

    def release_lease(self, scope: str, owner_id: str) -> bool:
        self._ensure_writable()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id, scope)
                cursor = self.connection.execute("DELETE FROM run_leases WHERE scope=? AND owner_id=?", (scope, owner_id))
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return cursor.rowcount == 1

    def lease_info(self, scope: str = "delivery") -> dict[str, str] | None:
        row = self.connection.execute(
            "SELECT scope,owner_id,acquired_at,heartbeat_at,expires_at FROM run_leases WHERE scope=?", (scope,)
        ).fetchone()
        if not row:
            return None
        return {
            "scope": str(row[0]),
            "owner_id": str(row[1]),
            "acquired_at": str(row[2]),
            "heartbeat_at": str(row[3]),
            "expires_at": str(row[4]),
        }

    def _assert_lease_owner(self, owner_id: str, scope: str = "delivery") -> None:
        info = self.lease_info(scope)
        if not info or info["owner_id"] != owner_id or info["expires_at"] <= _iso():
            raise LeaseLost("lease owner is not active")

    def _assert_lease_owner_in_transaction(
        self, owner_id: str, scope: str = "delivery", *, now: datetime | None = None
    ) -> None:
        """Check the capability using the same transaction as its mutation.

        The pre-flight check remains useful for a clearer error, but it is not
        the authority boundary: a lease can be replaced between two SQL calls.
        Protected transitions therefore repeat this query after ``BEGIN
        IMMEDIATE``.
        """

        self._assert_runtime_authority_in_transaction()
        row = self.connection.execute(
            "SELECT owner_id,expires_at FROM run_leases WHERE scope=?", (scope,)
        ).fetchone()
        if not row or str(row[0]) != owner_id or str(row[1]) <= _iso(now):
            raise LeaseLost("lease owner is not active")

    def _assert_runtime_authority_in_transaction(self) -> None:
        if self._runtime_context is not None and not self._runtime_context.live:
            raise LeaseLost("runtime execution authority is no longer live")
        if self.path is not None:
            held, _ = is_maintenance_held(self.path)
            if held:
                raise LeaseLost("maintenance authority superseded the runtime lease")
            try:
                maintenance_row = self.connection.execute(
                    "SELECT token,released_at FROM maintenance_fences WHERE fence_id=1"
                ).fetchone()
            except sqlite3.OperationalError:
                # Pre-v5 migration transactions have the portable marker/OS
                # guard but cannot yet have the v5 transaction-visible row.
                maintenance_row = None
            if maintenance_row is not None and maintenance_row[1] is None:
                raise LeaseLost("maintenance database fence superseded the runtime lease")

    def _assert_maintenance_authority_in_transaction(self) -> None:
        context = self._maintenance_context
        if context is None or not context.live:
            raise StateError("exclusive maintenance authority is missing, stale, or superseded")
        if context.db_path != (self.path.resolve() if self.path is not None else context.db_path):
            raise StateError("exclusive maintenance authority is for another database")
        if context.database_epoch is None:
            try:
                ensure_database_fence(self.connection, context)
            except Exception as exc:
                if isinstance(exc, StateError):
                    raise
                raise StateError("maintenance database fence is unavailable") from exc
        if context.database_epoch is None:
            return
        try:
            row = self.connection.execute(
                "SELECT epoch,token,released_at FROM maintenance_fences WHERE fence_id=1"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise StateError("maintenance database fence is unavailable") from exc
        if row is None or int(row[0]) != context.database_epoch or str(row[1]) != context.token or row[2] is not None:
            raise StateError("maintenance database fence is missing, stale, or superseded")

    def recover_expired_lease(self, scope: str = "delivery", *, now: datetime | None = None) -> bool:
        """Recover in-flight work before an expired or orphaned lease is reused.

        The lease row may already be gone after a crash or manual cleanup.  A
        durable ``in_flight`` row is still evidence that a remote request may
        have been transmitted, so it is always converted to ``ambiguous``
        while the short recovery transaction holds the runtime authority.
        """
        self._ensure_writable()
        now_text = _iso(now)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_runtime_authority_in_transaction()
                info = self.lease_info(scope)
                if info and info["expires_at"] > now_text:
                    self.connection.commit()
                    return False
                rows = self.connection.execute("SELECT chunk_id,delivery_id FROM outbox_chunks WHERE state='in_flight'").fetchall()
                for row in rows:
                    self.connection.execute(
                        "UPDATE outbox_chunks SET state='ambiguous',in_flight_at=NULL,next_attempt_at=NULL,error_class='telegram_ambiguous',error_text='lease expired or disappeared while request was in flight' WHERE chunk_id=? AND state='in_flight'",
                        (row[0],),
                    )
                    self.connection.execute(
                        "UPDATE delivery_attempts SET ended_at=?,outcome='ambiguous',error_class='telegram_ambiguous',error_text='lease expired or disappeared while request was in flight' WHERE delivery_id=? AND chunk_id=? AND outcome='in_flight'",
                        (now_text, row[1], row[0]),
                    )
                    self.connection.execute(
                        "UPDATE deliveries SET state='needs_attention' WHERE delivery_id=? AND state NOT IN ('completed','completed_empty','failed_terminal')",
                        (row[1],),
                    )
                self.connection.execute("DELETE FROM run_leases WHERE scope=? AND expires_at<=?", (scope, now_text))
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return bool(rows)

    def delivery_has_inflight(self, delivery_id: int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM outbox_chunks WHERE delivery_id=? AND state='in_flight' LIMIT 1", (delivery_id,)
        ).fetchone()
        return row is not None

    def unresolved_count(self, delivery_id: int | None = None) -> int:
        if delivery_id is None:
            row = self.connection.execute("SELECT COUNT(*) FROM outbox_chunks WHERE state='ambiguous'").fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM outbox_chunks WHERE delivery_id=? AND state='ambiguous'", (delivery_id,)
            ).fetchone()
        return int(row[0] or 0)

    def latest_delivery(self, delivery_date: str) -> DeliveryInfo | None:
        row = self.connection.execute(
            "SELECT delivery_id,delivery_date,generation,kind,state,run_id,config_hash,target_snapshot,next_attempt_at,terminal_error "
            "FROM deliveries WHERE delivery_date=? ORDER BY generation DESC LIMIT 1",
            (delivery_date,),
        ).fetchone()
        return self._delivery(row)

    def mark_target_mismatch(
        self, delivery_id: int, *, owner_id: str, expected: str, actual: str
    ) -> DeliveryInfo:
        self._ensure_writable()
        self._assert_lease_owner(owner_id)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                row = self.connection.execute(
                    "SELECT target_snapshot,state FROM deliveries WHERE delivery_id=?", (delivery_id,)
                ).fetchone()
                if not row:
                    raise InvalidTransition("delivery does not exist")
                if expected and str(row[0]) != expected:
                    raise InvalidTransition("delivery target snapshot no longer matches the audited value")
                if actual and str(row[0]) == actual:
                    self.connection.commit()
                    return self.delivery(delivery_id)  # type: ignore[return-value]
                self.connection.execute(
                    "UPDATE deliveries SET state='needs_attention',terminal_error=? WHERE delivery_id=? AND state NOT IN ('completed','completed_empty','failed_terminal')",
                    (_sanitize_error("target snapshot mismatch; manual destination reconciliation required"), delivery_id),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def prepare_delivery(
        self,
        delivery_id: int,
        items: Sequence[NewsItem],
        messages: Sequence[str],
        *,
        owner_id: str,
        item_chunk_indexes: Mapping[str, int] | None = None,
        target_snapshot: str = "",
    ) -> DeliveryInfo:
        self._ensure_writable()
        # C2.3: every runtime mutation is bound to the current lease owner.
        self._assert_lease_owner(owner_id)
        item_chunk_indexes = item_chunk_indexes or {}
        now = _iso()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                delivery = self.delivery(delivery_id)
                if not delivery or delivery.state not in {"collecting", "retry_wait"}:
                    raise InvalidTransition("delivery is not preparable")
                existing_items = self.connection.execute(
                    "SELECT COUNT(*) FROM delivery_items WHERE delivery_id=?", (delivery_id,)
                ).fetchone()[0]
                existing_chunks = self.connection.execute(
                    "SELECT COUNT(*) FROM outbox_chunks WHERE delivery_id=?", (delivery_id,)
                ).fetchone()[0]
                if existing_items or existing_chunks:
                    raise InvalidTransition("delivery payload is already partially prepared")
                if not messages or any(not isinstance(payload, str) or not payload for payload in messages):
                    raise StateError("delivery must persist at least one nonempty text payload")
                if len({item.fingerprint for item in items}) != len(items):
                    raise StateError("delivery contains duplicate item fingerprints")
                invalid_mapping = [
                    item.fingerprint
                    for item in items
                    if item.fingerprint in item_chunk_indexes
                    and (
                        int(item_chunk_indexes[item.fingerprint]) < 0
                        or int(item_chunk_indexes[item.fingerprint]) >= len(messages)
                    )
                ]
                if invalid_mapping:
                    raise StateError("delivery item-to-chunk mapping is incomplete or out of range")
                # C3.3: freeze target_snapshot on first prepare, verify on subsequent (idempotent)
                if delivery.target_snapshot and target_snapshot and delivery.target_snapshot != target_snapshot:
                    raise InvalidTransition("target snapshot mismatch — delivery frozen to different destination")
                if target_snapshot and not delivery.target_snapshot:
                    self.connection.execute("UPDATE deliveries SET target_snapshot=? WHERE delivery_id=?", (target_snapshot, delivery_id))
                for position, item in enumerate(items):
                    chunk_index = int(item_chunk_indexes.get(item.fingerprint, 0))
                    self.connection.execute(
                        "INSERT INTO delivery_items(delivery_id,position,fingerprint,url_key,title_key,title,url,source,source_url,published_at,summary,collector,query_name,score,topic,topic_label,relevance_reason,matches_json,chunk_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        self._item_row(delivery_id, position, item, chunk_index),
                    )
                for sequence, payload in enumerate(messages):
                    try:
                        bounded_payload = payload.encode("utf-8", errors="strict").decode("utf-8", errors="strict")
                    except UnicodeError as exc:
                        raise StateError("delivery payload contains an invalid Unicode scalar") from exc
                    payload_hash = sha256(bounded_payload.encode("utf-8")).hexdigest()
                    self.connection.execute(
                        "INSERT INTO outbox_chunks(delivery_id,sequence,payload,payload_hash,state,created_at) VALUES (?,?,?,?,?,?)",
                        (delivery_id, sequence, bounded_payload, payload_hash, "pending", now),
                    )
                state = "prepared_empty" if not items else "prepared"
                cursor = self.connection.execute(
                    "UPDATE deliveries SET state=?,prepared_at=? WHERE delivery_id=? AND state IN ('collecting','retry_wait')",
                    (state, now, delivery_id),
                )
                if cursor.rowcount != 1:
                    raise InvalidTransition("delivery preparation transition failed")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(delivery_id)  # type: ignore[return-value]

    @staticmethod
    def _item_row(delivery_id: int, position: int, item: NewsItem, chunk_index: int) -> tuple[Any, ...]:
        return (
            delivery_id,
            position,
            item.fingerprint[:128],
            item.url_key,
            item.title_key,
            item.title[:512],
            item.url[:2048],
            item.source[:160],
            item.source_url[:2048],
            item.published_at.isoformat() if item.published_at else None,
            item.summary[:2048],
            item.collector[:80],
            item.query_name[:160],
            int(item.score),
            item.topic[:160],
            item.topic_label[:256],
            item.relevance_reason[:512],
            json.dumps(item.matches[:20], ensure_ascii=False),
            chunk_index,
        )

    def record_source_results(
        self, delivery_id: int | None, results: Iterable[Any], *, owner_id: str | None = None
    ) -> None:
        self._ensure_writable()
        owner_id = self._require_runtime_owner(owner_id)
        self._assert_lease_owner(owner_id)
        now = _iso()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                for result in results:
                    self.connection.execute(
                        "INSERT INTO source_results(delivery_id,source_id,source_name,outcome,duration_ms,bytes_read,accepted_count,quarantined_count,reason_code,error_class,error_text,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            delivery_id,
                            str(result.source_id)[:160],
                            str(result.source_name)[:160],
                            str(result.outcome)[:40],
                            int(result.duration_ms),
                            int(result.bytes_read),
                            int(result.accepted_count),
                            int(result.quarantined_count),
                            str(result.reason_code)[:80],
                            str(result.error_class)[:80],
                            _sanitize_error(getattr(result, "error", ""), 500),
                            now,
                        ),
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def due_chunks(self, delivery_id: int, *, now: datetime | None = None) -> list[ChunkInfo]:
        now_text = _iso(now)
        rows = self.connection.execute(
            "SELECT chunk_id,delivery_id,sequence,payload,payload_hash,state,attempt_count,telegram_message_id FROM outbox_chunks WHERE delivery_id=? AND (state='pending' OR (state='retry_wait' AND (next_attempt_at IS NULL OR next_attempt_at<=?))) ORDER BY sequence",
            (delivery_id, now_text),
        ).fetchall()
        return [
            ChunkInfo(int(row[0]), int(row[1]), int(row[2]), str(row[3]), str(row[4]), str(row[5]), int(row[6]), str(row[7] or ""))
            for row in rows
        ]

    def begin_chunk_attempt(self, chunk_id: int, *, run_id: str, owner_id: str, now: datetime | None = None) -> tuple[ChunkInfo, int]:
        self._ensure_writable()
        self._assert_lease_owner(owner_id)
        now_text = _iso(now)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                row = self.connection.execute(
                    "SELECT chunk_id,delivery_id,sequence,payload,payload_hash,state,attempt_count,telegram_message_id,next_attempt_at FROM outbox_chunks WHERE chunk_id=?",
                    (chunk_id,),
                ).fetchone()
                if not row:
                    raise InvalidTransition("chunk does not exist")
                if row[5] == "retry_wait" and row[8] and row[8] > now_text:
                    raise RetryNotDue("chunk retry is not due")
                if row[5] not in {"pending", "retry_wait"}:
                    raise InvalidTransition(f"chunk is {row[5]}")
                actual_hash = sha256(str(row[3]).encode("utf-8")).hexdigest()
                if actual_hash != str(row[4]):
                    raise StateError("outbox payload hash does not match the frozen payload")
                earlier = self.connection.execute(
                    "SELECT sequence,state FROM outbox_chunks WHERE delivery_id=? AND sequence<? AND state!='sent' ORDER BY sequence LIMIT 1",
                    (row[1], row[2]),
                ).fetchone()
                if earlier:
                    raise InvalidTransition(
                        f"chunk ordering violation: sequence {earlier[0]} is {earlier[1]} before {row[2]}"
                    )
                delivery = self.delivery(int(row[1]))
                if not delivery or delivery.state in {"needs_attention", "completed", "completed_empty", "failed_terminal"}:
                    raise InvalidTransition("delivery cannot send this chunk")
                attempt_number = int(row[6]) + 1
                self.connection.execute(
                    "UPDATE outbox_chunks SET state='in_flight',attempt_count=?,in_flight_at=?,first_attempt_at=COALESCE(first_attempt_at,?),last_attempt_at=?,error_class='',error_text='' WHERE chunk_id=? AND state IN ('pending','retry_wait')",
                    (attempt_number, now_text, now_text, now_text, chunk_id),
                )
                if self.connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise InvalidTransition("chunk transition lost")
                self.connection.execute(
                    "UPDATE deliveries SET state='sending' WHERE delivery_id=? AND state IN ('prepared','prepared_empty','retry_wait','sending')",
                    (delivery.delivery_id,),
                )
                self.connection.execute(
                    "INSERT INTO delivery_attempts(delivery_id,chunk_id,attempt_number,started_at,outcome,run_id) VALUES (?,?,?,?,?,?)",
                    (delivery.delivery_id, chunk_id, attempt_number, now_text, "in_flight", run_id),
                )
                self._record_retry_observation(delivery.delivery_id, attempt_number, now_text)
                self.connection.execute(
                    "UPDATE outbox_chunks SET retry_deadline_at=(SELECT retry_deadline_at FROM deliveries WHERE delivery_id=?) WHERE chunk_id=?",
                    (delivery.delivery_id, chunk_id),
                )
                self._record_transition(
                    "chunk",
                    chunk_id,
                    str(row[5]),
                    "in_flight",
                    actor_type="runtime",
                    actor_id=owner_id,
                    reason="send_intent_persisted",
                    run_id=run_id,
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        fresh = self.connection.execute(
            "SELECT chunk_id,delivery_id,sequence,payload,payload_hash,state,attempt_count,telegram_message_id FROM outbox_chunks WHERE chunk_id=?",
            (chunk_id,),
        ).fetchone()
        return ChunkInfo(
            int(fresh[0]), int(fresh[1]), int(fresh[2]), str(fresh[3]), str(fresh[4]), str(fresh[5]), int(fresh[6]), str(fresh[7] or "")
        ), attempt_number

    def finish_chunk(
        self,
        chunk_id: int,
        outcome: str,
        *,
        run_id: str,
        owner_id: str,
        error_class: str = "",
        error_text: str = "",
        telegram_message_id: str = "",
        next_attempt_at: datetime | None = None,
    ) -> DeliveryInfo:
        self._ensure_writable()
        self._assert_lease_owner(owner_id)
        if outcome not in {"accepted", "rejected_retryable", "rejected_terminal", "ambiguous"}:
            raise ValueError("unsupported chunk outcome")
        if outcome == "accepted" and (
            not isinstance(telegram_message_id, str)
            or not telegram_message_id.isdigit()
            or int(telegram_message_id) <= 0
        ):
            raise StateError("accepted chunk outcome requires a positive Telegram message id")
        now = _iso()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                row = self.connection.execute(
                    "SELECT delivery_id,sequence,attempt_count,state FROM outbox_chunks WHERE chunk_id=?", (chunk_id,)
                ).fetchone()
                if not row or row[3] != "in_flight":
                    raise InvalidTransition("chunk is not in flight")
                delivery_id = int(row[0])
                attempt_number = int(row[2])
                attempt = self.connection.execute(
                    "SELECT run_id,outcome FROM delivery_attempts WHERE delivery_id=? AND chunk_id=? AND attempt_number=?",
                    (delivery_id, chunk_id, attempt_number),
                ).fetchone()
                if not attempt or str(attempt[0]) != run_id or str(attempt[1]) != "in_flight":
                    raise InvalidTransition("chunk attempt owner or run identity does not match")
                final_state = {
                    "accepted": "sent",
                    "rejected_retryable": "retry_wait",
                    "rejected_terminal": "failed_terminal",
                    "ambiguous": "ambiguous",
                }[outcome]
                next_text = _iso(next_attempt_at) if next_attempt_at else None
                self.connection.execute(
                    "UPDATE outbox_chunks SET state=?,in_flight_at=NULL,next_attempt_at=?,telegram_message_id=?,error_class=?,error_text=?,sent_at=? WHERE chunk_id=? AND state='in_flight'",
                    (
                        final_state,
                        next_text,
                        str(telegram_message_id)[:128],
                        str(error_class)[:80],
                        _sanitize_error(error_text, 1000),
                        now if outcome == "accepted" else None,
                        chunk_id,
                    ),
                )
                if self.connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise InvalidTransition("chunk acknowledgement transition lost")
                self.connection.execute(
                    "UPDATE delivery_attempts SET ended_at=?,outcome=?,error_class=?,error_text=? WHERE delivery_id=? AND chunk_id=? AND attempt_number=? AND outcome='in_flight'",
                    (now, outcome, str(error_class)[:80], _sanitize_error(error_text, 1000), delivery_id, chunk_id, attempt_number),
                )
                self.connection.execute(
                    "UPDATE outbox_chunks SET last_attempt_at=? WHERE chunk_id=?",
                    (now, chunk_id),
                )
                self._record_retry_observation(delivery_id, attempt_number, now)
                if outcome == "accepted":
                    self._record_chunk_history(delivery_id, chunk_id, now)
                    remaining = self.connection.execute(
                        "SELECT COUNT(*) FROM outbox_chunks WHERE delivery_id=? AND state NOT IN ('sent')", (delivery_id,)
                    ).fetchone()[0]
                    if remaining == 0:
                        empty = (
                            self.connection.execute("SELECT COUNT(*) FROM delivery_items WHERE delivery_id=?", (delivery_id,)).fetchone()[0]
                            == 0
                        )
                        self.connection.execute(
                            "UPDATE deliveries SET state=?,completed_at=?,next_attempt_at=NULL WHERE delivery_id=?",
                            ("completed_empty" if empty else "completed", now, delivery_id),
                        )
                    else:
                        self.connection.execute("UPDATE deliveries SET state='sending' WHERE delivery_id=?", (delivery_id,))
                elif outcome == "rejected_retryable":
                    self.connection.execute(
                        "UPDATE deliveries SET state='retry_wait',next_attempt_at=? WHERE delivery_id=?", (next_text, delivery_id)
                    )
                elif outcome == "ambiguous":
                    self.connection.execute(
                        "UPDATE deliveries SET state='needs_attention',terminal_error=? WHERE delivery_id=?",
                        (_sanitize_error(error_text or "telegram acceptance is unknown"), delivery_id),
                    )
                else:
                    self.connection.execute(
                        "UPDATE deliveries SET state='failed_terminal',completed_at=?,terminal_error=? WHERE delivery_id=?",
                        (now, _sanitize_error(error_text or "terminal chunk rejection"), delivery_id),
                    )
                self._record_transition(
                    "chunk",
                    chunk_id,
                    "in_flight",
                    final_state,
                    actor_type="runtime",
                    actor_id=owner_id,
                    reason=error_class or outcome,
                    run_id=run_id,
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def _record_chunk_history(self, delivery_id: int, chunk_id: int, sent_at: str) -> None:
        rows = self.connection.execute(
            "SELECT url_key,title_key,fingerprint,title,url,source,published_at FROM delivery_items WHERE delivery_id=? AND chunk_index=(SELECT sequence FROM outbox_chunks WHERE chunk_id=?)",
            (delivery_id, chunk_id),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "INSERT OR IGNORE INTO article_history(url_key,title_key,fingerprint,delivery_id,chunk_id,title,url,source,published_at,sent_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row[0], row[1], row[2], delivery_id, chunk_id, row[3], row[4], row[5], row[6], sent_at),
            )

    def ensure_collection_retry(
        self,
        delivery_date: str,
        *,
        run_id: str,
        config_hash: str,
        next_attempt_at: datetime,
        error: str,
        owner_id: str | None = None,
    ) -> DeliveryInfo:
        self._ensure_writable()
        owner_id = self._require_runtime_owner(owner_id)
        self._assert_lease_owner(owner_id)
        active = self.active_delivery(delivery_date)
        if active and active.kind == "collection_retry":
            with self._lock:
                self.connection.execute("BEGIN IMMEDIATE")
                try:
                    self._assert_lease_owner_in_transaction(owner_id)
                    self.connection.execute(
                        "UPDATE deliveries SET state='retry_wait',next_attempt_at=?,terminal_error=? WHERE delivery_id=?",
                        (_iso(next_attempt_at), _sanitize_error(error), active.delivery_id),
                    )
                    self.connection.commit()
                except Exception:
                    self.connection.rollback()
                    raise
            return self.delivery(active.delivery_id)  # type: ignore[return-value]
        created = self.create_delivery(
            delivery_date,
            kind="collection_retry",
            run_id=run_id,
            config_hash=config_hash,
            state="retry_wait",
            owner_id=owner_id,
        )
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                self.connection.execute(
                    "UPDATE deliveries SET next_attempt_at=?,terminal_error=? WHERE delivery_id=?",
                    (_iso(next_attempt_at), _sanitize_error(error), created.delivery_id),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(created.delivery_id)  # type: ignore[return-value]

    def set_collection_retry(
        self, delivery_id: int, *, next_attempt_at: datetime, error: str, owner_id: str | None = None
    ) -> DeliveryInfo:
        self._ensure_writable()
        owner_id = self._require_runtime_owner(owner_id)
        self._assert_lease_owner(owner_id)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                previous = self.connection.execute("SELECT state FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
                cursor = self.connection.execute(
                    "UPDATE deliveries SET state='retry_wait',next_attempt_at=?,terminal_error=? WHERE delivery_id=? AND state IN ('collecting','retry_wait')",
                    (_iso(next_attempt_at), _sanitize_error(error), delivery_id),
                )
                if cursor.rowcount != 1:
                    raise InvalidTransition("delivery cannot enter collection retry")
                self._record_transition(
                    "delivery",
                    delivery_id,
                    str(previous[0]) if previous else "",
                    "retry_wait",
                    actor_type="runtime",
                    actor_id=owner_id,
                    reason=error,
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def collection_retry_count(self, delivery_id: int) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM delivery_attempts WHERE delivery_id=? AND chunk_id IS NULL", (delivery_id,)
        ).fetchone()
        return int(row[0] or 0)

    def delivery_retry_policy(self, delivery_id: int, fallback: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return the retry policy frozen with a delivery.

        Older migrated deliveries carry ``{}``; only those rows use the
        caller-supplied fallback.  A nonempty persisted policy is authoritative
        so a later configuration reload cannot extend or reset a delivery's
        retry budget.
        """

        row = self.connection.execute("SELECT retry_policy_json FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        raw = row[0] if row else ""
        try:
            parsed = json.loads(str(raw)) if raw else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        if isinstance(parsed, dict) and parsed:
            return dict(parsed)
        return dict(fallback or {})

    def _record_retry_observation(self, delivery_id: int, attempt_number: int, now_text: str) -> None:
        policy = self.delivery_retry_policy(delivery_id)
        first_row = self.connection.execute("SELECT retry_first_at FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        first = str(first_row[0]) if first_row and first_row[0] else now_text
        deadline = None
        try:
            elapsed_limit = int(policy.get("max_elapsed_seconds", 0))
            deadline = (_utc(datetime.fromisoformat(first)) + timedelta(seconds=max(0, elapsed_limit))).isoformat() if elapsed_limit else None
            elapsed = max(0, int((_utc(datetime.fromisoformat(now_text)) - _utc(datetime.fromisoformat(first))).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            elapsed = 0
        self.connection.execute(
            "UPDATE deliveries SET retry_first_at=COALESCE(retry_first_at,?),retry_last_at=?,retry_attempt_high_water=MAX(retry_attempt_high_water,?),retry_deadline_at=COALESCE(retry_deadline_at,?),retry_elapsed_seconds=? WHERE delivery_id=?",
            (first, now_text, max(0, int(attempt_number)), deadline, elapsed, delivery_id),
        )

    def retry_budget_exhausted(
        self,
        delivery_id: int,
        *,
        max_attempts: int,
        max_elapsed_seconds: int,
        chunk_id: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Evaluate persisted attempt and elapsed-time limits fail-closed.

        Attempt counts live in SQLite, so a process restart or a new calendar
        invocation cannot reset them.  A backwards/invalid wall clock is also
        treated as exhausted rather than silently extending the retry window.
        """

        fallback_policy = {
            "max_attempts": max_attempts,
            "max_elapsed_seconds": max_elapsed_seconds,
        }
        policy = self.delivery_retry_policy(delivery_id, fallback_policy)
        max_attempts = int(policy.get("max_attempts", max_attempts))
        max_elapsed_seconds = int(policy.get("max_elapsed_seconds", max_elapsed_seconds))
        if chunk_id is None:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM delivery_attempts WHERE delivery_id=? AND chunk_id IS NULL", (delivery_id,)
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT attempt_count FROM outbox_chunks WHERE delivery_id=? AND chunk_id=?",
                (delivery_id, chunk_id),
            ).fetchone()
        attempts = int(row[0] or 0) if row else 0
        if attempts >= max(1, int(max_attempts)):
            return True
        delivery_row = self.connection.execute(
            "SELECT started_at FROM deliveries WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        attempt_row = self.connection.execute(
            "SELECT MIN(started_at) FROM delivery_attempts WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        started_value = attempt_row[0] if attempt_row and attempt_row[0] else (delivery_row[0] if delivery_row else None)
        if not started_value:
            return True
        try:
            started = datetime.fromisoformat(str(started_value)).astimezone(UTC)
            current = _utc(now)
        except (TypeError, ValueError, OverflowError):
            return True
        elapsed = (current - started).total_seconds()
        if elapsed < 0:
            return True
        return elapsed >= max(1, int(max_elapsed_seconds))

    def record_collection_attempt(
        self, delivery_id: int, *, run_id: str, error: str, outcome: str, owner_id: str | None = None
    ) -> int:
        self._ensure_writable()
        owner_id = self._require_runtime_owner(owner_id)
        self._assert_lease_owner(owner_id)
        now = _iso()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                attempt_number = self.collection_retry_count(delivery_id) + 1
                self.connection.execute(
                    "INSERT INTO delivery_attempts(delivery_id,chunk_id,attempt_number,started_at,ended_at,outcome,error_class,error_text,run_id) VALUES (?,?,?, ?,?,?,?, ?,?)",
                    (delivery_id, None, attempt_number, now, now, outcome, "all_sources_failed", _sanitize_error(error, 1000), run_id),
                )
                self._record_retry_observation(delivery_id, attempt_number, now)
                self._record_transition(
                    "delivery",
                    delivery_id,
                    "collecting",
                    "retry_wait" if outcome == "collection_retry" else outcome,
                    actor_type="runtime",
                    actor_id=owner_id,
                    reason=error,
                    run_id=run_id,
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return attempt_number

    def fail_delivery(self, delivery_id: int, error: str, *, owner_id: str | None = None) -> DeliveryInfo:
        self._ensure_writable()
        owner_id = self._require_runtime_owner(owner_id)
        self._assert_lease_owner(owner_id)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                cursor = self.connection.execute(
                    "UPDATE deliveries SET state='failed_terminal',completed_at=?,terminal_error=? WHERE delivery_id=? AND state NOT IN ('completed','completed_empty','failed_terminal','needs_attention')",
                    (_iso(), _sanitize_error(error), delivery_id),
                )
                if cursor.rowcount != 1:
                    raise InvalidTransition("delivery cannot enter terminal failure")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def reopen_collection_retry(
        self, delivery_id: int, *, now: datetime | None = None, owner_id: str | None = None
    ) -> DeliveryInfo:
        self._ensure_writable()
        owner_id = self._require_runtime_owner(owner_id)
        self._assert_lease_owner(owner_id)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                cursor = self.connection.execute(
                    "UPDATE deliveries SET state='collecting',next_attempt_at=NULL,terminal_error='' WHERE delivery_id=? AND kind='collection_retry' AND state='retry_wait' AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
                    (delivery_id, _iso(now)),
                )
                if cursor.rowcount != 1:
                    raise RetryNotDue("collection retry is not due")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def reopen_for_collection(
        self, delivery_id: int, *, now: datetime | None = None, owner_id: str | None = None
    ) -> DeliveryInfo:
        """Reopen a retrying collection delivery without changing its generation."""
        self._ensure_writable()
        owner_id = self._require_runtime_owner(owner_id)
        self._assert_lease_owner(owner_id)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner_in_transaction(owner_id)
                cursor = self.connection.execute(
                    "UPDATE deliveries SET state='collecting',next_attempt_at=NULL,terminal_error='' WHERE delivery_id=? AND state='retry_wait' AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
                    (delivery_id, _iso(now)),
                )
                if cursor.rowcount != 1:
                    raise RetryNotDue("delivery retry is not due")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def resolve_chunk(
        self,
        chunk_id: int,
        resolution: str,
        *,
        reason: str,
        operator: str,
        maintenance_context: MaintenanceContext | None = None,
    ) -> DeliveryInfo:
        self._ensure_writable()
        context = maintenance_context or self._maintenance_context
        if context is None:
            raise StateError("manual chunk resolution requires exclusive maintenance authority")
        if self._maintenance_context is not context:
            raise StateError("manual chunk resolution context is not attached to this state store")
        if resolution not in {"sent", "retry"}:
            raise ValueError("resolution must be sent or retry")
        reason = _sanitize_error(reason, 500)
        operator = _sanitize_error(operator, 160)
        if not reason or not operator:
            raise StateError("manual chunk resolution requires a nonempty reason and operator")
        now = _iso()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_maintenance_authority_in_transaction()
                row = self.connection.execute(
                    "SELECT delivery_id,state,error_class,attempt_count FROM outbox_chunks WHERE chunk_id=?", (chunk_id,)
                ).fetchone()
                if not row or row[1] not in {"ambiguous", "failed_terminal"}:
                    raise InvalidTransition("only an ambiguous or explicitly retry-safe terminal chunk can be resolved")
                delivery_id = int(row[0])
                if row[1] == "failed_terminal":
                    if resolution != "retry":
                        raise InvalidTransition("a terminal chunk can only receive an audited retry authorization")
                    if str(row[2]) not in {"telegram_terminal_retry_safe", "operator_retry_safe"}:
                        raise InvalidTransition("terminal chunk is not allowlisted for an audited retry")
                    if self.connection.execute(
                        "SELECT 1 FROM outbox_chunks WHERE delivery_id=? AND state IN ('ambiguous','in_flight') LIMIT 1",
                        (delivery_id,),
                    ).fetchone():
                        raise InvalidTransition("delivery has unresolved or in-flight work")
                    if self.connection.execute(
                        "SELECT 1 FROM delivery_resolutions WHERE chunk_id=? AND resolution='retry' AND reason LIKE 'terminal_retry:%' LIMIT 1",
                        (chunk_id,),
                    ).fetchone():
                        raise InvalidTransition("terminal retry authorization has already been consumed")
                    reason = f"terminal_retry:{reason}"
                self.connection.execute(
                    "INSERT INTO delivery_resolutions(chunk_id,resolution,reason,operator,resolved_at) VALUES (?,?,?,?,?)",
                    (chunk_id, resolution, reason, operator, now),
                )
                if resolution == "sent" and row[1] == "ambiguous":
                    self.connection.execute(
                        "UPDATE outbox_chunks SET state='sent',sent_at=?,error_class='',error_text='' WHERE chunk_id=? AND state='ambiguous'",
                        (now, chunk_id),
                    )
                    self._record_chunk_history(delivery_id, chunk_id, now)
                    remaining = self.connection.execute(
                        "SELECT COUNT(*) FROM outbox_chunks WHERE delivery_id=? AND state NOT IN ('sent')", (delivery_id,)
                    ).fetchone()[0]
                    if remaining == 0:
                        empty = (
                            self.connection.execute("SELECT COUNT(*) FROM delivery_items WHERE delivery_id=?", (delivery_id,)).fetchone()[0]
                            == 0
                        )
                        self.connection.execute(
                            "UPDATE deliveries SET state=?,completed_at=?,terminal_error='' WHERE delivery_id=?",
                            ("completed_empty" if empty else "completed", now, delivery_id),
                        )
                    else:
                        self.connection.execute(
                            "UPDATE deliveries SET state='sending',terminal_error='' WHERE delivery_id=?", (delivery_id,)
                        )
                elif resolution == "retry":
                    self.connection.execute(
                        "UPDATE outbox_chunks SET state='pending',next_attempt_at=NULL,in_flight_at=NULL,error_class='',error_text='' WHERE chunk_id=? AND state IN ('ambiguous','failed_terminal')",
                        (chunk_id,)
                    )
                    self.connection.execute("UPDATE deliveries SET state='sending',terminal_error='' WHERE delivery_id=?", (delivery_id,))
                else:
                    raise InvalidTransition("terminal chunk cannot be marked sent without remote evidence")
                self._record_transition(
                    "chunk",
                    chunk_id,
                    str(row[1]),
                    "sent" if resolution == "sent" else "pending",
                    actor_type="operator",
                    actor_id=operator,
                    reason=reason,
                )
                delivery_after = self.connection.execute(
                    "SELECT state FROM deliveries WHERE delivery_id=?", (delivery_id,)
                ).fetchone()
                if delivery_after is not None:
                    self._record_transition(
                        "delivery",
                        delivery_id,
                        "needs_attention" if row[1] == "ambiguous" else "failed_terminal",
                        str(delivery_after[0]),
                        actor_type="operator",
                        actor_id=operator,
                        reason=reason,
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def status_snapshot(self, delivery_date: str | None = None) -> dict[str, Any]:
        active = self.active_delivery(delivery_date) if delivery_date else self._latest_active()
        unresolved = self.connection.execute("SELECT COUNT(*) FROM outbox_chunks WHERE state='ambiguous'").fetchone()[0]
        last_success = self.connection.execute(
            "SELECT MAX(completed_at) FROM deliveries WHERE state IN ('completed','completed_empty')"
        ).fetchone()[0]
        last_attempt = self.connection.execute("SELECT MAX(started_at) FROM delivery_attempts").fetchone()[0]
        next_retry = self.connection.execute("SELECT MIN(next_attempt_at) FROM deliveries WHERE state='retry_wait'").fetchone()[0]
        attempt_row = self.connection.execute(
            "SELECT attempt_id,outcome,error_class,error_text,started_at,ended_at FROM delivery_attempts ORDER BY attempt_id DESC LIMIT 1"
        ).fetchone()
        chunk_row = None
        if active:
            chunk_row = self.connection.execute(
                "SELECT chunk_id,sequence,state,attempt_count FROM outbox_chunks "
                "WHERE delivery_id=? ORDER BY CASE state WHEN 'in_flight' THEN 0 WHEN 'ambiguous' THEN 1 "
                "WHEN 'retry_wait' THEN 2 WHEN 'pending' THEN 3 ELSE 4 END, sequence LIMIT 1",
                (active.delivery_id,),
            ).fetchone()
        return {
            "schema_version": self.schema_version,
            "application_version": __version__,
            "lease": self.lease_info(),
            "scheduler_lease": self.lease_info("scheduler"),
            "active_delivery": active.__dict__ if active and hasattr(active, "__dict__") else self._delivery_dict(active),
            "latest_delivery": self._delivery_dict(self._latest_delivery()),
            "unresolved_ambiguity_count": int(unresolved),
            "last_attempt_at": last_attempt,
            "last_success_at": last_success,
            "last_successful_delivery": last_success,
            "next_retry_at": next_retry,
            "last_attempt": {
                "attempt_id": int(attempt_row[0]),
                "outcome": str(attempt_row[1]),
                "error_class": str(attempt_row[2]),
                "error_text": str(attempt_row[3]),
                "started_at": str(attempt_row[4]),
                "ended_at": attempt_row[5],
            }
            if attempt_row
            else None,
            "active_chunk": {
                "chunk_id": int(chunk_row[0]),
                "sequence": int(chunk_row[1]),
                "state": str(chunk_row[2]),
                "attempt_count": int(chunk_row[3]),
            }
            if chunk_row
            else None,
            "integrity": self.integrity_check(),
        }

    def prune_history(
        self,
        *,
        now: datetime | None = None,
        attempt_retention_days: int = 90,
        article_retention_days: int = 365,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Preview or apply bounded retention without touching unresolved work.

        Retention is an exclusive maintenance operation.  Attempts and source
        observations are retained for the configured window; article identity
        history is retained longer.  Rows belonging to active, retrying,
        ambiguous, or otherwise operator-attention work are never eligible,
        even when their timestamps are old.
        """

        if not isinstance(attempt_retention_days, int) or isinstance(attempt_retention_days, bool) or attempt_retention_days < 1:
            raise ValueError("attempt_retention_days must be a positive integer")
        if not isinstance(article_retention_days, int) or isinstance(article_retention_days, bool) or article_retention_days < 1:
            raise ValueError("article_retention_days must be a positive integer")
        self._ensure_writable()
        if self._maintenance_context is None:
            raise StateError("history pruning requires exclusive maintenance authority")
        current = _utc(now)
        attempt_cutoff = _iso(current - timedelta(days=attempt_retention_days))
        article_cutoff = _iso(current - timedelta(days=article_retention_days))
        previous_epoch = self._maintenance_context.database_epoch
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_maintenance_authority_in_transaction()
                attempt_query = (
                    "SELECT COUNT(*) FROM delivery_attempts a "
                    "JOIN deliveries d ON d.delivery_id=a.delivery_id "
                    "WHERE a.ended_at IS NOT NULL AND a.ended_at < ? "
                    "AND d.state NOT IN ('collecting','prepared','prepared_empty','sending','retry_wait','needs_attention') "
                    "AND NOT EXISTS (SELECT 1 FROM outbox_chunks c WHERE c.delivery_id=a.delivery_id AND c.state IN ('in_flight','ambiguous'))"
                )
                source_query = (
                    "SELECT COUNT(*) FROM source_results s "
                    "LEFT JOIN deliveries d ON d.delivery_id=s.delivery_id "
                    "WHERE s.created_at < ? AND (d.delivery_id IS NULL OR d.state NOT IN "
                    "('collecting','prepared','prepared_empty','sending','retry_wait','needs_attention'))"
                )
                article_query = (
                    "SELECT COUNT(*) FROM article_history h "
                    "JOIN deliveries d ON d.delivery_id=h.delivery_id "
                    "WHERE h.sent_at < ? AND d.state IN ('completed','completed_empty','failed_terminal') "
                    "AND NOT EXISTS (SELECT 1 FROM outbox_chunks c WHERE c.delivery_id=h.delivery_id AND c.state IN ('in_flight','ambiguous'))"
                )
                counts = {
                    "delivery_attempts": int(self.connection.execute(attempt_query, (attempt_cutoff,)).fetchone()[0] or 0),
                    "source_results": int(self.connection.execute(source_query, (attempt_cutoff,)).fetchone()[0] or 0),
                    "article_history": int(self.connection.execute(article_query, (article_cutoff,)).fetchone()[0] or 0),
                }
                if apply:
                    self.connection.execute(
                        "DELETE FROM delivery_attempts WHERE attempt_id IN (SELECT a.attempt_id FROM delivery_attempts a "
                        "JOIN deliveries d ON d.delivery_id=a.delivery_id WHERE a.ended_at IS NOT NULL AND a.ended_at < ? "
                        "AND d.state NOT IN ('collecting','prepared','prepared_empty','sending','retry_wait','needs_attention') "
                        "AND NOT EXISTS (SELECT 1 FROM outbox_chunks c WHERE c.delivery_id=a.delivery_id AND c.state IN ('in_flight','ambiguous')))",
                        (attempt_cutoff,),
                    )
                    self.connection.execute(
                        "DELETE FROM source_results WHERE source_result_id IN (SELECT s.source_result_id FROM source_results s "
                        "LEFT JOIN deliveries d ON d.delivery_id=s.delivery_id WHERE s.created_at < ? AND (d.delivery_id IS NULL OR d.state NOT IN "
                        "('collecting','prepared','prepared_empty','sending','retry_wait','needs_attention')))",
                        (attempt_cutoff,),
                    )
                    self.connection.execute(
                        "DELETE FROM article_history WHERE history_id IN (SELECT h.history_id FROM article_history h "
                        "JOIN deliveries d ON d.delivery_id=h.delivery_id WHERE h.sent_at < ? AND d.state IN ('completed','completed_empty','failed_terminal') "
                        "AND NOT EXISTS (SELECT 1 FROM outbox_chunks c WHERE c.delivery_id=h.delivery_id AND c.state IN ('in_flight','ambiguous')))",
                        (article_cutoff,),
                    )
                    self.connection.commit()
                else:
                    self.connection.rollback()
                    # The fence row is intentionally part of the rolled-back
                    # preview transaction.  Do not leave the context pointing
                    # at an epoch that was never committed.
                    if previous_epoch is None:
                        self._maintenance_context._database_epoch = None
            except Exception:
                self.connection.rollback()
                raise
        return {
            "applied": bool(apply),
            "attempt_cutoff": attempt_cutoff,
            "article_cutoff": article_cutoff,
            "counts": counts,
            "unresolved_count": self.unresolved_count(),
        }

    def _latest_active(self) -> DeliveryInfo | None:
        row = self.connection.execute(
            "SELECT delivery_id,delivery_date,generation,kind,state,run_id,config_hash,target_snapshot,next_attempt_at,terminal_error FROM deliveries WHERE state NOT IN ('completed','completed_empty','failed_terminal') ORDER BY delivery_id DESC LIMIT 1"
        ).fetchone()
        return self._delivery(row)

    def _latest_delivery(self) -> DeliveryInfo | None:
        row = self.connection.execute(
            "SELECT delivery_id,delivery_date,generation,kind,state,run_id,config_hash,target_snapshot,next_attempt_at,terminal_error FROM deliveries ORDER BY delivery_id DESC LIMIT 1"
        ).fetchone()
        return self._delivery(row)

    @staticmethod
    def _delivery_dict(delivery: DeliveryInfo | None) -> dict[str, Any] | None:
        if delivery is None:
            return None
        return {
            "delivery_id": delivery.delivery_id,
            "delivery_date": delivery.delivery_date,
            "generation": delivery.generation,
            "kind": delivery.kind,
            "state": delivery.state,
            "run_id": delivery.run_id,
            "config_hash": delivery.config_hash,
            "target_snapshot": delivery.target_snapshot,
            "next_attempt_at": delivery.next_attempt_at,
            "terminal_error": delivery.terminal_error,
        }
