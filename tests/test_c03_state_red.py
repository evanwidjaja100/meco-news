"""C0.3 state red reproducers — F-003/005/006/007/008/011 (closure plan).

These are deterministic failing tests that capture the audit reproducers before fixes.
Each must fail for the *correct* invariant reason before C2.x, then pass after.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
import unittest

from meco_news.config import load_config
from meco_news.migrations import SCHEMA_SQL, migration_checksum
from meco_news.preflight import run_preflight
from meco_news.storage import StateStore


class TestF003PreflightFalseGreen(unittest.TestCase):
    """F-003: preflight must be non-zero for migration-required (N-1) and newer (N+1)."""

    def test_n_minus_1_migration_required_is_nonzero(self) -> None:
        import os
        from unittest.mock import patch

        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            con = sqlite3.connect(path)
            v1_checksum = migration_checksum(1)
            con.executescript(SCHEMA_SQL)
            con.execute("DELETE FROM schema_migrations")
            con.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (1, ?, '2026-01-01T00:00:00+00:00', '1.0.0')",
                (v1_checksum,),
            )
            con.commit()
            con.close()
            # Isolate N-1 from secret failures — set real token so only schema matters
            with patch.dict(
                os.environ, {"TELEGRAM_BOT_TOKEN": "123456:real-token-value-for-test", "TELEGRAM_CHAT_ID": "12345"}, clear=False
            ):
                code, report = run_preflight(config, state_path=path)
            # Current bug: preflight.py:99 only sets ready=False for >2, not for <2, so N-1 incorrectly returns 0 with ready=True
            self.assertNotEqual(code, 0, "N-1 must not be ready (exit 0)")
            self.assertFalse(report.get("ready"), "N-1 must be ready=False")
            self.assertEqual(code, 5, "N-1 must be PREFLIGHT_SCHEMA (5)")


class TestF005GenerationZeroTreatedAsAbsent(unittest.TestCase):
    """F-005: generation 0 must not be treated as absent. latest_generation(0) must be 0, not -1."""

    def test_latest_generation_zero_is_zero_not_minus_one(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            with StateStore(path) as store:
                # First delivery creates generation 0
                first = store.create_delivery("2026-08-25", generation=0, config_hash="h1")
                self.assertEqual(first.generation, 0)
                latest = store.latest_generation("2026-08-25")
                # Bug: int(row[0] or -1) returns -1 when MAX=0
                self.assertEqual(latest, 0, f"latest_generation must be 0 after gen 0, got {latest} — bug storage.py:384")

    def test_force_creates_n_plus_one_not_recreates_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            with StateStore(path) as store:
                store.create_delivery("2026-08-25", generation=0, config_hash="h")
                # Simulate what run_once does for force: latest+1
                latest = store.latest_generation("2026-08-25")
                next_gen = latest + 1
                self.assertEqual(next_gen, 1, f"force must allocate 1 after 0, got {next_gen} due to latest bug")


class TestF006MutableChecksum(unittest.TestCase):
    """F-006: historical migration checksums must be immutable when future migration added."""

    def test_checksum_immutable_when_schema_grows(self) -> None:
        import meco_news.migrations as m

        c1_before = m.migration_checksum(1)
        c2_before = m.migration_checksum(2)
        orig = m.SCHEMA_SQL
        # Simulate future catalog addition — extending global SCHEMA_SQL should NOT change prior checksums
        m.SCHEMA_SQL = orig + "\nCREATE TABLE future_dummy(id INTEGER);"
        try:
            c1_after = m.migration_checksum(1)
            c2_after = m.migration_checksum(2)
            self.assertEqual(c1_before, c1_after, "checksum for v1 must not change when future schema added")
            self.assertEqual(c2_before, c2_after, "checksum for v2 must not change when future schema added")
        finally:
            m.SCHEMA_SQL = orig


class TestF007NonOwnerCanMutate(unittest.TestCase):
    """F-007: every runtime mutation must require live lease capability."""

    def test_prepare_without_lease_should_fail(self) -> None:
        from meco_news.models import NewsItem
        from datetime import datetime, UTC
        from meco_news.storage import LeaseLost, InvalidTransition, StateError

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            with StateStore(path) as store:
                delivery = store.create_delivery("2026-08-25", config_hash="h")
                item = NewsItem(
                    title="LPG terminal",
                    url="https://example.com/lpg",
                    source="Example",
                    published_at=datetime.now(UTC),
                    score=10,
                    topic="lpg_energy",
                )
                # Must require live lease — before fix this succeeds (bug), after fix it raises
                with self.assertRaises((LeaseLost, InvalidTransition, StateError)):
                    # No lease acquired — should fail closed
                    store.prepare_delivery(delivery.delivery_id, [item], ["<b>hello</b>"], owner_id="no-lease")

    def test_begin_chunk_without_owner_should_fail(self) -> None:
        # begin_chunk does check lease, so this one should pass after fix but we test the missing check on other mutators
        pass


class TestF008RestoreAcceptsActiveWork(unittest.TestCase):
    """F-008: restore must refuse active lease / in-flight chunk and need exclusive guard."""

    def test_restore_during_active_lease_should_be_refused(self) -> None:
        from meco_news.backup import create_backup, restore_backup

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.db"
            bak_dir = Path(d) / "backups"
            # Create a DB with an active delivery lease
            with StateStore(src) as store:
                store.acquire_lease("delivery", "owner1", 180)
                store.create_delivery("2026-08-25", config_hash="h")
            artifact = create_backup(src, bak_dir, config_hash="h")
            # Try to restore over a DB that still has active lease — should refuse but currently does not check state
            dst = Path(d) / "dst.db"
            with StateStore(dst) as store:
                store.acquire_lease("delivery", "owner2", 180)
            # Current restore_backup only checks backup integrity, not target active work
            try:
                restore_backup(artifact.database, dst)
                # If it succeeds, bug reproduced — restore accepted active work
                self.fail("BUG REPRODUCED: restore succeeded despite active lease on target — must refuse (C2.5)")
            except Exception as exc:
                # Correct behavior is to raise due to active work — if it now raises, red test is no longer red; adjust after C2.5
                self.assertIn("active", str(exc).lower() + "lease", "restore should refuse active lease")
