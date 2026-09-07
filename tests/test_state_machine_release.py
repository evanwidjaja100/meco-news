"""Release-gate coverage for the durable state machine.

These tests exercise the public transition boundary with disposable SQLite
databases.  Direct SQL is used only to create fault fixtures (tampered hashes,
old timestamps, and orphaned rows); production transitions remain API-driven.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from meco_news.collectors import SourceResult
from meco_news.maintenance import MaintenanceBusy, MaintenanceContext
from meco_news.models import NewsItem
from meco_news.storage import (
    InvalidTransition,
    LeaseLost,
    MigrationRequiredError,
    RetryNotDue,
    StateError,
    StateStore,
)


def _item(number: int = 0) -> NewsItem:
    return NewsItem(
        title=f"Industrial tank project {number}",
        url=f"https://example.com/story/{number}",
        source="Example Publisher",
        published_at=datetime(2026, 9, 6, tzinfo=UTC),
        summary="A new LPG storage project is under construction.",
        topic="lpg_energy",
        topic_label="LPG storage",
        relevance_reason="Potential tank demand.",
        score=12,
        source_host="example.com",
    )


def _complete(store: StateStore, delivery_date: str, owner: str, item: NewsItem | None = None) -> int:
    delivery = store.active_delivery(delivery_date)
    if delivery is None:
        delivery = store.create_delivery(delivery_date, owner_id=owner)
    items = [item] if item is not None else []
    payload = "<b>fixture delivery</b>"
    mapping = {item.fingerprint: 0} if item is not None else {}
    store.prepare_delivery(delivery.delivery_id, items, [payload], owner_id=owner, item_chunk_indexes=mapping)
    chunk = store.due_chunks(delivery.delivery_id)[0]
    run_id = f"fixture-run-{delivery.delivery_id}"
    store.begin_chunk_attempt(chunk.chunk_id, run_id=run_id, owner_id=owner)
    store.finish_chunk(chunk.chunk_id, "accepted", run_id=run_id, owner_id=owner, telegram_message_id="1")
    return delivery.delivery_id


class StateConnectionBoundaryTests(unittest.TestCase):
    def test_memory_readonly_close_and_connection_failure_cleanup(self) -> None:
        memory = StateStore(":memory:")
        self.assertEqual(memory.schema_version, 5)
        memory.close()
        memory.close()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                self.assertEqual(store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(store.connection.execute("PRAGMA synchronous").fetchone()[0], 2)
                self.assertEqual(store.latest_generation("none"), -1)
                store.release_lease("delivery", "owner")
            with StateStore(path, readonly=True) as readonly:
                self.assertEqual(readonly.connection.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertEqual(readonly.schema_version, 5)

            failed = Path(directory) / "failed.db"
            with patch("meco_news.storage.sqlite3.connect", side_effect=OSError("open failed")), self.assertRaises(OSError):
                StateStore(failed)
            runtime_dir = Path(f"{failed}.runtime")
            self.assertFalse(list(runtime_dir.glob("*.json")) if runtime_dir.exists() else [])

    def test_readonly_schema_failures_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty_ledger = root / "empty-ledger.db"
            con = sqlite3.connect(empty_ledger)
            con.execute("CREATE TABLE schema_migrations(version INTEGER, checksum TEXT)")
            con.commit()
            con.close()
            with self.assertRaises(MigrationRequiredError):
                StateStore(empty_ledger, readonly=True)

            source = root / "tampered.db"
            with StateStore(source):
                pass
            con = sqlite3.connect(source)
            con.execute("UPDATE schema_migrations SET checksum='bad' WHERE version=1")
            con.commit()
            con.close()
            with self.assertRaises(StateError):
                StateStore(source, readonly=True)

            missing_table = root / "missing-table.db"
            with StateStore(missing_table):
                pass
            con = sqlite3.connect(missing_table)
            con.execute("DROP TABLE source_results")
            con.commit()
            con.close()
            with self.assertRaises(StateError):
                StateStore(missing_table, readonly=True)

            fake = object.__new__(StateStore)
            fake.connection = MagicMock()
            fake.connection.execute.side_effect = sqlite3.OperationalError("no ledger")
            self.assertEqual(fake.schema_version, 0)

    def test_runtime_authority_is_checked_inside_the_write_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with (
                StateStore(path) as store,
                patch.object(store, "_ensure_writable"),
                patch.object(type(store._runtime_context), "live", new_callable=PropertyMock, return_value=False),
                self.assertRaises(LeaseLost),
            ):
                store.acquire_lease("delivery", "owner", 180)

            with tempfile.TemporaryDirectory() as other_directory:
                other = Path(other_directory) / "other.db"
                with MaintenanceContext.acquire(path, owner="operator") as context, self.assertRaises(MaintenanceBusy):
                    StateStore(other, maintenance_context=context)


class DeliveryTransitionBoundaryTests(unittest.TestCase):
    def test_owner_generation_force_and_direct_completion_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                with self.assertRaises(LeaseLost):
                    store.create_delivery("2026-09-06")
                store.acquire_lease("delivery", "owner", 180)
                first = store.create_delivery(
                    "2026-09-06",
                    owner_id="owner",
                    retry_policy={"enabled": True, "max_attempts": 2, "max_elapsed_seconds": 60},
                )
                with self.assertRaisesRegex(StateError, "direct completion"):
                    store.complete_run("2026-09-06", [_item()], owner_id="owner")
                with self.assertRaisesRegex(StateError, "direct failure"):
                    store.fail_run("2026-09-06", "no", owner_id="owner")
                self.assertEqual(store.latest_delivery("2026-09-06").state, "collecting")
                _complete(store, "2026-09-06", "owner", _item())
                forced = store.create_delivery(
                    "2026-09-06",
                    generation=1,
                    owner_id="owner",
                    predecessor_delivery_id=first.delivery_id,
                    force_operator="operator",
                    force_reason="re-run after a verified completed delivery",
                )
                self.assertEqual(forced.generation, 1)
                self.assertEqual(
                    store.connection.execute("SELECT operator FROM force_audits WHERE delivery_id=?", (forced.delivery_id,)).fetchone()[0],
                    "operator",
                )
                for kwargs in (
                    {"kind": "unsupported"},
                    {"generation": -1},
                    {"force_operator": "operator"},
                    {"predecessor_delivery_id": 999, "force_operator": "operator", "force_reason": "r", "generation": 2},
                    {"predecessor_delivery_id": first.delivery_id, "force_operator": "operator", "force_reason": "r", "generation": 0},
                ):
                    with self.subTest(kwargs=kwargs), self.assertRaises((StateError, InvalidTransition)):
                        store.create_delivery("2026-09-06", owner_id="owner", **kwargs)
                store.release_lease("delivery", "owner")

    def test_prepare_mapping_target_hash_and_chunk_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                first, second = _item(1), _item(2)
                delivery = store.create_delivery("2026-09-06", owner_id="owner", target_snapshot="target")
                with self.assertRaises(StateError):
                    store.prepare_delivery(delivery.delivery_id, [first], [""], owner_id="owner")
                with self.assertRaises(StateError):
                    store.prepare_delivery(delivery.delivery_id, [first, first], ["x"], owner_id="owner")
                with self.assertRaises(StateError):
                    store.prepare_delivery(delivery.delivery_id, [first], ["x"], owner_id="owner", item_chunk_indexes={first.fingerprint: 2})
                prepared = store.prepare_delivery(
                    delivery.delivery_id,
                    [first, second],
                    ["<b>one</b>", "<b>two</b>"],
                    owner_id="owner",
                    item_chunk_indexes={first.fingerprint: 0, second.fingerprint: 1},
                    target_snapshot="target",
                )
                with self.assertRaises(InvalidTransition):
                    store.prepare_delivery(delivery.delivery_id, [first], ["x"], owner_id="owner", target_snapshot="other")
                chunks = store.due_chunks(prepared.delivery_id)
                with self.assertRaises(InvalidTransition):
                    store.begin_chunk_attempt(chunks[1].chunk_id, run_id="run", owner_id="owner")
                chunk, attempt = store.begin_chunk_attempt(chunks[0].chunk_id, run_id="run", owner_id="owner")
                self.assertEqual((chunk.sequence, attempt), (0, 1))
                with self.assertRaises(InvalidTransition):
                    store.finish_chunk(chunk.chunk_id, "accepted", run_id="other", owner_id="owner", telegram_message_id="1")
                with self.assertRaises(StateError):
                    store.finish_chunk(chunk.chunk_id, "accepted", run_id="run", owner_id="owner", telegram_message_id="0")
                store.finish_chunk(chunk.chunk_id, "accepted", run_id="run", owner_id="owner", telegram_message_id="1")
                second_chunk = store.due_chunks(prepared.delivery_id)[0]
                store.begin_chunk_attempt(second_chunk.chunk_id, run_id="run", owner_id="owner")
                store.finish_chunk(second_chunk.chunk_id, "accepted", run_id="run", owner_id="owner", telegram_message_id="2")
                self.assertEqual(store.delivery(prepared.delivery_id).state, "completed")
                self.assertEqual(store.sent_fingerprints([first, second]), {first.fingerprint, second.fingerprint})

    def test_retry_terminal_ambiguous_and_manual_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="owner")
                store.prepare_delivery(delivery.delivery_id, [_item()], ["x"], owner_id="owner")
                chunk = store.due_chunks(delivery.delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="run", owner_id="owner")
                future = datetime.now(UTC) + timedelta(hours=1)
                self.assertEqual(
                    store.finish_chunk(chunk.chunk_id, "rejected_retryable", run_id="run", owner_id="owner", error_class="rate", next_attempt_at=future).state,
                    "retry_wait",
                )
                with self.assertRaises(RetryNotDue):
                    store.begin_chunk_attempt(chunk.chunk_id, run_id="run2", owner_id="owner")
                store.connection.execute("UPDATE outbox_chunks SET next_attempt_at=?", ("2000-01-01T00:00:00+00:00",))
                store.connection.commit()
                store.begin_chunk_attempt(chunk.chunk_id, run_id="run2", owner_id="owner")
                self.assertEqual(
                    store.finish_chunk(chunk.chunk_id, "ambiguous", run_id="run2", owner_id="owner", error_text="unknown").state,
                    "needs_attention",
                )
                store.release_lease("delivery", "owner")
            with self.assertRaises(StateError), StateStore(path) as ordinary:
                ordinary.resolve_chunk(chunk.chunk_id, "retry", reason="x", operator="op")
            with MaintenanceContext.acquire(path, owner="operator") as context, StateStore(path, maintenance_context=context) as store:
                self.assertEqual(store.resolve_chunk(chunk.chunk_id, "retry", reason="confirmed not delivered", operator="op", maintenance_context=context).state, "sending")
                store.connection.execute("UPDATE outbox_chunks SET error_class='operator_retry_safe',state='failed_terminal' WHERE chunk_id=?", (chunk.chunk_id,))
                store.connection.execute("UPDATE deliveries SET state='failed_terminal' WHERE delivery_id=?", (delivery.delivery_id,))
                store.connection.commit()
                with self.assertRaises(InvalidTransition):
                    store.resolve_chunk(chunk.chunk_id, "sent", reason="no evidence", operator="op", maintenance_context=context)

    def test_collection_retry_clock_and_recovery_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            now = datetime.now(UTC)
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                retry = store.ensure_collection_retry(
                    "2026-09-06", run_id="r", config_hash="h", next_attempt_at=now + timedelta(hours=1), error="down", owner_id="owner"
                )
                same = store.ensure_collection_retry(
                    "2026-09-06", run_id="r2", config_hash="h", next_attempt_at=now + timedelta(hours=2), error="still down", owner_id="owner"
                )
                self.assertEqual(retry.delivery_id, same.delivery_id)
                with self.assertRaises(RetryNotDue):
                    store.reopen_collection_retry(retry.delivery_id, now=now, owner_id="owner")
                reopened = store.reopen_collection_retry(retry.delivery_id, now=now + timedelta(days=1), owner_id="owner")
                store.set_collection_retry(reopened.delivery_id, next_attempt_at=now, error="again", owner_id="owner")
                self.assertEqual(store.record_collection_attempt(reopened.delivery_id, run_id="r3", error="down", outcome="collection_retry", owner_id="owner"), 1)
                self.assertTrue(store.retry_budget_exhausted(reopened.delivery_id, max_attempts=1, max_elapsed_seconds=60, now=now + timedelta(days=1)))
                self.assertTrue(store.retry_budget_exhausted(999, max_attempts=2, max_elapsed_seconds=60, now=now))
                store.connection.execute("UPDATE deliveries SET started_at='not-a-date' WHERE delivery_id=?", (reopened.delivery_id,))
                store.connection.commit()
                self.assertTrue(store.retry_budget_exhausted(reopened.delivery_id, max_attempts=2, max_elapsed_seconds=60, now=now))
                store.release_lease("delivery", "owner")

            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delivery = store.create_delivery("2026-09-07", owner_id="owner")
                store.prepare_delivery(delivery.delivery_id, [], ["x"], owner_id="owner")
                self.assertEqual(store.fail_delivery(delivery.delivery_id, "bad", owner_id="owner").state, "failed_terminal")
                with self.assertRaises(InvalidTransition):
                    store.fail_delivery(delivery.delivery_id, "again", owner_id="owner")
                store.release_lease("delivery", "owner")

    def test_orphan_recovery_and_prune_preserve_attention_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            old = datetime(2020, 1, 1, tzinfo=UTC)
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                completed_id = _complete(store, "2026-09-06", "owner", _item(10))
                active = store.create_delivery("2026-09-07", owner_id="owner")
                store.prepare_delivery(active.delivery_id, [_item(11)], ["x"], owner_id="owner")
                chunk = store.due_chunks(active.delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="run", owner_id="owner", now=old)
                store.record_source_results(completed_id, [SourceResult("s", "source", "succeeded")], owner_id="owner")
                store.connection.execute("DELETE FROM run_leases WHERE scope='delivery'")
                store.connection.execute("UPDATE delivery_attempts SET ended_at=? WHERE delivery_id=?", (old.isoformat(), completed_id))
                store.connection.execute("UPDATE article_history SET sent_at=? WHERE delivery_id=?", (old.isoformat(), completed_id))
                store.connection.execute("UPDATE source_results SET created_at=? WHERE delivery_id=?", (old.isoformat(), completed_id))
                store.connection.commit()
                self.assertTrue(store.recover_expired_lease(now=datetime(2021, 1, 1, tzinfo=UTC)))
                self.assertEqual(store.unresolved_count(active.delivery_id), 1)
                store.acquire_lease("delivery", "owner", 180)
                store.release_lease("delivery", "owner")

            with MaintenanceContext.acquire(path, owner="retention") as context, StateStore(path, maintenance_context=context) as store:
                preview = store.prune_history(now=datetime(2026, 9, 6, tzinfo=UTC), attempt_retention_days=1, article_retention_days=1)
                self.assertTrue(all(value >= 1 for value in preview["counts"].values()))
                self.assertEqual(store.unresolved_count(active.delivery_id), 1)
                applied = store.prune_history(now=datetime(2026, 9, 6, tzinfo=UTC), attempt_retention_days=1, article_retention_days=1, apply=True)
                self.assertTrue(applied["applied"])
                self.assertEqual(store.unresolved_count(active.delivery_id), 1)
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM article_history WHERE delivery_id=?", (completed_id,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
