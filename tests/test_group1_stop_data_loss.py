"""Group 1 (stop data loss) regression tests.

Locks in the three production-readiness fixes verified by
docs/reviews/2026-09-07-independent/reproduce.py:

- restore reconciles post-backup acknowledgments instead of replaying sends,
- an interrupted restore auto-rolls back on next open (no silent fresh DB),
- an exhausted frozen retry budget fails terminal with zero sends,
- a backup holding unresolved in-flight/ambiguous work refuses restore.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, UTC
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from meco_news import app, maintenance
from meco_news.backup import create_backup, restore_backup
from meco_news.config import load_config
from meco_news.models import NewsItem
from meco_news.storage import StateError, StateStore

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config/watchlist.json")
ENV = {"TELEGRAM_BOT_TOKEN": "synthetic-review-token", "TELEGRAM_CHAT_ID": "12345"}


def _prepare(path, *, snapshot="", policy=None):
    if not snapshot:
        with patch.dict(os.environ, ENV):
            snapshot = app._target_snapshot(CONFIG)
    with StateStore(path) as store:
        store.acquire_lease("delivery", "setup", 180)
        delivery = store.create_delivery(app._delivery_date(CONFIG), owner_id="setup", retry_policy=policy)
        item = NewsItem(
            title="Industrial project",
            url="https://example.com/industrial",
            source="Fixture",
            published_at=datetime.now(UTC),
        )
        store.prepare_delivery(
            delivery.delivery_id,
            [item],
            ["<b>Original frozen digest</b>"],
            owner_id="setup",
            target_snapshot=snapshot,
        )
        chunk = store.due_chunks(delivery.delivery_id)[0]
        store.release_lease("delivery", "setup")
    return delivery.delivery_id, chunk.chunk_id


class _SimulatedCrash(RuntimeError):
    pass


class Group1StopDataLossTests(unittest.TestCase):
    def test_restore_does_not_replay_acknowledged_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "restore.db"
            delivery_id, chunk_id = _prepare(path)
            backup = create_backup(path, root / "backups")
            with StateStore(path) as store:
                store.acquire_lease("delivery", "sender", 180)
                store.begin_chunk_attempt(chunk_id, run_id="first", owner_id="sender")
                store.finish_chunk(
                    chunk_id, "accepted", run_id="first", owner_id="sender", telegram_message_id="10"
                )
                store.release_lease("delivery", "sender")
            restore_backup(backup.database, path)
            with StateStore(path, readonly=True) as store:
                self.assertEqual(store.delivery(delivery_id).state, "completed")
            with (
                patch.dict(os.environ, {**ENV, "STATE_DB": str(path)}),
                patch.object(app, "TelegramClient") as client,
                patch.object(app, "collect_all", side_effect=AssertionError("unexpected collection")),
            ):
                client.return_value.send_html.return_value = "11"
                result = app.run_once(CONFIG)
            self.assertEqual(client.return_value.send_html.call_count, 0)
            self.assertEqual(result.outcome, "already_completed")

    def test_exhausted_frozen_retry_budget_sends_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "retry.db"
            policy = {"enabled": True, "max_attempts": 4, "max_elapsed_seconds": 60}
            delivery_id, chunk_id = _prepare(path, policy=policy)
            with StateStore(path) as store:
                store.acquire_lease("delivery", "sender", 180)
                store.begin_chunk_attempt(chunk_id, run_id="first", owner_id="sender")
                store.finish_chunk(
                    chunk_id,
                    "rejected_retryable",
                    run_id="first",
                    owner_id="sender",
                    next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
                )
                past = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
                store.connection.execute("UPDATE delivery_attempts SET started_at=?", (past,))
                store.connection.commit()
                exhausted = store.retry_budget_exhausted(
                    delivery_id, chunk_id=chunk_id, max_attempts=4, max_elapsed_seconds=60
                )
                store.release_lease("delivery", "sender")
            self.assertTrue(exhausted)
            with (
                patch.dict(os.environ, {**ENV, "STATE_DB": str(path)}),
                patch.object(app, "TelegramClient") as client,
            ):
                client.return_value.send_html.return_value = "12"
                result = app.run_once(CONFIG)
            self.assertEqual(result.outcome, "failed_terminal")
            self.assertEqual(client.return_value.send_html.call_count, 0)

    def test_interrupted_restore_recovers_prior_state_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "crash-restore.db"
            _delivery_id, chunk_id = _prepare(path)
            with StateStore(path) as store:
                store.acquire_lease("delivery", "sender", 180)
                store.begin_chunk_attempt(chunk_id, run_id="first", owner_id="sender")
                store.finish_chunk(
                    chunk_id, "accepted", run_id="first", owner_id="sender", telegram_message_id="20"
                )
                store.release_lease("delivery", "sender")
            artifact = create_backup(path, root / "backups")
            expected = os.fspath(path.resolve())
            real_replace = os.replace

            def _die_like_process_death(source, destination):
                if os.fspath(destination) == expected:
                    raise _SimulatedCrash("simulated process death before install completes")
                result = real_replace(source, destination)
                if os.fspath(source) == expected:
                    raise _SimulatedCrash("simulated process death after target rename")
                return result

            with self.assertRaises(_SimulatedCrash), patch(
                "meco_news.backup.os.replace", side_effect=_die_like_process_death
            ):
                restore_backup(artifact.database, path)
            self.assertFalse(path.exists())
            with (
                patch.object(
                    maintenance, "_utc_now", return_value=datetime.now(UTC) + timedelta(hours=2)
                ),
                StateStore(path) as store,
            ):
                remaining = store.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
            self.assertEqual(remaining, 1)
            self.assertTrue(path.exists())

    def test_backup_with_unresolved_work_refuses_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "unresolved.db"
            _delivery_id, chunk_id = _prepare(path)
            with StateStore(path) as store:
                store.acquire_lease("delivery", "sender", 180)
                store.begin_chunk_attempt(chunk_id, run_id="first", owner_id="sender")
                store.release_lease("delivery", "sender")
            artifact = create_backup(path, root / "backups")
            with self.assertRaises(StateError):
                restore_backup(artifact.database, root / "restore-target.db")


if __name__ == "__main__":
    unittest.main()
