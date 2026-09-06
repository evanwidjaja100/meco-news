"""Versioned SQLite state, leases, immutable deliveries, and outbox chunks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Any
from collections.abc import Iterable, Mapping, Sequence

from . import __version__
from .migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATION_SQL,
    SCHEMA_SQL,
    MigrationGuard,
    MigrationNotPermitted,
    ledger_contiguity_issue,
    migration_checksum,
    verify_catalog,
)
from .observability import redact as _redact_log_value
from .models import NewsItem, canonical_url


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
    "database schema requires migration (migration_required); automatic migration is disabled, "
    "use the audited migrate command"
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


def _apply_schema_to(connection: sqlite3.Connection) -> None:
    # The schema file contains only standalone CREATE statements. Executing
    # them individually preserves the surrounding transaction; SQLite's
    # executescript helper would implicitly commit before running it.
    for statement in SCHEMA_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)


def _record_migration_to(connection: sqlite3.Connection, version: int, app_version: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (?, ?, ?, ?)",
        (version, migration_checksum(version), _iso(), app_version),
    )


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
            for statement in MIGRATION_SQL[version].split(";"):
                if statement.strip():
                    connection.execute(statement)
            _record_migration_to(connection, version, app_version)
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

    def __init__(self, path: str | Path, *, readonly: bool = False, busy_timeout_ms: int = 5_000):
        memory_only = str(path) == ":memory:"
        self.readonly = readonly and not memory_only
        self.path = None if memory_only else Path(path)
        if self.path and not self.readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.readonly:
            if not self.path or not self.path.exists():
                raise FileNotFoundError(str(self.path))
            # URI mode keeps a dry-run from creating or migrating a database.
            # immutable=1 additionally keeps the read path from creating -shm/-wal
            # sidecars; verify-after-write callers hold no concurrent writer, and live
            # readers (preflight/health/status) are best-effort point-in-time checks.
            self.connection = sqlite3.connect(
                f"file:{self.path.resolve().as_posix()}?mode=ro&immutable=1", uri=True, timeout=busy_timeout_ms / 1000
            )
        else:
            database_path = ":memory:" if memory_only else self.path
            if database_path is None:
                raise StateError("state database path is not available")
            self.connection = sqlite3.connect(database_path, timeout=busy_timeout_ms / 1000)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            if not self.readonly:
                # C2.1: verify (read-only SELECTs) before enabling WAL, so a
                # refused open cannot change a single byte of the database.
                self._ensure_schema()
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            # A refused open must not leak a locked connection (C2.1).
            self.connection.close()
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
                _record_migration_to(self.connection, 1, __version__)
                _record_migration_to(self.connection, 2, __version__)
                _record_migration_to(self.connection, 3, __version__)
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
            }
            missing = sorted(required - tables)
            if missing:
                raise StateError(f"database schema is missing required table(s): {', '.join(missing)}")
            cols = {r[1] for r in self.connection.execute("PRAGMA table_info(deliveries)").fetchall()}
            if "target_snapshot" not in cols:
                # A current-ledger database without the v3 column needs the audited migration path.
                raise MigrationRequiredError(MIGRATION_REQUIRED_MESSAGE)
            return
        raise MigrationRequiredError(MIGRATION_REQUIRED_MESSAGE)

    def _backup_before_migrate(self) -> Path | None:
        if not self.path or not self.path.exists():
            return None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"{self.path.name}.pre-migrate-{stamp}.bak")
        counter = 1
        while target.exists():
            target = self.path.with_name(f"{self.path.name}.pre-migrate-{stamp}-{counter}.bak")
            counter += 1
        self.backup_to(target)
        return target

    def _ensure_writable(self) -> None:
        if self.readonly:
            raise DatabaseReadOnly("state store is read-only")

    def close(self) -> None:
        self.connection.close()

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
        try:
            with destination:
                self.connection.backup(destination)
        finally:
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
    ) -> DeliveryInfo:
        self._ensure_writable()
        with self._lock:
            if generation is None:
                generation = self.latest_generation(delivery_date) + 1
            run_id = run_id or str(uuid.uuid4())
            now = _iso()
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "INSERT INTO deliveries(delivery_date,generation,kind,state,run_id,config_hash,target_snapshot,started_at) VALUES (?,?,?,?,?,?,?,?)",
                    (delivery_date, generation, kind, state, run_id, config_hash, target_snapshot, now),
                )
                delivery_id = int(self.connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def start_run(self, delivery_date: str) -> None:
        """Compatibility wrapper for the pre-v2 storage API."""
        self._ensure_writable()
        active = self.active_delivery(delivery_date)
        if active:
            return
        if self.already_completed(delivery_date):
            return
        self.create_delivery(delivery_date, generation=max(0, self.latest_generation(delivery_date) + 1), state="collecting")

    def complete_run(self, delivery_date: str, items: Iterable[NewsItem]) -> None:
        """Compatibility wrapper that records an already-acknowledged run."""
        self._ensure_writable()
        item_list = list(items)
        active = self.active_delivery(delivery_date)
        if active is None:
            active = self.create_delivery(
                delivery_date, generation=max(0, self.latest_generation(delivery_date) + 1), kind="content", state="collecting"
            )
        now = _iso()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                for position, item in enumerate(item_list):
                    self.connection.execute(
                        "INSERT OR IGNORE INTO delivery_items(delivery_id,position,fingerprint,url_key,title_key,title,url,source,source_url,published_at,summary,collector,query_name,score,topic,topic_label,relevance_reason,matches_json,chunk_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        self._item_row(active.delivery_id, position, item, 0),
                    )
                    self.connection.execute(
                        "INSERT OR IGNORE INTO article_history(url_key,title_key,fingerprint,delivery_id,chunk_id,title,url,source,published_at,sent_at) VALUES (?,?,?,?,NULL,?,?,?,?,?)",
                        (
                            item.url_key,
                            item.title_key,
                            item.fingerprint,
                            active.delivery_id,
                            item.title[:512],
                            item.url[:2048],
                            item.source[:160],
                            item.published_at.isoformat() if item.published_at else None,
                            now,
                        ),
                    )
                state = "completed" if item_list else "completed_empty"
                self.connection.execute(
                    "UPDATE deliveries SET state=?,prepared_at=COALESCE(prepared_at,?),completed_at=? WHERE delivery_id=? AND state NOT IN ('completed','completed_empty')",
                    (state, now, now, active.delivery_id),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def fail_run(self, delivery_date: str, error: str) -> None:
        self._ensure_writable()
        active = self.active_delivery(delivery_date)
        if not active:
            active = self.create_delivery(delivery_date, generation=max(0, self.latest_generation(delivery_date) + 1), state="collecting")
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET state='failed_terminal', completed_at=?, terminal_error=? WHERE delivery_id=? AND state NOT IN ('completed','completed_empty')",
                (_iso(), _sanitize_error(error), active.delivery_id),
            )

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
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE run_leases SET heartbeat_at=?,expires_at=? WHERE scope=? AND owner_id=?",
                (_iso(current), _iso(current + timedelta(seconds=ttl_seconds)), scope, owner_id),
            )
        if cursor.rowcount != 1:
            raise LeaseLost("lease owner is no longer active")
        return _iso(current + timedelta(seconds=ttl_seconds))

    def release_lease(self, scope: str, owner_id: str) -> bool:
        self._ensure_writable()
        with self.connection:
            cursor = self.connection.execute("DELETE FROM run_leases WHERE scope=? AND owner_id=?", (scope, owner_id))
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

    def recover_expired_lease(self, scope: str = "delivery", *, now: datetime | None = None) -> bool:
        """Mark in-flight chunks ambiguous before an expired lease is reused."""
        self._ensure_writable()
        info = self.lease_info(scope)
        if not info or info["expires_at"] > _iso(now):
            return False
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self.connection.execute("SELECT chunk_id,delivery_id FROM outbox_chunks WHERE state='in_flight'").fetchall()
                for row in rows:
                    self.connection.execute(
                        "UPDATE outbox_chunks SET state='ambiguous',error_class='telegram_ambiguous',error_text='lease expired while request was in flight' WHERE chunk_id=? AND state='in_flight'",
                        (row[0],),
                    )
                    self.connection.execute(
                        "UPDATE deliveries SET state='needs_attention' WHERE delivery_id=? AND state NOT IN ('completed','completed_empty','failed_terminal')",
                        (row[1],),
                    )
                self.connection.execute("DELETE FROM run_leases WHERE scope=? AND expires_at<=?", (scope, _iso(now)))
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return bool(rows)

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
                self._assert_lease_owner(owner_id)
                delivery = self.delivery(delivery_id)
                if not delivery or delivery.state not in {"collecting", "retry_wait"}:
                    raise InvalidTransition("delivery is not preparable")
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
                    bounded_payload = str(payload)
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

    def record_source_results(self, delivery_id: int | None, results: Iterable[Any]) -> None:
        self._ensure_writable()
        now = _iso()
        with self.connection:
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
                self._assert_lease_owner(owner_id)
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
                delivery = self.delivery(int(row[1]))
                if not delivery or delivery.state in {"needs_attention", "completed", "completed_empty", "failed_terminal"}:
                    raise InvalidTransition("delivery cannot send this chunk")
                attempt_number = int(row[6]) + 1
                self.connection.execute(
                    "UPDATE outbox_chunks SET state='in_flight',attempt_count=?,in_flight_at=?,error_class='',error_text='' WHERE chunk_id=? AND state IN ('pending','retry_wait')",
                    (attempt_number, now_text, chunk_id),
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
        now = _iso()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner(owner_id)
                row = self.connection.execute(
                    "SELECT delivery_id,sequence,attempt_count,state FROM outbox_chunks WHERE chunk_id=?", (chunk_id,)
                ).fetchone()
                if not row or row[3] != "in_flight":
                    raise InvalidTransition("chunk is not in flight")
                delivery_id = int(row[0])
                attempt_number = int(row[2])
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
    ) -> DeliveryInfo:
        self._ensure_writable()
        active = self.active_delivery(delivery_date)
        if active and active.kind == "collection_retry":
            with self.connection:
                self.connection.execute(
                    "UPDATE deliveries SET state='retry_wait',next_attempt_at=?,terminal_error=? WHERE delivery_id=?",
                    (_iso(next_attempt_at), _sanitize_error(error), active.delivery_id),
                )
            return self.delivery(active.delivery_id)  # type: ignore[return-value]
        created = self.create_delivery(delivery_date, kind="collection_retry", run_id=run_id, config_hash=config_hash, state="retry_wait")
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET next_attempt_at=?,terminal_error=? WHERE delivery_id=?",
                (_iso(next_attempt_at), _sanitize_error(error), created.delivery_id),
            )
        return self.delivery(created.delivery_id)  # type: ignore[return-value]

    def set_collection_retry(self, delivery_id: int, *, next_attempt_at: datetime, error: str) -> DeliveryInfo:
        self._ensure_writable()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE deliveries SET state='retry_wait',next_attempt_at=?,terminal_error=? WHERE delivery_id=? AND state IN ('collecting','retry_wait')",
                (_iso(next_attempt_at), _sanitize_error(error), delivery_id),
            )
        if cursor.rowcount != 1:
            raise InvalidTransition("delivery cannot enter collection retry")
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def collection_retry_count(self, delivery_id: int) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM delivery_attempts WHERE delivery_id=? AND chunk_id IS NULL", (delivery_id,)
        ).fetchone()
        return int(row[0] or 0)

    def record_collection_attempt(self, delivery_id: int, *, run_id: str, error: str, outcome: str) -> int:
        self._ensure_writable()
        attempt_number = self.collection_retry_count(delivery_id) + 1
        now = _iso()
        with self.connection:
            self.connection.execute(
                "INSERT INTO delivery_attempts(delivery_id,chunk_id,attempt_number,started_at,ended_at,outcome,error_class,error_text,run_id) VALUES (?,?,?, ?,?,?,?, ?,?)",
                (delivery_id, None, attempt_number, now, now, outcome, "all_sources_failed", _sanitize_error(error, 1000), run_id),
            )
        return attempt_number

    def fail_delivery(self, delivery_id: int, error: str) -> DeliveryInfo:
        self._ensure_writable()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE deliveries SET state='failed_terminal',completed_at=?,terminal_error=? WHERE delivery_id=? AND state NOT IN ('completed','completed_empty','failed_terminal')",
                (_iso(), _sanitize_error(error), delivery_id),
            )
        if cursor.rowcount != 1:
            raise InvalidTransition("delivery cannot enter terminal failure")
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def reopen_collection_retry(self, delivery_id: int, *, now: datetime | None = None) -> DeliveryInfo:
        self._ensure_writable()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE deliveries SET state='collecting',next_attempt_at=NULL,terminal_error='' WHERE delivery_id=? AND kind='collection_retry' AND state='retry_wait' AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
                (delivery_id, _iso(now)),
            )
        if cursor.rowcount != 1:
            raise RetryNotDue("collection retry is not due")
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def reopen_for_collection(self, delivery_id: int, *, now: datetime | None = None) -> DeliveryInfo:
        """Reopen a retrying collection delivery without changing its generation."""
        self._ensure_writable()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE deliveries SET state='collecting',next_attempt_at=NULL,terminal_error='' WHERE delivery_id=? AND state='retry_wait' AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
                (delivery_id, _iso(now)),
            )
        if cursor.rowcount != 1:
            raise RetryNotDue("delivery retry is not due")
        return self.delivery(delivery_id)  # type: ignore[return-value]

    def resolve_chunk(self, chunk_id: int, resolution: str, *, owner_id: str, reason: str, operator: str) -> DeliveryInfo:
        self._ensure_writable()
        self._assert_lease_owner(owner_id)
        if resolution not in {"sent", "retry"}:
            raise ValueError("resolution must be sent or retry")
        reason = _sanitize_error(reason, 500)
        operator = _sanitize_error(operator, 160)
        now = _iso()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_lease_owner(owner_id)
                row = self.connection.execute("SELECT delivery_id,state FROM outbox_chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
                if not row or row[1] != "ambiguous":
                    raise InvalidTransition("only an ambiguous chunk can be resolved")
                delivery_id = int(row[0])
                self.connection.execute(
                    "INSERT INTO delivery_resolutions(chunk_id,resolution,reason,operator,resolved_at) VALUES (?,?,?,?,?)",
                    (chunk_id, resolution, reason, operator, now),
                )
                if resolution == "sent":
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
                else:
                    self.connection.execute(
                        "UPDATE outbox_chunks SET state='pending',next_attempt_at=NULL,in_flight_at=NULL,error_class='',error_text='' WHERE chunk_id=? AND state='ambiguous'",
                        (chunk_id,),
                    )
                    self.connection.execute("UPDATE deliveries SET state='sending',terminal_error='' WHERE delivery_id=?", (delivery_id,))
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
