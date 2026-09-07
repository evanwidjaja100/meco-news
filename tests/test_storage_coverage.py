from __future__ import annotations

from datetime import datetime, timedelta, UTC
from pathlib import Path
import sqlite3
import tempfile
import unittest

from meco_news.collectors import SourceResult
from meco_news.maintenance import MaintenanceContext
from meco_news.models import NewsItem
from meco_news.storage import (
    DatabaseReadOnly,
    InvalidTransition,
    LeaseLost,
    RetryNotDue,
    StateError,
    StateStore,
    _key,
)


def _item(number: int = 0) -> NewsItem:
    return NewsItem(
        title=f"Industrial tank project {number}",
        url=f"https://example.com/story/{number}",
        source="Example",
        published_at=datetime(2026, 9, 6, tzinfo=UTC),
        summary="A new LPG storage project is under construction.",
        topic="lpg_energy",
        topic_label="LPG",
        relevance_reason="Tank demand",
        score=12,
        source_host="example.com",
    )


class StorageBoundaryCorpusTests(unittest.TestCase):
    def test_static_helpers_and_authority_edges(self) -> None:
        self.assertEqual(StateStore._delivery(None), None)
        v2 = StateStore._delivery((1, "2026-09-06", 0, "content", "prepared", "run", "hash", "next", "error"))
        self.assertIsNotNone(v2)
        self.assertEqual(v2.target_snapshot, "")  # type: ignore[union-attr]
        self.assertEqual(StateStore._retry_policy_json(None), "{}")
        self.assertEqual(StateStore._retry_policy_json({"enabled": True, "max_attempts": 2}), '{"enabled":true,"max_attempts":2}')
        for policy in ([], {"unknown": 1}, {"enabled": 1}, {"max_attempts": -1}):
            with self.assertRaises(StateError):
                StateStore._retry_policy_json(policy)  # type: ignore[arg-type]
        empty = object.__new__(StateStore)
        empty.readonly = False
        empty.path = None
        empty._maintenance_context = None
        empty._runtime_context = None
        empty._ensure_writable()
        with self.assertRaises(LeaseLost):
            StateStore._require_runtime_owner(None)
        with self.assertRaises(LeaseLost):
            StateStore._require_runtime_owner("   ")

    def test_backup_identity_and_legacy_status_edges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="owner")
                self.assertFalse(store.already_completed("legacy-date"))
                many = [_item(index) for index in range(401)]
                self.assertEqual(store.identity_keys(many, title_dedupe_days=0), (set(), set()))
                store.connection.execute("BEGIN")
                target = Path(directory) / "snapshot.db"
                store.backup_to(target)
                self.assertTrue(target.exists())
                store.connection.rollback()
                self.assertEqual(store.delivery(delivery.delivery_id).state, "collecting")  # type: ignore[union-attr]
                store.release_lease("delivery", "owner")

            class FailingConnection:
                in_transaction = False

                def execute(self, _sql: str):
                    return self

                def backup(self, _destination: sqlite3.Connection) -> None:
                    raise sqlite3.Error("copy failed")

                def rollback(self) -> None:
                    self.rolled_back = True

            fake = object.__new__(StateStore)
            fake.path = Path(directory) / "state.db"
            fake.connection = FailingConnection()
            with self.assertRaises(sqlite3.Error):
                fake.backup_to(Path(directory) / "failed.db")
            self.assertTrue(fake.connection.rolled_back)

    def test_two_chunk_ordering_and_remaining_delivery_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="owner")
                first, second = _item(1), _item(2)
                store.prepare_delivery(
                    delivery.delivery_id,
                    [first, second],
                    ["<b>first</b>", "<b>second</b>"],
                    owner_id="owner",
                    item_chunk_indexes={first.fingerprint: 0, second.fingerprint: 1},
                )
                chunks = store.due_chunks(delivery.delivery_id)
                with self.assertRaises(InvalidTransition):
                    store.begin_chunk_attempt(chunks[1].chunk_id, run_id="run", owner_id="owner")
                first_chunk, first_attempt = store.begin_chunk_attempt(chunks[0].chunk_id, run_id="run", owner_id="owner")
                updated = store.finish_chunk(
                    first_chunk.chunk_id, "accepted", run_id="run", owner_id="owner", telegram_message_id="1"
                )
                self.assertEqual(updated.state, "sending")
                second_chunk, second_attempt = store.begin_chunk_attempt(chunks[1].chunk_id, run_id="run", owner_id="owner")
                self.assertEqual((first_attempt, second_attempt), (1, 1))
                self.assertEqual(
                    store.finish_chunk(second_chunk.chunk_id, "accepted", run_id="run", owner_id="owner", telegram_message_id="2").state,
                    "completed",
                )
                store.release_lease("delivery", "owner")

    def test_recovery_and_target_idempotence_edges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="owner")
                self.assertFalse(store.recover_expired_lease(now=datetime.now(UTC)))
                with self.assertRaises(InvalidTransition):
                    store.mark_target_mismatch(9999, owner_id="owner", expected="", actual="new")
                store.prepare_delivery(delivery.delivery_id, [], ["<b>x</b>"], owner_id="owner", target_snapshot="same")
                unchanged = store.mark_target_mismatch(delivery.delivery_id, owner_id="owner", expected="same", actual="same")
                self.assertEqual(unchanged.state, "prepared_empty")
                store.release_lease("delivery", "owner")

    def test_manual_resolution_retry_and_nonfinal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="owner")
                first, second = _item(3), _item(4)
                store.prepare_delivery(
                    delivery.delivery_id,
                    [first, second],
                    ["<b>one</b>", "<b>two</b>"],
                    owner_id="owner",
                    item_chunk_indexes={first.fingerprint: 0, second.fingerprint: 1},
                )
                chunks = store.due_chunks(delivery.delivery_id)
                store.begin_chunk_attempt(chunks[0].chunk_id, run_id="run", owner_id="owner")
                store.finish_chunk(chunks[0].chunk_id, "ambiguous", run_id="run", owner_id="owner", error_text="unknown")
                store.release_lease("delivery", "owner")
            with MaintenanceContext.acquire(path, owner="operator") as context, StateStore(path, maintenance_context=context) as store:
                retried = store.resolve_chunk(chunks[0].chunk_id, "retry", reason="operator confirmed retry", operator="operator", maintenance_context=context)
                self.assertEqual(retried.state, "sending")
                store.connection.execute("UPDATE outbox_chunks SET state='ambiguous' WHERE chunk_id=?", (chunks[1].chunk_id,))
                store.connection.execute("UPDATE deliveries SET state='needs_attention' WHERE delivery_id=?", (delivery.delivery_id,))
                store.connection.commit()
                sent = store.resolve_chunk(chunks[1].chunk_id, "sent", reason="confirmed externally", operator="operator", maintenance_context=context)
                self.assertEqual(sent.state, "sending")
    def test_memory_readonly_and_utility_boundaries(self) -> None:
        store = StateStore(":memory:")
        self.assertEqual(store.schema_version, 5)
        self.assertEqual(store.sent_fingerprints([]), set())
        self.assertEqual(store.identity_keys([]), (set(), set()))
        store.close()
        store.close()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as writable:
                writable.acquire_lease("delivery", "owner", 180)
                delivery = writable.create_delivery("2026-09-06", owner_id="owner")
                writable.release_lease("delivery", "owner")
            with StateStore(path, readonly=True) as readonly:
                with self.assertRaises(DatabaseReadOnly):
                    readonly.fail_run("2026-09-06", "x", owner_id="owner")
                with self.assertRaises(StateError):
                    readonly.backup_to(path)
                self.assertEqual(readonly.latest_generation("missing"), -1)
                self.assertIsNone(readonly.delivery(999))
                self.assertEqual(readonly.active_delivery("missing"), None)
                self.assertEqual(readonly._delivery(None), None)
            self.assertEqual(delivery.generation, 0)

    def test_retry_policy_and_generation_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                with self.assertRaises(LeaseLost):
                    store.create_delivery("2026-09-06")
                with self.assertRaises(StateError):
                    store.create_delivery("2026-09-06", owner_id="owner", kind="bad")
                with self.assertRaises(StateError):
                    store.create_delivery("2026-09-06", owner_id="owner", generation=-1)
                with self.assertRaises(StateError):
                    store.create_delivery("2026-09-06", owner_id="owner", force_operator="op")
                with self.assertRaises(StateError):
                    store.create_delivery("2026-09-06", owner_id="owner", predecessor_delivery_id=1, force_reason="reason")
                for policy in ({"unknown": 1}, {"enabled": 1}, {"max_attempts": True}, {"max_attempts": -1}):
                    with self.assertRaises(StateError):
                        store.create_delivery("2026-09-06", owner_id="owner", retry_policy=policy)
                delivery = store.create_delivery(
                    "2026-09-06",
                    owner_id="owner",
                    retry_policy={"enabled": True, "max_attempts": 3, "max_elapsed_seconds": 60},
                )
                self.assertEqual(store.delivery_retry_policy(delivery.delivery_id)["max_attempts"], 3)
                self.assertEqual(store.delivery_retry_policy(999, {"max_attempts": 2}), {"max_attempts": 2})
                store.connection.execute("UPDATE deliveries SET retry_policy_json='not-json' WHERE delivery_id=?", (delivery.delivery_id,))
                store.connection.commit()
                self.assertEqual(store.delivery_retry_policy(delivery.delivery_id, {"fallback": 1}), {"fallback": 1})
                store.release_lease("delivery", "owner")

    def test_compatibility_run_and_lease_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                with self.assertRaises(LeaseLost):
                    store.start_run("2026-09-06", owner_id="wrong")
                store.acquire_lease("delivery", "owner", 180)
                store.start_run("2026-09-06", owner_id="owner")
                store.start_run("2026-09-06", owner_id="owner")
                with self.assertRaisesRegex(StateError, "direct completion"):
                    store.complete_run("2026-09-06", [], owner_id="owner")
                with self.assertRaisesRegex(StateError, "direct failure"):
                    store.fail_run("2026-09-07", "<secret>", owner_id="owner")
                self.assertFalse(store.already_completed("2026-09-06"))
                with self.assertRaises(LeaseLost):
                    store.release_lease("other", "owner")
                self.assertTrue(store.release_lease("delivery", "owner"))
                with self.assertRaises(LeaseLost):
                    store.heartbeat_lease("delivery", "owner")

    def test_prepare_and_chunk_state_machine_negative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="owner")
                item = _item()
                with self.assertRaises(StateError):
                    store.prepare_delivery(delivery.delivery_id, [], [], owner_id="owner")
                with self.assertRaises(StateError):
                    store.prepare_delivery(delivery.delivery_id, [item, item], ["x"], owner_id="owner")
                with self.assertRaises(StateError):
                    store.prepare_delivery(delivery.delivery_id, [item], ["x"], owner_id="owner", item_chunk_indexes={item.fingerprint: 2})
                prepared = store.prepare_delivery(delivery.delivery_id, [item], ["<b>x</b>"], owner_id="owner", target_snapshot="old")
                with self.assertRaises(InvalidTransition):
                    store.prepare_delivery(delivery.delivery_id, [item], ["x"], owner_id="owner", target_snapshot="new")
                chunk = store.due_chunks(prepared.delivery_id)[0]
                with self.assertRaises(InvalidTransition):
                    store.begin_chunk_attempt(99999, run_id="run", owner_id="owner")
                store.connection.execute("UPDATE outbox_chunks SET payload_hash=? WHERE chunk_id=?", ("bad", chunk.chunk_id))
                store.connection.commit()
                with self.assertRaises(StateError):
                    store.begin_chunk_attempt(chunk.chunk_id, run_id="run", owner_id="owner")
                store.connection.execute("UPDATE outbox_chunks SET payload_hash=? WHERE chunk_id=?", (_key(chunk.payload), chunk.chunk_id))
                store.connection.commit()
                with self.assertRaises(StateError):
                    store.finish_chunk(chunk.chunk_id, "accepted", run_id="run", owner_id="owner", telegram_message_id="0")
                with self.assertRaises(ValueError):
                    store.finish_chunk(chunk.chunk_id, "bad", run_id="run", owner_id="owner")
                store.release_lease("delivery", "owner")

    def test_outbox_retry_terminal_and_ambiguous_paths(self) -> None:
        for outcome, error_class in (("rejected_retryable", "rate"), ("rejected_terminal", "operator_retry_safe"), ("ambiguous", "unknown")):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.db"
                with StateStore(path) as store:
                    store.acquire_lease("delivery", "owner", 180)
                    delivery = store.create_delivery("2026-09-06", owner_id="owner")
                    item = _item()
                    store.prepare_delivery(delivery.delivery_id, [item], ["x"], owner_id="owner")
                    chunk = store.due_chunks(delivery.delivery_id)[0]
                    store.begin_chunk_attempt(chunk.chunk_id, run_id="run", owner_id="owner")
                    if outcome == "rejected_retryable":
                        updated = store.finish_chunk(chunk.chunk_id, outcome, run_id="run", owner_id="owner", error_class=error_class, next_attempt_at=datetime.now(UTC) + timedelta(hours=1))
                        self.assertEqual(updated.state, "retry_wait")
                        self.assertEqual(store.due_chunks(delivery.delivery_id), [])
                    elif outcome == "rejected_terminal":
                        updated = store.finish_chunk(chunk.chunk_id, outcome, run_id="run", owner_id="owner", error_class=error_class, error_text="terminal")
                        self.assertEqual(updated.state, "failed_terminal")
                    else:
                        updated = store.finish_chunk(chunk.chunk_id, outcome, run_id="run", owner_id="owner", error_class=error_class, error_text="uncertain")
                        self.assertEqual(updated.state, "needs_attention")
                    store.release_lease("delivery", "owner")

    def test_collection_retry_and_retry_budget_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            now = datetime(2026, 9, 6, tzinfo=UTC)
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                retry = store.ensure_collection_retry("2026-09-06", run_id="r", config_hash="h", next_attempt_at=now + timedelta(hours=1), error="down", owner_id="owner")
                same = store.ensure_collection_retry("2026-09-06", run_id="r2", config_hash="h", next_attempt_at=now + timedelta(hours=2), error="still down", owner_id="owner")
                self.assertEqual(retry.delivery_id, same.delivery_id)
                with self.assertRaises(RetryNotDue):
                    store.reopen_collection_retry(retry.delivery_id, now=now, owner_id="owner")
                reopened = store.reopen_collection_retry(retry.delivery_id, now=now + timedelta(days=1), owner_id="owner")
                self.assertEqual(reopened.state, "collecting")
                store.set_collection_retry(reopened.delivery_id, next_attempt_at=now, error="again", owner_id="owner")
                self.assertEqual(store.record_collection_attempt(reopened.delivery_id, run_id="r3", error="down", outcome="collection_retry", owner_id="owner"), 1)
                self.assertTrue(store.retry_budget_exhausted(reopened.delivery_id, max_attempts=1, max_elapsed_seconds=60, now=now + timedelta(days=1)))
                self.assertTrue(store.retry_budget_exhausted(999, max_attempts=2, max_elapsed_seconds=60, now=now))
                store.release_lease("delivery", "owner")

    def test_recovery_source_results_target_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="owner")
                item = _item()
                store.prepare_delivery(delivery.delivery_id, [item], ["x"], owner_id="owner")
                store.record_source_results(delivery.delivery_id, [SourceResult("s", "S", "failed", reason_code="network_error", error="token=secret")], owner_id="owner")
                chunk = store.due_chunks(delivery.delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="run", owner_id="owner", now=datetime(2020, 1, 1, tzinfo=UTC))
                store.connection.execute("DELETE FROM run_leases WHERE scope='delivery'")
                store.connection.commit()
                self.assertTrue(store.recover_expired_lease(now=datetime(2021, 1, 1, tzinfo=UTC)))
                self.assertEqual(store.unresolved_count(delivery.delivery_id), 1)
                self.assertEqual(store.status_snapshot()["active_chunk"]["state"], "ambiguous")
                store.acquire_lease("delivery", "owner", 180)
                with self.assertRaises(InvalidTransition):
                    store.mark_target_mismatch(delivery.delivery_id, owner_id="owner", expected="wrong", actual="new")
                store.release_lease("delivery", "owner")

    def test_manual_resolution_sent_and_safe_terminal_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="owner")
                item = _item()
                store.prepare_delivery(delivery.delivery_id, [item], ["x"], owner_id="owner")
                chunk = store.due_chunks(delivery.delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="run", owner_id="owner")
                store.finish_chunk(chunk.chunk_id, "ambiguous", run_id="run", owner_id="owner", error_text="unknown")
                store.release_lease("delivery", "owner")
            with StateStore(path) as ordinary, self.assertRaises(StateError):
                ordinary.resolve_chunk(chunk.chunk_id, "sent", reason="r", operator="o")
            with MaintenanceContext.acquire(path, owner="operator") as context, StateStore(path, maintenance_context=context) as store:
                with self.assertRaises(ValueError):
                    store.resolve_chunk(chunk.chunk_id, "bad", reason="r", operator="o")
                resolved = store.resolve_chunk(chunk.chunk_id, "sent", reason="confirmed", operator="o", maintenance_context=context)
                self.assertEqual(resolved.state, "completed")
                with self.assertRaises(InvalidTransition):
                    store.resolve_chunk(chunk.chunk_id, "sent", reason="again", operator="o", maintenance_context=context)


if __name__ == "__main__":
    unittest.main()
