"""C1.2 read-only inspector RED tests (closure plan C1.2, F-003 follow-up).

Covers the classified inspection contract from ADR-C04 interim:
missing / migration_required / compatible / newer_incompatible / malformed /
corrupt, the supported Python range, safe WAL-capability probing, the
exclusive maintenance guard with the non-public maintenance_verify routine,
deterministic preflight exit precedence, and byte-level no-mutation proofs.

Written RED-first: meco_news.inspection and meco_news.maintenance do not
exist yet, so every test here must fail before the C1.2 implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch
import unittest

from meco_news.config import load_config
from meco_news.inspection import (
    SUPPORTED_PYTHON_RANGE,
    check_python_version,
    inspect_state,
    probe_wal_capability,
    python_in_range,
)
from meco_news.maintenance import (
    MaintenanceBusy,
    MaintenanceContext,
    MaintenanceError,
    _maintenance_verify,
    is_maintenance_held,
)
from meco_news.migrations import CURRENT_SCHEMA_VERSION, migration_checksum
from meco_news.preflight import (
    PREFLIGHT_LEASE,
    PREFLIGHT_MAINTENANCE,
    PREFLIGHT_RUNTIME,
    PREFLIGHT_SCHEMA,
    PREFLIGHT_SECRET,
    PREFLIGHT_STATE,
    run_preflight,
)
from meco_news.storage import StateStore

VALID_SECRETS = {"TELEGRAM_BOT_TOKEN": "123456:real-token-value-for-test", "TELEGRAM_CHAT_ID": "12345"}


def _fresh_db(path: Path) -> Path:
    with StateStore(path) as store:
        store.create_delivery("2026-09-06", config_hash="h")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    return path


def _snapshot(path: Path) -> tuple[str, list[str]]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    names = sorted(p.name for p in path.parent.iterdir())
    return digest, names


class TestInspectClassification(unittest.TestCase):
    def test_missing_path_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            result = inspect_state(Path(d) / "absent.db")
            self.assertEqual(result.classification, "missing")
            self.assertEqual(result.schema_version, 0)

    def test_current_schema_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            result = inspect_state(path)
            self.assertEqual(result.classification, "compatible")
            self.assertEqual(result.schema_version, CURRENT_SCHEMA_VERSION)
            self.assertEqual(result.integrity, "ok")

    def test_n_minus_1_is_migration_required(self) -> None:
        from meco_news.migrations import SCHEMA_SQL

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            con = sqlite3.connect(path)
            con.executescript(SCHEMA_SQL)
            con.execute("DELETE FROM schema_migrations")
            con.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (1, ?, '2026-01-01T00:00:00+00:00', '1.0.0')",
                (migration_checksum(1),),
            )
            con.commit()
            con.close()
            result = inspect_state(path)
            self.assertEqual(result.classification, "migration_required")
            self.assertEqual(result.schema_version, 1)

    def test_n_plus_1_is_newer_incompatible(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            con = sqlite3.connect(path)
            con.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (?, ?, '2026-09-06T00:00:00+00:00', '9.9.9')",
                (CURRENT_SCHEMA_VERSION + 1, migration_checksum(CURRENT_SCHEMA_VERSION + 1)),
            )
            con.commit()
            con.close()
            result = inspect_state(path)
            self.assertEqual(result.classification, "newer_incompatible")
            self.assertEqual(result.schema_version, CURRENT_SCHEMA_VERSION + 1)

    def test_empty_file_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            path.write_bytes(b"")
            self.assertEqual(inspect_state(path).classification, "malformed")

    def test_non_database_bytes_are_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            path.write_bytes(b"this is definitely not a sqlite database" * 64)
            self.assertEqual(inspect_state(path).classification, "corrupt")

    def test_header_valid_body_garbage_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            raw = bytearray(path.read_bytes())
            self.assertGreater(len(raw), 8192)
            raw[4096:4224] = b"\x55" * 128
            path.write_bytes(bytes(raw))
            self.assertEqual(inspect_state(path).classification, "corrupt")

    def test_ledger_without_schema_migrations_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE junk(id INTEGER PRIMARY KEY)")
            con.commit()
            con.close()
            self.assertEqual(inspect_state(path).classification, "malformed")

    def test_legacy_v1_tables_without_ledger_are_migration_required(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE sent_articles(fingerprint TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE runs(delivery_date TEXT PRIMARY KEY)")
            con.commit()
            con.close()
            result = inspect_state(path)
            self.assertEqual(result.classification, "migration_required")
            self.assertEqual(result.schema_version, 0)

    def test_checksum_mismatch_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            con = sqlite3.connect(path)
            con.execute("UPDATE schema_migrations SET checksum=? WHERE version=?", ("f" * 64, CURRENT_SCHEMA_VERSION))
            con.commit()
            con.close()
            self.assertEqual(inspect_state(path).classification, "malformed")

    def test_missing_table_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            con = sqlite3.connect(path)
            con.execute("DROP TABLE source_results")
            con.commit()
            con.close()
            result = inspect_state(path)
            self.assertEqual(result.classification, "malformed")
            self.assertIn("source_results", result.detail)

    def test_missing_index_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            con = sqlite3.connect(path)
            con.execute("DROP INDEX idx_chunks_due")
            con.commit()
            con.close()
            result = inspect_state(path)
            self.assertEqual(result.classification, "malformed")
            self.assertIn("idx_chunks_due", result.detail)


class TestInspectReadOnly(unittest.TestCase):
    def test_inspection_mutates_no_bytes_or_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            before = _snapshot(path)
            inspect_state(path)
            inspect_state(path)
            self.assertEqual(_snapshot(path), before)

    def test_preflight_mutates_no_bytes_or_sidecars(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            before = _snapshot(path)
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                run_preflight(config, state_path=path)
                run_preflight(config, state_path=path)
            self.assertEqual(_snapshot(path), before)

    def test_preflight_on_missing_db_creates_nothing(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "absent.db"
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                run_preflight(config, state_path=path)
            self.assertEqual([p.name for p in Path(d).iterdir()], [])


class TestPythonRange(unittest.TestCase):
    def test_supported_range_constant_matches_pyproject(self) -> None:
        self.assertEqual(SUPPORTED_PYTHON_RANGE, ">=3.12,<3.15")

    def test_running_interpreter_is_supported(self) -> None:
        ok, _ = check_python_version()
        self.assertTrue(ok)
        self.assertTrue(python_in_range(sys.version_info))

    def test_boundaries(self) -> None:
        self.assertTrue(python_in_range((3, 12, 0)))
        self.assertTrue(python_in_range((3, 14, 6)))
        self.assertFalse(python_in_range((3, 11, 9)))
        self.assertFalse(python_in_range((3, 15, 0)))
        self.assertFalse(python_in_range((3, 16, 0)))

    def test_check_reports_version_string(self) -> None:
        ok, version = check_python_version((3, 11, 0))
        self.assertFalse(ok)
        self.assertIn("3.11", version)

    def test_preflight_reports_runtime_check(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                _, report = run_preflight(config, state_path=path)
            self.assertTrue(report["checks"]["runtime"]["ok"])

    def test_unsupported_python_fails_preflight(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with (
                patch.dict(os.environ, VALID_SECRETS, clear=False),
                patch("meco_news.inspection.check_python_version", return_value=(False, "3.11.9")),
            ):
                code, report = run_preflight(config, state_path=path)
            self.assertFalse(report.get("ready"))
            self.assertFalse(report["checks"]["runtime"]["ok"])
            self.assertEqual(code, PREFLIGHT_RUNTIME)


class TestWalProbe(unittest.TestCase):
    def test_writable_directory_is_wal_capable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            probe = probe_wal_capability(Path(d))
            self.assertTrue(probe.ok)
            self.assertEqual(probe.journal_mode, "wal")

    def test_probe_leaves_directory_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            before = sorted(p.name for p in Path(d).iterdir())
            probe_wal_capability(Path(d))
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), before)

    def test_probe_does_not_touch_live_database(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            before = _snapshot(path)
            probe_wal_capability(Path(d))
            self.assertEqual(_snapshot(path), before)

    def test_missing_directory_is_not_capable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            probe = probe_wal_capability(Path(d) / "absent-dir")
            self.assertFalse(probe.ok)

    def test_preflight_reports_wal_capability(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                _, report = run_preflight(config, state_path=path)
            self.assertTrue(report["checks"]["state_filesystem"]["wal"]["ok"])


class TestMaintenanceGuard(unittest.TestCase):
    def test_acquire_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            held, _ = is_maintenance_held(path)
            self.assertFalse(held)
            with MaintenanceContext.acquire(path, owner="op-1") as ctx:
                self.assertTrue(ctx.live)
                held, info = is_maintenance_held(path)
                self.assertTrue(held)
                self.assertEqual(info["owner"], "op-1")
            held, _ = is_maintenance_held(path)
            self.assertFalse(held)

    def test_double_acquire_raises_busy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="op-1"), self.assertRaises(MaintenanceBusy):
                MaintenanceContext.acquire(path, owner="op-2")

    def test_stale_marker_is_not_held_and_can_be_taken_over(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="crashed", ttl_seconds=-1):
                pass
            held, _ = is_maintenance_held(path)
            self.assertFalse(held)
            with MaintenanceContext.acquire(path, owner="op-2") as ctx:
                self.assertTrue(ctx.live)

    def test_preflight_during_maintenance_is_non_ready(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            before = _snapshot(path)
            with patch.dict(os.environ, VALID_SECRETS, clear=False), MaintenanceContext.acquire(path, owner="restore-job"):
                code, report = run_preflight(config, state_path=path)
            self.assertFalse(report.get("ready"))
            self.assertEqual(code, PREFLIGHT_MAINTENANCE)
            self.assertFalse(report["checks"]["maintenance"]["ok"])
            self.assertEqual(report["checks"]["maintenance"]["reason"], "maintenance_in_progress")
            self.assertEqual(_snapshot(path), before)

    def test_stale_marker_does_not_block_preflight(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="crashed", ttl_seconds=-1):
                pass
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                code, report = run_preflight(config, state_path=path)
            self.assertNotEqual(code, PREFLIGHT_MAINTENANCE)
            self.assertTrue(report["checks"]["maintenance"]["ok"])


class TestMaintenanceVerify(unittest.TestCase):
    def test_live_context_verifies_without_ready(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="restore-job") as ctx:
                result = _maintenance_verify(path, ctx)
            self.assertTrue(result["verified_for_maintenance"])
            self.assertNotIn("ready", result)
            self.assertEqual(result["classification"], "compatible")

    def test_missing_context_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="restore-job") as ctx:
                pass
            with self.assertRaises(MaintenanceError):
                _maintenance_verify(path, ctx)

    def test_wrong_path_context_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            first = _fresh_db(Path(d) / "first.db")
            second = _fresh_db(Path(d) / "second.db")
            with MaintenanceContext.acquire(first, owner="restore-job") as ctx, self.assertRaises(MaintenanceError):
                _maintenance_verify(second, ctx)

    def test_wrong_scope_context_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="restore-job", scope="restore") as ctx, self.assertRaises(MaintenanceError):
                _maintenance_verify(path, ctx, scope="maintenance")

    def test_verify_never_emits_ready_true(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="restore-job") as ctx:
                result = _maintenance_verify(path, ctx)
            self.assertFalse(result.get("ready", False))


class TestPreflightSchemaExits(unittest.TestCase):
    def test_n_plus_1_reports_schema_exit(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            con = sqlite3.connect(path)
            con.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (?, ?, '2026-09-06T00:00:00+00:00', '9.9.9')",
                (CURRENT_SCHEMA_VERSION + 1, migration_checksum(CURRENT_SCHEMA_VERSION + 1)),
            )
            con.commit()
            con.close()
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                code, report = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_SCHEMA)
            self.assertFalse(report.get("ready"))
            self.assertEqual(report["checks"]["database"]["classification"], "newer_incompatible")

    def test_malformed_db_reports_schema_exit(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            path.write_bytes(b"")
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                code, report = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_SCHEMA)
            self.assertFalse(report.get("ready"))
            self.assertEqual(report["checks"]["database"]["classification"], "malformed")

    def test_corrupt_db_reports_schema_exit(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            path.write_bytes(b"garbage bytes, not sqlite" * 64)
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                code, report = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_SCHEMA)
            self.assertFalse(report.get("ready"))
            self.assertEqual(report["checks"]["database"]["classification"], "corrupt")

    def test_compatible_db_reports_classification(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                code, report = run_preflight(config, state_path=path)
            self.assertEqual(code, 0)
            self.assertTrue(report.get("ready"))
            self.assertEqual(report["checks"]["database"]["classification"], "compatible")


class TestExitPrecedence(unittest.TestCase):
    def _leased_db(self, d: str) -> Path:
        path = _fresh_db(Path(d) / "state.db")
        with StateStore(path) as store:
            store.acquire_lease("delivery", "owner1", 180)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        return path

    def test_state_beats_schema(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "absent-dir" / "state.db"
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                code, _ = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_STATE)

    def test_maintenance_beats_secret(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as td:
            path = self._leased_db(td)
            with (
                MaintenanceContext.acquire(path, owner="restore-job"),
                patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False),
            ):
                code, report = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_MAINTENANCE)
            self.assertFalse(report.get("ready"))

    def test_schema_beats_lease(self) -> None:
        from meco_news.migrations import SCHEMA_SQL

        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.db"
            con = sqlite3.connect(path)
            con.executescript(SCHEMA_SQL)
            con.execute("DELETE FROM schema_migrations")
            con.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (1, ?, '2026-01-01T00:00:00+00:00', '1.0.0')",
                (migration_checksum(1),),
            )
            con.commit()
            con.close()
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                code, _ = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_SCHEMA)

    def test_lease_beats_secret(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as td:
            path = self._leased_db(td)
            with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
                code, _ = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_LEASE)

    def test_secret_failure_is_exit_3(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as td:
            path = _fresh_db(Path(td) / "state.db")
            with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
                code, report = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_SECRET)
            self.assertFalse(report.get("ready"))

    def test_runtime_beats_everything(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "absent-dir" / "state.db"
            with (
                patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False),
                patch("meco_news.inspection.check_python_version", return_value=(False, "3.11.9")),
            ):
                code, _ = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_RUNTIME)


class TestReadyConjunction(unittest.TestCase):
    def test_each_false_check_forces_not_ready(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as td:
            good = _fresh_db(Path(td) / "good.db")
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                _, report = run_preflight(config, state_path=good)
                self.assertTrue(report.get("ready"))
                for name, check in report["checks"].items():
                    if name in {"sources", "telegram"}:
                        continue
                    if isinstance(check, dict):
                        self.assertTrue(check.get("ok"), f"fixture check {name} must start ok")
            bad_tz = replace(config, timezone="Nope/Zone")
            with patch.dict(os.environ, VALID_SECRETS, clear=False):
                _, report = run_preflight(bad_tz, state_path=good)
            self.assertFalse(report.get("ready"))


class StubStore:
    """Divergent read-only store stand-in for preflight race/swap windows."""

    def __init__(self, *, integrity: str = "ok", version: int = CURRENT_SCHEMA_VERSION, error: Exception | None = None) -> None:
        self._integrity = integrity
        self._version = version
        self._error = error

    def __enter__(self) -> StubStore:
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def integrity_check(self) -> str:
        return self._integrity

    @property
    def schema_version(self) -> int:
        return self._version

    def status_snapshot(self) -> dict[str, object]:
        return {}


class TestInspectEdgeBranches(unittest.TestCase):
    def test_directory_path_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            result = inspect_state(Path(d))
            self.assertEqual(result.classification, "corrupt")
            self.assertEqual(result.integrity, "open_failed")

    def test_returned_integrity_errors_are_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            base = path.read_bytes()
            needle = b"2026-09-06"
            last = base.rfind(needle)
            self.assertGreater(last, 0)
            raw = bytearray(base)
            raw[last + 3] ^= 8
            path.write_bytes(bytes(raw))
            result = inspect_state(path)
            self.assertEqual(result.classification, "corrupt")
            self.assertIn("missing from index", result.integrity)

    def test_non_integer_ledger_version_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            con = sqlite3.connect(path)
            con.execute("DROP TABLE schema_migrations")
            con.execute("CREATE TABLE schema_migrations(version TEXT, checksum TEXT, applied_at TEXT, app_version TEXT)")
            con.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES ('x', 'y', '2026-09-06T00:00:00+00:00', 't')"
            )
            con.commit()
            con.close()
            self.assertEqual(inspect_state(path).classification, "malformed")

    def test_mkstem_failure_is_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch("meco_news.inspection.tempfile.mkstemp", side_effect=OSError("denied")):
                probe = probe_wal_capability(Path(d))
            self.assertFalse(probe.ok)
            self.assertEqual(probe.reason, "state directory is not writable")

    def test_non_wal_mode_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            fake = MagicMock()
            fake.execute.return_value.fetchone.return_value = ("delete",)
            with patch("meco_news.inspection.sqlite3.connect", return_value=fake):
                probe = probe_wal_capability(Path(d))
            self.assertFalse(probe.ok)
            self.assertEqual(probe.journal_mode, "delete")

    def test_probe_connect_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch("meco_news.inspection.sqlite3.connect", side_effect=sqlite3.Error("boom")):
                probe = probe_wal_capability(Path(d))
            self.assertFalse(probe.ok)
            self.assertEqual(probe.reason, "WAL probe database failed")

    def test_probe_cleanup_failures_are_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch("pathlib.Path.unlink", side_effect=OSError("denied")):
                probe = probe_wal_capability(Path(d))
            self.assertTrue(probe.ok)


class TestMaintenanceEdgeBranches(unittest.TestCase):
    def test_stale_guard_while_held_reports_not_held(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="crashed", ttl_seconds=-1) as ctx:
                held, info = is_maintenance_held(path)
                self.assertFalse(held)
                self.assertTrue(info.get("stale_marker"))
                self.assertFalse(ctx.live)

    def test_naive_timestamp_marker_is_evaluated(self) -> None:
        from datetime import datetime, UTC

        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            marker = Path(str(path) + ".maintenance.json")
            payload = {
                "owner": "op-naive",
                "scope": "maintenance",
                "pid": 1,
                "token": "tok",
                "acquired_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                "ttl_seconds": 3600,
            }
            marker.write_text(json.dumps(payload), encoding="utf-8")
            held, info = is_maintenance_held(path)
            self.assertTrue(held)
            self.assertEqual(info["owner"], "op-naive")

    def test_garbage_marker_is_not_held(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            marker = Path(str(path) + ".maintenance.json")
            marker.write_text("{not json", encoding="utf-8")
            held, _ = is_maintenance_held(path)
            self.assertFalse(held)

    def test_acquire_write_failure_cleans_up_and_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with (
                patch("meco_news.maintenance.os.replace", side_effect=OSError("boom")),
                patch("pathlib.Path.unlink", side_effect=OSError("denied")),
                self.assertRaises(OSError),
            ):
                MaintenanceContext.acquire(path, owner="op-1")
            self.assertFalse(Path(str(path) + ".maintenance.json").exists())

    def test_release_reports_second_release_as_miss(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            ctx = MaintenanceContext.acquire(path, owner="op-1")
            self.assertTrue(ctx.release())
            self.assertFalse(ctx.release())

    def test_release_unlink_failure_reports_false(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            ctx = MaintenanceContext.acquire(path, owner="op-1")
            try:
                with patch("pathlib.Path.unlink", side_effect=OSError("denied")):
                    self.assertFalse(ctx.release())
            finally:
                Path(str(path) + ".maintenance.json").unlink(missing_ok=True)


class TestPreflightDivergentStore(unittest.TestCase):
    def test_version_divergence_reports_schema(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            stub = StubStore(version=CURRENT_SCHEMA_VERSION + 1)
            with patch.dict(os.environ, VALID_SECRETS, clear=False), patch("meco_news.preflight.StateStore", return_value=stub):
                code, report = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_SCHEMA)
            self.assertFalse(report.get("ready"))

    def test_integrity_divergence_reports_schema(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            stub = StubStore(integrity="*** database disk image is malformed ***")
            with patch.dict(os.environ, VALID_SECRETS, clear=False), patch("meco_news.preflight.StateStore", return_value=stub):
                code, report = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_SCHEMA)
            self.assertFalse(report.get("ready"))

    def test_store_failure_reports_schema(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            stub = StubStore(error=sqlite3.DatabaseError("locked"))
            with patch.dict(os.environ, VALID_SECRETS, clear=False), patch("meco_news.preflight.StateStore", return_value=stub):
                code, report = run_preflight(config, state_path=path)
            self.assertEqual(code, PREFLIGHT_SCHEMA)
            self.assertFalse(report.get("ready"))
            self.assertEqual(report["checks"]["database"]["reason"], "DatabaseError")


if __name__ == "__main__":
    unittest.main()
