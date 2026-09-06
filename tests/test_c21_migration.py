"""C2.1 immutable migration catalog RED tests (closure plan).

Covers: catalog immutability/verification, exact prior/intermediate/current
fixtures plus malformed/gap/duplicate/missing-object/mismatch/current+1,
runtime open refusing migration-required state without changing bytes,
catalog-runner migration under a test guard (idempotent no-op on repeat),
and the audited but disabled public migrate command (fail-closed with
maintenance_unavailable until C2.2).
"""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import tempfile
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
import unittest

from meco_news.app import main
from meco_news.config import load_config
from meco_news.inspection import inspect_state
from meco_news.migrations import (
    CURRENT_SCHEMA_VERSION,
    CatalogReport,
    MIGRATION_DESCRIPTIONS,
    MIGRATION_SQL,
    SCHEMA_SQL,
    MigrationGuard,
    MigrationNotPermitted,
    catalog_entries,
    ledger_contiguity_issue,
    migration_checksum,
    verify_catalog,
)
from meco_news.storage import (
    MigrationRequiredError,
    StateError,
    StateStore,
    _ledger_versions,
    run_catalog_migrations,
)


def _snapshot(path: Path) -> tuple[bytes, tuple[str, ...]]:
    with open(path, "rb") as handle:
        blob = handle.read()
    siblings = tuple(sorted(p.name for p in path.parent.iterdir()))
    return blob, siblings


