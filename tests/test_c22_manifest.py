"""C2.2b offline manifested migration tests (closure plan C2.2)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from meco_news import migrate as migrate_module
from meco_news.maintenance import MaintenanceContext, MaintenanceError
from meco_news.migrate import (
    MigrationBackup,
    _is_current_and_complete,
    _read_source_state,
    create_migration_manifest,
    run_guarded_migrations,
    verify_migration_manifest,
)
from meco_news.migrations import CURRENT_SCHEMA_VERSION, MIGRATION_SQL, SCHEMA_SQL, migration_checksum
from meco_news.storage import StateError, StateStore


def _pre_migrate_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".pre-migrate-" in p.name)


def _fetch_all(path: Path, sql: str) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(path)
    try:
        return [tuple(row) for row in connection.execute(sql).fetchall()]
    finally:
        connection.close()


def _make_legacy_v1(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(MIGRATION_SQL[1])
        connection.execute(
            "INSERT INTO sent_articles(fingerprint,title,url,source,topic,score,sent_at,delivery_date)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                "fp1",
                "LPG terminal",
                "https://example.com/lpg",
                "Example",
                "lpg_energy",
                10,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01",
            ),
        )
        connection.execute(
            "INSERT INTO runs(delivery_date,started_at,completed_at,status,item_count,error)"
            " VALUES (?,?,?,?,?,?)",
            (
                "2026-01-01",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T01:00:00+00:00",
                "completed",
                1,
                "",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _make_v2(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(MIGRATION_SQL[1])
        connection.executescript(MIGRATION_SQL[2])
        for version in (1, 2):
            connection.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (?,?,?,?)",
                (version, migration_checksum(version), "2026-01-01T00:00:00+00:00", "1.0.0"),
            )
        connection.commit()
    finally:
        connection.close()


def _make_current(path: Path) -> None:
    with StateStore(path):
        pass


class TestMigrationManifest(unittest.TestCase):
    def test_manifest_contains_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            self.assertIsInstance(artifact, MigrationBackup)
            self.assertTrue(artifact.backup.is_file())
            self.assertTrue(artifact.manifest.is_file())
            self.assertTrue(artifact.backup_id)
            self.assertEqual(artifact.integrity, "ok")
            self.assertEqual(artifact.schema_version, 0)
            self.assertEqual(artifact.app_version, "2.0.0")
            self.assertEqual(artifact.config_hash, "abc123")
            payload = json.loads(artifact.manifest.read_text(encoding="utf-8"))
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
                self.assertIn(field, payload)
            self.assertEqual(payload["backup_id"], artifact.backup_id)
            self.assertEqual(payload["db_sha256"], artifact.db_sha256)

    def test_manifest_leaves_source_bytes_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            verify_migration_manifest(path, artifact.manifest)
            self.assertEqual(path.read_bytes(), before)

    def test_manifest_backup_is_logically_equal_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            source_articles = _fetch_all(path, "SELECT fingerprint, title FROM sent_articles")
            source_runs = _fetch_all(path, "SELECT delivery_date, status FROM runs")
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(_fetch_all(artifact.backup, "SELECT fingerprint, title FROM sent_articles"), source_articles)
            self.assertEqual(_fetch_all(artifact.backup, "SELECT delivery_date, status FROM runs"), source_runs)

    def test_verify_rejects_tampered_sha(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            payload = json.loads(artifact.manifest.read_text(encoding="utf-8"))
            payload["db_sha256"] = "0" * 64
            artifact.manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(StateError):
                verify_migration_manifest(path, artifact.manifest)

    def test_verify_rejects_missing_backup(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            artifact.backup.unlink()
            with self.assertRaises(FileNotFoundError):
                verify_migration_manifest(path, artifact.manifest)

    def test_manifest_garbage_db_fails_closed_without_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            path.write_bytes(b"\x00\x01not-a-sqlite-database" * 64)
            with self.assertRaises(StateError):
                create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(_pre_migrate_files(Path(d)), [])


class TestGuardedMigrationFence(unittest.TestCase):
    def test_run_refuses_wrong_path_context(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            first = Path(d) / "first.db"
            second = Path(d) / "second.db"
            _make_legacy_v1(first)
            _make_legacy_v1(second)
            before = second.read_bytes()
            with (
                MaintenanceContext.acquire(first, owner="m1") as ctx,
                self.assertRaises(MaintenanceError),
            ):
                run_guarded_migrations(second, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(second.read_bytes(), before)
            self.assertEqual(_pre_migrate_files(Path(d)), [])

    def test_run_refuses_released_context(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            ctx = MaintenanceContext.acquire(path, owner="m1")
            ctx.release()
            before = path.read_bytes()
            with self.assertRaises(MaintenanceError):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_pre_migrate_files(Path(d)), [])

    def test_run_refuses_wrong_scope(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            with (
                MaintenanceContext.acquire(path, owner="m1", scope="restore") as ctx,
                self.assertRaises(MaintenanceError),
            ):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_pre_migrate_files(Path(d)), [])

    def test_run_aborts_when_verify_fails_leaves_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            with (
                MaintenanceContext.acquire(path, owner="m1") as ctx,
                patch("meco_news.migrate.verify_migration_manifest", side_effect=StateError("boom")),
                self.assertRaises(StateError),
            ):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)


class TestGuardedMigrationRun(unittest.TestCase):
    def test_run_migrates_legacy_v1_to_current(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            with MaintenanceContext.acquire(path, owner="m1") as ctx:
                applied = run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(applied, CURRENT_SCHEMA_VERSION)
            files = _pre_migrate_files(Path(d))
            self.assertTrue(any(name.endswith(".bak") for name in files))
            self.assertTrue(any(name.endswith(".manifest.json") for name in files))
            with StateStore(path) as store:
                self.assertEqual(store.schema_version, CURRENT_SCHEMA_VERSION)
                rows = store.connection.execute(
                    "SELECT delivery_date FROM deliveries WHERE delivery_date=?", ("2026-01-01",)
                ).fetchall()
                self.assertEqual(len(rows), 1)

    def test_run_migrates_v2_to_current(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            with MaintenanceContext.acquire(path, owner="m1") as ctx:
                applied = run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(applied, 1)
            with StateStore(path) as store:
                self.assertEqual(store.schema_version, CURRENT_SCHEMA_VERSION)

    def test_run_current_is_audited_noop_without_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            before = path.read_bytes()
            with MaintenanceContext.acquire(path, owner="m1") as ctx:
                applied = run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(applied, 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_pre_migrate_files(Path(d)), [])


class _FakeCursor:
    def __init__(self, rows: list[tuple[str, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[str, ...]:
        return self._rows[0]

    def fetchall(self) -> list[tuple[str, ...]]:
        return list(self._rows)

    def __iter__(self):  # noqa: ANN204
        return iter(self._rows)


class _FailingConnection:
    """Wrap a real connection; fail closed on configured statements."""

    def __init__(self, real: sqlite3.Connection, *, fail_sql: str = "", fail_integrity_call: int = 0) -> None:
        self._real = real
        self._fail_sql = fail_sql
        self._fail_integrity_call = fail_integrity_call
        self._integrity_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_real", "_fail_sql", "_fail_integrity_call", "_integrity_calls"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)

    def execute(self, sql: Any, *args: Any, **kwargs: Any) -> Any:
        text = str(sql)
        if self._fail_sql and self._fail_sql in text:
            raise sqlite3.DatabaseError("injected failure")
        if "integrity_check" in text and self._fail_integrity_call:
            self._integrity_calls += 1
            if self._integrity_calls >= self._fail_integrity_call:
                return _FakeCursor([("database disk image is malformed",)])
        return self._real.execute(sql, *args, **kwargs)


class _BackupBombConnection(_FailingConnection):
    def backup(self, *args: Any, **kwargs: Any) -> Any:
        raise sqlite3.DatabaseError("injected backup failure")


def _writer_failing_connect(real_connect: Any, *, fail_sql: str = "", fail_integrity_call: int = 0) -> Any:
    """Route only the migration writer through a failing proxy.

    Read-only manifest/verify connections use mode=ro URIs and stay real;
    the destination backup connection (first non-URI open) stays real; the
    writer (second non-URI open) fails on the configured statements.
    """
    state = {"non_uri": 0}

    def fake_connect(target: Any, *args: Any, **kwargs: Any) -> Any:
        connection = real_connect(target, *args, **kwargs)
        if kwargs.get("uri"):
            return connection
        state["non_uri"] += 1
        if state["non_uri"] < 2:
            return connection
        return _FailingConnection(connection, fail_sql=fail_sql, fail_integrity_call=fail_integrity_call)

    return fake_connect


def _mock_ro_connection(handler: Any) -> Any:
    connection = MagicMock()
    connection.execute.side_effect = handler
    return connection


def _ok_cursor() -> _FakeCursor:
    return _FakeCursor([("ok",)])


class TestReadSourceState(unittest.TestCase):
    def test_legacy_reports_version_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            self.assertEqual(_read_source_state(path), ("ok", 0))

    def test_empty_ledger_reports_version_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(SCHEMA_SQL)
                connection.execute("DELETE FROM schema_migrations")
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(_read_source_state(path), ("ok", 0))

    def test_open_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            with patch.object(
                migrate_module, "_ro_connection", side_effect=sqlite3.OperationalError("boom")
            ), self.assertRaises(StateError):
                _read_source_state(path)

    def test_integrity_probe_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)

            def _boom(sql: Any, *args: Any, **kwargs: Any) -> Any:
                raise sqlite3.DatabaseError("boom")

            with patch.object(
                migrate_module, "_ro_connection", return_value=_mock_ro_connection(_boom)
            ), self.assertRaises(StateError):
                _read_source_state(path)

    def test_integrity_not_ok_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            mock_connection = _mock_ro_connection(lambda sql, *args, **kwargs: _FakeCursor([("corrupt",)]))
            with patch.object(
                migrate_module, "_ro_connection", return_value=mock_connection
            ), self.assertRaises(StateError):
                _read_source_state(path)

    def test_schema_probe_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)

            def _handler(sql: Any, *args: Any, **kwargs: Any) -> Any:
                if "integrity_check" in str(sql):
                    return _ok_cursor()
                raise sqlite3.DatabaseError("boom")

            with patch.object(
                migrate_module, "_ro_connection", return_value=_mock_ro_connection(_handler)
            ), self.assertRaises(StateError):
                _read_source_state(path)

    def test_ledger_read_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)

            def _handler(sql: Any, *args: Any, **kwargs: Any) -> Any:
                text = str(sql)
                if "integrity_check" in text:
                    return _ok_cursor()
                if "sqlite_master" in text:
                    return _FakeCursor([("schema_migrations",), ("deliveries",)])
                raise sqlite3.DatabaseError("boom")

            with patch.object(
                migrate_module, "_ro_connection", return_value=_mock_ro_connection(_handler)
            ), self.assertRaises(StateError):
                _read_source_state(path)


class TestIsCurrentAndComplete(unittest.TestCase):
    def test_current_db_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            self.assertTrue(_is_current_and_complete(path))

    def test_v2_is_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            self.assertFalse(_is_current_and_complete(path))

    def test_tampered_ledger_is_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("UPDATE schema_migrations SET checksum=? WHERE version=2", ("0" * 64,))
                connection.commit()
            finally:
                connection.close()
            self.assertFalse(_is_current_and_complete(path))

    def test_missing_table_is_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP TABLE source_results")
                connection.commit()
            finally:
                connection.close()
            self.assertFalse(_is_current_and_complete(path))

    def test_empty_ledger_is_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(SCHEMA_SQL)
                connection.execute("DELETE FROM schema_migrations")
                connection.commit()
            finally:
                connection.close()
            self.assertFalse(_is_current_and_complete(path))

    def test_open_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            with patch.object(
                migrate_module, "_ro_connection", side_effect=sqlite3.OperationalError("boom")
            ), self.assertRaises(StateError):
                _is_current_and_complete(path)

    def test_schema_probe_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)

            def _boom(sql: Any, *args: Any, **kwargs: Any) -> Any:
                raise sqlite3.DatabaseError("boom")

            with patch.object(
                migrate_module, "_ro_connection", return_value=_mock_ro_connection(_boom)
            ), self.assertRaises(StateError):
                _is_current_and_complete(path)

    def test_ledger_read_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)

            def _handler(sql: Any, *args: Any, **kwargs: Any) -> Any:
                text = str(sql)
                if "sqlite_master" in text:
                    return _FakeCursor([("schema_migrations",), ("deliveries",)])
                raise sqlite3.DatabaseError("boom")

            with patch.object(
                migrate_module, "_ro_connection", return_value=_mock_ro_connection(_handler)
            ), self.assertRaises(StateError):
                _is_current_and_complete(path)

    def test_column_probe_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            checksums = {version: migration_checksum(version) for version in (1, 2, 3)}
            tables = [
                "schema_migrations",
                "run_leases",
                "deliveries",
                "delivery_attempts",
                "delivery_items",
                "outbox_chunks",
                "article_history",
                "source_results",
                "delivery_resolutions",
            ]

            def _handler(sql: Any, *args: Any, **kwargs: Any) -> Any:
                text = str(sql)
                if "sqlite_master" in text:
                    return _FakeCursor([(name,) for name in tables])
                if "schema_migrations" in text:
                    return _FakeCursor([(str(version), checksums[version]) for version in (1, 2, 3)])
                raise sqlite3.DatabaseError("boom")

            with patch.object(
                migrate_module, "_ro_connection", return_value=_mock_ro_connection(_handler)
            ), self.assertRaises(StateError):
                _is_current_and_complete(path)

class TestCreateManifestFailures(unittest.TestCase):
    def test_missing_source_raises_without_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            with self.assertRaises(FileNotFoundError):
                create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(_pre_migrate_files(Path(d)), [])

    def test_repeated_create_uses_counter_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            first = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            second = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            self.assertNotEqual(first.backup, second.backup)
            self.assertTrue(second.backup.name.endswith("-1.bak"))
            verify_migration_manifest(path, first.manifest)
            verify_migration_manifest(path, second.manifest)
            second.backup.unlink()
            third = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            self.assertTrue(third.backup.name.endswith("-2.bak"))
            verify_migration_manifest(path, third.manifest)

    def test_origin_open_failure_raises_without_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            with (
                patch.object(migrate_module, "_read_source_state", return_value=("ok", 0)),
                patch.object(migrate_module, "_ro_connection", side_effect=sqlite3.OperationalError("boom")),
                self.assertRaises(StateError),
            ):
                create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_pre_migrate_files(Path(d)), [])

    def test_backup_operation_failure_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            bomb = _BackupBombConnection(migrate_module._ro_connection(path))
            with (
                patch.object(migrate_module, "_read_source_state", return_value=("ok", 0)),
                patch.object(migrate_module, "_ro_connection", return_value=bomb),
                self.assertRaises(StateError),
            ):
                create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            bomb._real.close()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_pre_migrate_files(Path(d)), [])

    def test_backup_verify_open_failure_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            real_ro = migrate_module._ro_connection
            first = real_ro(path)
            second = real_ro(path)
            with (
                patch.object(
                    migrate_module, "_ro_connection", side_effect=[first, second, sqlite3.OperationalError("boom")]
                ),
                self.assertRaises(StateError),
            ):
                create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            first.close()
            second.close()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_pre_migrate_files(Path(d)), [])

    def test_backup_integrity_probe_failure_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            real_ro = migrate_module._ro_connection
            probe_fail = _mock_ro_connection(_boom_execute)
            with (
                patch.object(
                    migrate_module, "_ro_connection", side_effect=[real_ro(path), real_ro(path), probe_fail]
                ),
                self.assertRaises(StateError),
            ):
                create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_pre_migrate_files(Path(d)), [])

    def test_backup_integrity_not_ok_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            real_ro = migrate_module._ro_connection
            corrupt = _mock_ro_connection(lambda sql, *args, **kwargs: _FakeCursor([("malformed",)]))
            with (
                patch.object(migrate_module, "_ro_connection", side_effect=[real_ro(path), real_ro(path), corrupt]),
                self.assertRaises(StateError),
            ):
                create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_pre_migrate_files(Path(d)), [])


def _boom_execute(sql: Any, *args: Any, **kwargs: Any) -> Any:
    raise sqlite3.DatabaseError("boom")


class TestVerifyManifestFailures(unittest.TestCase):
    def test_missing_manifest_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            with self.assertRaises(FileNotFoundError):
                verify_migration_manifest(path, Path(d) / "absent.manifest.json")

    def test_unparsable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            manifest = Path(d) / "broken.manifest.json"
            manifest.write_text("not json{{{", encoding="utf-8")
            with self.assertRaises(StateError):
                verify_migration_manifest(path, manifest)

    def test_non_object_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            manifest = Path(d) / "listed.manifest.json"
            manifest.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(StateError):
                verify_migration_manifest(path, manifest)

    def test_missing_field(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            payload = json.loads(artifact.manifest.read_text(encoding="utf-8"))
            del payload["backup_sha256"]
            artifact.manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(StateError):
                verify_migration_manifest(path, artifact.manifest)

    def test_manifest_integrity_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            payload = json.loads(artifact.manifest.read_text(encoding="utf-8"))
            payload["integrity"] = "corrupt"
            artifact.manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(StateError):
                verify_migration_manifest(path, artifact.manifest)

    def test_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            payload = json.loads(artifact.manifest.read_text(encoding="utf-8"))
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                verify_migration_manifest(path, payload)

    def test_mapping_manifest_success(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            payload = json.loads(artifact.manifest.read_text(encoding="utf-8"))
            data = verify_migration_manifest(path, payload)
            self.assertEqual(data["backup_id"], artifact.backup_id)

    def test_source_integrity_recheck_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            with (
                patch.object(migrate_module, "_read_source_state", return_value=("corrupt", 0)),
                self.assertRaises(StateError),
            ):
                verify_migration_manifest(path, artifact.manifest)

    def test_backup_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            artifact = create_migration_manifest(path, app_version="2.0.0", config_hash="abc123")
            with artifact.backup.open("ab") as handle:
                handle.write(b"\x00")
            with self.assertRaises(StateError):
                verify_migration_manifest(path, artifact.manifest)

class TestRunGuardedFailures(unittest.TestCase):
    def test_missing_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            with MaintenanceContext.acquire(path, owner="m1") as ctx:
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")

    def test_invalid_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            with (
                MaintenanceContext.acquire(path, owner="m1") as ctx, patch.object(
                    migrate_module, "verify_catalog", return_value=SimpleNamespace(ok=False, issues=("boom",))
                ),
                self.assertRaises(StateError),
            ):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)

    def test_non_legacy_tables_without_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE stray (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            before = path.read_bytes()
            with MaintenanceContext.acquire(path, owner="m1") as ctx, self.assertRaises(StateError):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)

    def test_gap_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(SCHEMA_SQL)
                connection.execute("DELETE FROM schema_migrations")
                for version in (1, 3):
                    connection.execute(
                        "INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (?,?,?,?)",
                        (version, migration_checksum(version), "2026-01-01T00:00:00+00:00", "1.0.0"),
                    )
                connection.commit()
            finally:
                connection.close()
            before = path.read_bytes()
            with MaintenanceContext.acquire(path, owner="m1") as ctx, self.assertRaises(StateError):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)

    def test_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("UPDATE schema_migrations SET checksum=? WHERE version=2", ("0" * 64,))
                connection.commit()
            finally:
                connection.close()
            before = path.read_bytes()
            with MaintenanceContext.acquire(path, owner="m1") as ctx, self.assertRaises(StateError):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)

    def test_newer_schema(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (?,?,?,?)",
                    (4, "4" * 64, "2026-01-01T00:00:00+00:00", "9.9.9"),
                )
                connection.commit()
            finally:
                connection.close()
            before = path.read_bytes()
            with MaintenanceContext.acquire(path, owner="m1") as ctx, self.assertRaises(StateError):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)

    def test_current_race_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            with (
                MaintenanceContext.acquire(path, owner="m1") as ctx,
                patch.object(migrate_module, "_is_current_and_complete", return_value=False),
            ):
                applied = run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(applied, 0)
            with StateStore(path) as store:
                self.assertEqual(store.schema_version, CURRENT_SCHEMA_VERSION)

    def test_empty_database_migrates_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            path.write_bytes(b"")
            with MaintenanceContext.acquire(path, owner="m1") as ctx:
                applied = run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(applied, CURRENT_SCHEMA_VERSION)
            with StateStore(path) as store:
                self.assertEqual(store.schema_version, CURRENT_SCHEMA_VERSION)

    def test_rollback_when_apply_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            fake = _writer_failing_connect(sqlite3.connect, fail_sql="CREATE TABLE")
            with (
                MaintenanceContext.acquire(path, owner="m1") as ctx, patch.object(migrate_module.sqlite3, "connect", new=fake),
                self.assertRaises(sqlite3.Error),
            ):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)
            tables = {row[0] for row in _fetch_all(path, "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("schema_migrations", tables)

    def test_rollback_when_pending_apply_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            before = path.read_bytes()
            fake = _writer_failing_connect(sqlite3.connect, fail_sql="target_snapshot")
            with (
                MaintenanceContext.acquire(path, owner="m1") as ctx, patch.object(migrate_module.sqlite3, "connect", new=fake),
                self.assertRaises(sqlite3.Error),
            ):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)
            versions = [row[0] for row in _fetch_all(path, "SELECT version FROM schema_migrations ORDER BY version")]
            self.assertEqual(versions, [1, 2])

    def test_pre_integrity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = path.read_bytes()
            fake = _writer_failing_connect(sqlite3.connect, fail_integrity_call=1)
            with (
                MaintenanceContext.acquire(path, owner="m1") as ctx, patch.object(migrate_module.sqlite3, "connect", new=fake),
                self.assertRaises(StateError),
            ):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            self.assertEqual(path.read_bytes(), before)

    def test_post_integrity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            fake = _writer_failing_connect(sqlite3.connect, fail_integrity_call=2)
            with (
                MaintenanceContext.acquire(path, owner="m1") as ctx, patch.object(migrate_module.sqlite3, "connect", new=fake),
                self.assertRaises(StateError),
            ):
                run_guarded_migrations(path, context=ctx, app_version="2.0.0", config_hash="abc123")
            with StateStore(path, readonly=True) as store:
                self.assertEqual(store.schema_version, CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
