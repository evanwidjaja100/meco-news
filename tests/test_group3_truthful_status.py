"""Group 3 (truthful status) regression tests.

Locks in the F11 production-readiness fix verified by
docs/reviews/2026-09-07-independent/reproduce.py: a corrupt state
database must never report ``missing`` with exit 0. ``--status --json``
reports the truthful inspection state and exits nonzero for any damaged
database, while a missing database (initial setup) and a healthy
database stay exit 0.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from meco_news.app import _state_status
from meco_news.maintenance import MaintenanceContext
from meco_news.migrations import CURRENT_SCHEMA_VERSION, migration_checksum
from meco_news.storage import StateStore

ROOT = Path(__file__).resolve().parents[1]
CONFIG = str(ROOT / "config" / "watchlist.json")
ENV = {"TELEGRAM_BOT_TOKEN": "synthetic-test-token", "TELEGRAM_CHAT_ID": "12345"}


def _fresh_db(path: Path) -> Path:
    with StateStore(path) as store:
        store.acquire_lease("delivery", "fixture", 180)
        store.create_delivery("2026-09-06", config_hash="h", owner_id="fixture")
        store.release_lease("delivery", "fixture")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    return path


def _run_status(path: Path) -> tuple[int, dict[str, Any]]:
    process = subprocess.run(
        [sys.executable, "-m", "meco_news", "--config", CONFIG, "--status", "--json"],
        cwd=str(ROOT),
        env={**os.environ, **ENV, "PYTHONPATH": str(ROOT), "STATE_DB": str(path), "LOG_FILE": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return process.returncode, json.loads(process.stdout)


class Group3TruthfulStatusTests(unittest.TestCase):
    def test_missing_database_reports_missing_exit_0(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.db"
            code, report = _run_status(path)
            self.assertEqual(code, 0)
            self.assertEqual(report["state"], "missing")
            self.assertFalse(path.exists(), "status must stay read-only and never create the database")

    def test_corrupt_bytes_report_corrupt_exit_1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt.db"
            path.write_bytes(b"not a sqlite database")
            code, report = _run_status(path)
            self.assertEqual(code, 1)
            self.assertEqual(report["state"], "corrupt")

    def test_empty_file_reports_malformed_exit_1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.db"
            path.write_bytes(b"")
            code, report = _run_status(path)
            self.assertEqual(code, 1)
            self.assertEqual(report["state"], "malformed")

    def test_newer_schema_reports_newer_incompatible_exit_1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _fresh_db(Path(directory) / "newer.db")
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at, app_version)"
                    " VALUES (?, ?, '2026-09-07T00:00:00+00:00', '9.9.9')",
                    (CURRENT_SCHEMA_VERSION + 1, migration_checksum(CURRENT_SCHEMA_VERSION + 1)),
                )
                connection.commit()
            finally:
                connection.close()
            code, report = _run_status(path)
            self.assertEqual(code, 1)
            self.assertEqual(report["state"], "newer_incompatible")

    def test_healthy_database_reports_ok_exit_0(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _fresh_db(Path(directory) / "healthy.db")
            code, report = _run_status(path)
            self.assertEqual(code, 0)
            self.assertEqual(report["state"], "ok")
            self.assertEqual(report["schema_version"], CURRENT_SCHEMA_VERSION)

    def test_wal_mode_database_reports_ok_exit_0(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _fresh_db(Path(directory) / "wal.db")
            connection = sqlite3.connect(path)
            try:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(str(mode).casefold(), "wal")
            code, report = _run_status(path)
            self.assertEqual(code, 0, f"WAL-mode database must report ok: {report}")
            self.assertEqual(report["state"], "ok")

    def test_status_under_maintenance_reports_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _fresh_db(Path(directory) / "maint.db")
            with MaintenanceContext.acquire(path, owner="fixture"):
                report = _state_status(path)
            self.assertEqual(report["state"], "ok", f"readonly status must keep working under maintenance: {report}")

    def test_state_status_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = _state_status(root / "absent.db")
            self.assertEqual(missing["state"], "missing")
            (root / "corrupt.db").write_bytes(b"not a sqlite database")
            corrupt = _state_status(root / "corrupt.db")
            self.assertEqual(corrupt["state"], "corrupt")
            self.assertIn("integrity", corrupt)
            self.assertIn("detail", corrupt)
            healthy = _state_status(_fresh_db(root / "healthy.db"))
            self.assertEqual(healthy["state"], "ok")
            self.assertEqual(healthy["schema_version"], CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