def _make_legacy_v1(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(MIGRATION_SQL[1])
        connection.execute(
            "INSERT INTO sent_articles(fingerprint,title,url,source,topic,score,sent_at,delivery_date)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("fp1", "LPG terminal", "https://example.com/lpg", "Example", "lpg_energy", 10, "2026-01-01T00:00:00+00:00", "2026-01-01"),
        )
        connection.execute(
            "INSERT INTO runs(delivery_date,started_at,completed_at,status,item_count,error) VALUES (?,?,?,?,?,?)",
            ("2026-01-01", "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00", "completed", 1, ""),
        )
        connection.commit()
    finally:
        connection.close()


def _make_ledger(connection: sqlite3.Connection, versions: list[int], tamper: int | None = None) -> None:
    for version in versions:
        checksum = migration_checksum(version) if version != tamper else "0" * 64
        if version > CURRENT_SCHEMA_VERSION:
            checksum = "4" * 64
        connection.execute(
            "INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (?,?,?,?)",
            (version, checksum, "2026-01-01T00:00:00+00:00", "1.0.0"),
        )


def _make_n_minus_1(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_SQL)
        connection.execute("DELETE FROM schema_migrations")
        _make_ledger(connection, [1])
        connection.commit()
    finally:
        connection.close()


def _make_v2(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(MIGRATION_SQL[1])
        connection.executescript(MIGRATION_SQL[2])
        _make_ledger(connection, [1, 2])
        connection.commit()
    finally:
        connection.close()


def _make_current(path: Path) -> None:
    with StateStore(path):
        pass


def _make_gap(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_SQL)
        connection.execute("DELETE FROM schema_migrations")
        _make_ledger(connection, [1, 3])
        connection.commit()
    finally:
        connection.close()


def _make_plus1(path: Path) -> None:
    _make_current(path)
    connection = sqlite3.connect(path)
    try:
        _make_ledger(connection, [4])
        connection.commit()
    finally:
        connection.close()


class TestCatalogImmutability(unittest.TestCase):
    def test_prior_checksums_stable_when_future_migration_added(self) -> None:
        before = {v: migration_checksum(v) for v in (1, 2, 3)}
        extended_sql = dict(MIGRATION_SQL)
        extended_sql[4] = "ALTER TABLE runs ADD COLUMN note TEXT NOT NULL DEFAULT '';"
        extended_desc = dict(MIGRATION_DESCRIPTIONS)
        extended_desc[4] = "future maintenance note column"
        for version in (1, 2, 3):
            recomputed = sha256(f"{version}:{extended_desc[version]}:{extended_sql[version]}".encode()).hexdigest()
            self.assertEqual(recomputed, before[version])

    def test_verify_catalog_ok_on_shipped_catalog(self) -> None:
        report = verify_catalog()
        self.assertTrue(report.ok, f"shipped catalog must verify: {report.issues}")
        self.assertEqual(list(report.versions), [1, 2, 3])
        self.assertEqual(len(report.checksums), 3)

    def test_verify_catalog_detects_gap(self) -> None:
        entries = [e for e in catalog_entries() if e[0] != 2]
        report = verify_catalog(entries)
        self.assertFalse(report.ok)
        self.assertTrue(any("gap" in issue for issue in report.issues), f"expected gap issue: {report.issues}")

    def test_verify_catalog_detects_duplicate(self) -> None:
        entries = list(catalog_entries()) + [catalog_entries()[0]]
        report = verify_catalog(entries)
        self.assertFalse(report.ok)
        self.assertTrue(any("duplicate" in issue for issue in report.issues), f"expected duplicate issue: {report.issues}")

    def test_verify_catalog_detects_tampered_sql(self) -> None:
        entries = [(v, d, s + "-- tampered" if v == 2 else s) for v, d, s in catalog_entries()]
        report = verify_catalog(entries)
        self.assertFalse(report.ok)
        self.assertTrue(any("checksum" in issue for issue in report.issues), f"expected checksum issue: {report.issues}")

    def test_verify_catalog_detects_missing_description(self) -> None:
        entries = [(v, ("" if v == 1 else d), s) for v, d, s in catalog_entries()]
        report = verify_catalog(entries)
        self.assertFalse(report.ok)
        self.assertTrue(any("description" in issue for issue in report.issues), f"expected description issue: {report.issues}")

    def test_verify_catalog_detects_missing_required_object(self) -> None:
        entries = [(v, d, (s.replace("runs", "renamed") if v == 1 else s)) for v, d, s in catalog_entries()]
        report = verify_catalog(entries)
        self.assertFalse(report.ok)
        self.assertTrue(any("required object" in issue for issue in report.issues), f"expected object issue: {report.issues}")

    def test_verify_catalog_detects_version_beyond_current(self) -> None:
        entries = list(catalog_entries()) + [(CURRENT_SCHEMA_VERSION + 1, "future work", "SELECT 1;")]
        report = verify_catalog(entries)
        self.assertFalse(report.ok)
        self.assertTrue(any("unsupported" in issue for issue in report.issues), f"expected unsupported issue: {report.issues}")


class TestLedgerContiguity(unittest.TestCase):
    def test_contiguous_ledger_has_no_issue(self) -> None:
        self.assertIsNone(ledger_contiguity_issue([1, 2, 3]))

    def test_gap_ledger_reports_gap(self) -> None:
        issue = ledger_contiguity_issue([1, 3])
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertIn("gap", issue)

    def test_duplicate_ledger_reports_duplicate(self) -> None:
        issue = ledger_contiguity_issue([1, 2, 2])
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertIn("duplicate", issue)

    def test_empty_ledger_reports_empty(self) -> None:
        issue = ledger_contiguity_issue([])
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertIn("empty", issue)


class TestFixtureClassification(unittest.TestCase):
    def test_legacy_v1_is_migration_required(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            self.assertEqual(inspect_state(path).classification, "migration_required")

    def test_n_minus_1_is_migration_required(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_n_minus_1(path)
            result = inspect_state(path)
            self.assertEqual(result.classification, "migration_required")
            self.assertEqual(result.schema_version, 1)

    def test_v2_exact_is_migration_required(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            result = inspect_state(path)
            self.assertEqual(result.classification, "migration_required")
            self.assertEqual(result.schema_version, 2)

    def test_current_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            result = inspect_state(path)
            self.assertEqual(result.classification, "compatible")
            self.assertEqual(result.schema_version, CURRENT_SCHEMA_VERSION)

    def test_tampered_checksum_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(SCHEMA_SQL)
                connection.execute("DELETE FROM schema_migrations")
                _make_ledger(connection, [1, 2, 3], tamper=2)
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(inspect_state(path).classification, "malformed")

    def test_gap_ledger_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_gap(path)
            self.assertEqual(inspect_state(path).classification, "malformed")

    def test_missing_object_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP TABLE run_leases")
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(inspect_state(path).classification, "malformed")

    def test_current_plus_1_is_newer_incompatible(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_plus1(path)
            result = inspect_state(path)
            self.assertEqual(result.classification, "newer_incompatible")
            self.assertEqual(result.schema_version, CURRENT_SCHEMA_VERSION + 1)

    def test_empty_file_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            connection = sqlite3.connect(path)
            connection.commit()
            connection.close()
            self.assertEqual(inspect_state(path).classification, "malformed")


class TestOpenRefusesMigration(unittest.TestCase):
    def test_open_legacy_v1_raises_and_changes_no_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = _snapshot(path)
            with self.assertRaises(MigrationRequiredError):
                StateStore(path)
            self.assertEqual(_snapshot(path), before)

    def test_open_n_minus_1_raises_and_changes_no_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_n_minus_1(path)
            before = _snapshot(path)
            with self.assertRaises(MigrationRequiredError):
                StateStore(path)
            self.assertEqual(_snapshot(path), before)

    def test_open_v2_raises_and_changes_no_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            before = _snapshot(path)
            with self.assertRaises(MigrationRequiredError):
                StateStore(path)
            self.assertEqual(_snapshot(path), before)

    def test_open_v3_missing_snapshot_column_raises_and_changes_no_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(MIGRATION_SQL[1])
                connection.executescript(MIGRATION_SQL[2])
                _make_ledger(connection, [1, 2, 3])
                connection.commit()
            finally:
                connection.close()
            before = _snapshot(path)
            with self.assertRaises(MigrationRequiredError):
                StateStore(path)
            self.assertEqual(_snapshot(path), before)

    def test_open_gap_ledger_raises_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_gap(path)
            with self.assertRaises(StateError):
                StateStore(path)

    def test_open_fresh_path_still_creates_current(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            with StateStore(path) as store:
                self.assertGreaterEqual(store.schema_version, CURRENT_SCHEMA_VERSION)
            self.assertEqual(inspect_state(path).classification, "compatible")

    def test_open_current_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            with StateStore(path):
                pass
            self.assertEqual(inspect_state(path).classification, "compatible")

    def test_migration_required_is_a_state_error(self) -> None:
        self.assertTrue(issubclass(MigrationRequiredError, StateError))


class TestCatalogRunner(unittest.TestCase):
    def test_runner_requires_an_explicit_guard(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            connection = sqlite3.connect(path)
            try:
                with self.assertRaises(MigrationNotPermitted):
                    run_catalog_migrations(connection, guard=None, app_version="2.0.0")  # type: ignore[arg-type]
            finally:
                connection.close()

    def test_runner_rejects_wrong_guard_scope(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            connection = sqlite3.connect(path)
            try:
                with self.assertRaises(MigrationNotPermitted):
                    run_catalog_migrations(connection, guard=MigrationGuard(scope="unknown"), app_version="2.0.0")
            finally:
                connection.close()

    def test_runner_applies_v2_to_v3(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            connection = sqlite3.connect(path)
            try:
                applied = run_catalog_migrations(connection, guard=MigrationGuard.for_tests(), app_version="2.0.0")
            finally:
                connection.close()
            self.assertEqual(applied, 1)
            result = inspect_state(path)
            self.assertEqual(result.classification, "compatible")
            with StateStore(path):
                pass

    def test_runner_adopts_legacy_v1_and_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            connection = sqlite3.connect(path)
            try:
                applied = run_catalog_migrations(connection, guard=MigrationGuard.for_tests(), app_version="2.0.0")
            finally:
                connection.close()
            self.assertEqual(applied, 3)
            self.assertEqual(inspect_state(path).classification, "compatible")
            check = sqlite3.connect(path)
            try:
                articles = check.execute("SELECT COUNT(*) FROM sent_articles").fetchone()[0]
                deliveries = check.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
            finally:
                check.close()
            self.assertEqual(articles, 1)
            self.assertEqual(deliveries, 1)

    def test_runner_repeated_is_noop_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            connection = sqlite3.connect(path)
            try:
                first = run_catalog_migrations(connection, guard=MigrationGuard.for_tests(), app_version="2.0.0")
                self.assertEqual(first, 1)
            finally:
                connection.close()
            before = _snapshot(path)
            connection = sqlite3.connect(path)
            try:
                second = run_catalog_migrations(connection, guard=MigrationGuard.for_tests(), app_version="2.0.0")
            finally:
                connection.close()
            self.assertEqual(second, 0)
            self.assertEqual(_snapshot(path), before)


class TestMigrateCommandFailClosed(unittest.TestCase):
    def _run_main(self, argv: list[str], state_path: Path) -> tuple[int | str, str]:
        err = io.StringIO()
        with patch.dict(
            os.environ,
            {"STATE_DB": str(state_path), "TELEGRAM_BOT_TOKEN": "123456:real-token-value-for-test", "TELEGRAM_CHAT_ID": "12345"},
            clear=False,
        ), contextlib.redirect_stderr(err):
            try:
                code: int | str = main(argv)
            except SystemExit as exit_raised:
                code = exit_raised.code if exit_raised.code is not None else 0
        return code, err.getvalue()

    def test_migrate_requires_to_version(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            code, err = self._run_main(["--migrate"], Path(d) / "state.db")
            self.assertEqual(code, 2)
            self.assertIn("--migrate requires --to-version", err)

    def test_to_version_without_migrate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            code, err = self._run_main(["--to-version", "3"], Path(d) / "state.db")
            self.assertEqual(code, 2)
            self.assertIn("--to-version requires --migrate", err)

    def test_to_version_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            code, err = self._run_main(["--migrate", "--to-version", "0"], Path(d) / "state.db")
            self.assertEqual(code, 2)
            self.assertIn("--to-version must be positive", err)

    def test_migrate_is_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            code, err = self._run_main(["--migrate", "--to-version", "3", "--preflight"], Path(d) / "state.db")
            self.assertEqual(code, 2)
            self.assertIn("mutually exclusive", err)

    def test_migrate_rejects_delivery_modifiers(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            code, err = self._run_main(["--migrate", "--to-version", "3", "--daemon"], Path(d) / "state.db")
            self.assertEqual(code, 2)
            self.assertIn("cannot be combined with delivery modifiers", err)

    def test_migrate_fails_closed_with_maintenance_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            before = _snapshot(path)
            code, err = self._run_main(["--migrate", "--to-version", "3"], path)
            self.assertEqual(code, 1)
            self.assertIn("maintenance_unavailable", err)
            self.assertEqual(_snapshot(path), before)


class TestDeliveryRefusesMigration(unittest.TestCase):
    def test_delivery_on_n_minus_1_fails_closed_without_writes(self) -> None:
        config = load_config("config/watchlist.json")
        del config
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_n_minus_1(path)
            before = _snapshot(path)
            err = io.StringIO()
            with patch.dict(
                os.environ,
                {"STATE_DB": str(path), "TELEGRAM_BOT_TOKEN": "123456:real-token-value-for-test", "TELEGRAM_CHAT_ID": "12345"},
                clear=False,
            ), contextlib.redirect_stderr(err):
                code = main([])
            self.assertNotEqual(code, 0, "delivery on migration-required state must fail closed")
            self.assertEqual(_snapshot(path), before)




class _OneRow:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row

    def fetchall(self) -> list[object]:
        return [self._row]

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter([self._row])


class _ScriptedIntegrityConnection:
    """Wrap a real connection; fail the given 1-based integrity_check calls."""

    def __init__(self, wrapped: sqlite3.Connection, failures: set[int]) -> None:
        self._wrapped = wrapped
        self._failures = failures
        self._calls = 0

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> object:
        if "integrity_check" in str(sql):
            self._calls += 1
            if self._calls in self._failures:
                return _OneRow(("*** in database main ***",))
        return self._wrapped.execute(sql, parameters)

    def commit(self) -> None:
        self._wrapped.commit()

    def rollback(self) -> None:
        self._wrapped.rollback()

    def executescript(self, sql_script: str) -> object:
        return self._wrapped.executescript(sql_script)

    def close(self) -> None:
        self._wrapped.close()


def _make_failed_legacy(path: Path, *, with_run: bool = True) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(MIGRATION_SQL[1])
        connection.execute(
            "INSERT INTO sent_articles(fingerprint,title,url,source,topic,score,sent_at,delivery_date)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("fp9", "Old story", "https://example.com/old", "Example", "lpg_energy", 9, "2026-01-01T00:00:00+00:00", "2026-01-01"),
        )
        if with_run:
            connection.execute(
                "INSERT INTO runs(delivery_date,started_at,completed_at,status,item_count,error) VALUES (?,?,?,?,?,?)",
                ("2026-01-01", "2026-01-01T00:00:00+00:00", None, "failed", 1, "boom"),
            )
        connection.commit()
    finally:
        connection.close()


def _make_junk(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE junk(id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()


def _make_broken_legacy(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sent_articles(fingerprint TEXT PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE runs(delivery_date TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,"
            " status TEXT NOT NULL, item_count INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')"
        )
        connection.commit()
    finally:
        connection.close()


def _make_empty_ledger(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_SQL)
        connection.execute("DELETE FROM schema_migrations")
        connection.commit()
    finally:
        connection.close()


class TestLedgerVersionParsing(unittest.TestCase):
    def test_parses_integer_versions(self) -> None:
        self.assertEqual(_ledger_versions([(1, "a"), (2, "b")]), [1, 2])

    def test_rejects_non_integer_version(self) -> None:
        with self.assertRaises(StateError):
            _ledger_versions([("1a", "bad")])


class TestCatalogEmptySql(unittest.TestCase):
    def test_verify_catalog_detects_empty_sql(self) -> None:
        entries = [(v, d, ("" if v == 1 else s)) for v, d, s in catalog_entries()]
        report = verify_catalog(entries)
        self.assertFalse(report.ok)
        self.assertTrue(any("no canonical SQL" in issue for issue in report.issues), f"expected sql issue: {report.issues}")


class TestCatalogRunnerNegativePaths(unittest.TestCase):
    def _run(self, path: Path, guard=None, failures=None):  # type: ignore[no-untyped-def]
        connection = sqlite3.connect(path)
        try:
            wrapped = _ScriptedIntegrityConnection(connection, set(failures or ())) if failures else connection
            return run_catalog_migrations(wrapped, guard=MigrationGuard.for_tests() if guard is None else guard, app_version="2.0.0")
        finally:
            connection.close()

    def test_runner_rejects_gap_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_gap(path)
            with self.assertRaises(StateError):
                self._run(path)

    def test_runner_rejects_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(SCHEMA_SQL)
                connection.execute("DELETE FROM schema_migrations")
                _make_ledger(connection, [1, 2, 3], tamper=2)
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(StateError):
                self._run(path)

    def test_runner_rejects_newer_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_plus1(path)
            with self.assertRaises(StateError):
                self._run(path)

    def test_runner_rejects_tables_without_ledger_or_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_junk(path)
            with self.assertRaises(StateError):
                self._run(path)

    def test_runner_rolls_back_broken_legacy_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_broken_legacy(path)
            before = _snapshot(path)
            with self.assertRaises(sqlite3.Error):
                self._run(path)
            self.assertEqual(_snapshot(path), before)

    def test_runner_rejects_invalid_catalog(self) -> None:
        bad = CatalogReport(False, (), (), ("gap: missing migration version(s): 2",))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            connection = sqlite3.connect(path)
            try:
                with patch("meco_news.storage.verify_catalog", return_value=bad), self.assertRaises(StateError):
                    run_catalog_migrations(connection, guard=MigrationGuard.for_tests(), app_version="2.0.0")
            finally:
                connection.close()

    def test_runner_rejects_failed_pre_migration_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            with self.assertRaises(StateError):
                self._run(path, failures={1})

    def test_runner_rejects_failed_post_adoption_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_legacy_v1(path)
            with self.assertRaises(StateError):
                self._run(path, failures={2})

    def test_runner_rejects_failed_post_pending_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            with self.assertRaises(StateError):
                self._run(path, failures={2})

    def test_runner_rolls_back_failed_pending_migration(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_v2(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("ALTER TABLE deliveries ADD COLUMN target_snapshot TEXT NOT NULL DEFAULT ''")
                connection.commit()
            finally:
                connection.close()
            before = _snapshot(path)
            with self.assertRaises(sqlite3.Error):
                self._run(path)
            self.assertEqual(_snapshot(path), before)

    def test_runner_creates_fresh_memory_database(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            applied = run_catalog_migrations(connection, guard=MigrationGuard.for_tests(), app_version="2.0.0")
        finally:
            connection.close()
        self.assertEqual(applied, CURRENT_SCHEMA_VERSION)

    def test_runner_adopts_failed_legacy_run_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_failed_legacy(path)
            connection = sqlite3.connect(path)
            try:
                applied = run_catalog_migrations(connection, guard=MigrationGuard.for_tests(), app_version="2.0.0")
            finally:
                connection.close()
            self.assertEqual(applied, 3)
            check = sqlite3.connect(path)
            try:
                state = check.execute("SELECT state FROM deliveries").fetchone()[0]
            finally:
                check.close()
            self.assertEqual(state, "failed_terminal")

    def test_runner_adopts_articles_without_any_run(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_failed_legacy(path, with_run=False)
            connection = sqlite3.connect(path)
            try:
                applied = run_catalog_migrations(connection, guard=MigrationGuard.for_tests(), app_version="2.0.0")
            finally:
                connection.close()
            self.assertEqual(applied, 3)
            self.assertEqual(inspect_state(path).classification, "compatible")


class TestOpenNegativePaths(unittest.TestCase):
    def test_open_empty_ledger_raises_migration_required(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_empty_ledger(path)
            with self.assertRaises(MigrationRequiredError):
                StateStore(path)

    def test_open_missing_table_raises_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP TABLE run_leases")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(StateError):
                StateStore(path)

    def test_open_fails_closed_on_failed_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            _make_current(path)
            with (
                patch.object(StateStore, "integrity_check", return_value="*** in database main ***"),
                self.assertRaises(StateError),
            ):
                StateStore(path)

    def test_open_fresh_create_rolls_back_apply_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            with patch("meco_news.storage._apply_schema_to", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
                StateStore(path)

    def test_open_fresh_create_fails_closed_on_post_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            with (
                patch.object(StateStore, "integrity_check", return_value="bad"),
                self.assertRaises(StateError),
            ):
                StateStore(path)


if __name__ == "__main__":
    unittest.main()