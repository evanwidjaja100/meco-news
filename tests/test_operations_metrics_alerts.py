from __future__ import annotations

from datetime import datetime, UTC
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from meco_news.alerts import AlertRecord, JsonlAlertSink, MemoryAlertSink, evaluate_backup, evaluate_health
from meco_news.backup import create_backup
from meco_news.metrics import metrics_schema, metrics_snapshot
from meco_news.operations import (
    BackupBusy,
    BackupJobLock,
    RetentionPlan,
    apply_retention_plan,
    build_retention_plan,
    inventory_backups,
    prune_state_history,
    scheduled_backup,
    write_replication_receipt,
)
import meco_news.operations as operations
from meco_news.models import NewsItem
from meco_news.storage import StateError, StateStore


class OperationsFixtureTests(unittest.TestCase):
    def _state(self, path: Path) -> None:
        item = NewsItem(
            title="LPG terminal project",
            url="https://example.com/lpg",
            source="Example",
            published_at=datetime.now(UTC),
            score=12,
            topic="lpg_energy",
        )
        with StateStore(path) as store:
            store.acquire_lease("delivery", "fixture", 180)
            delivery = store.create_delivery("2026-09-06", owner_id="fixture", config_hash="h")
            store.prepare_delivery(delivery.delivery_id, [item], ["<b>hello</b>"], owner_id="fixture")
            chunk = store.due_chunks(delivery.delivery_id)[0]
            store.begin_chunk_attempt(chunk.chunk_id, run_id="run", owner_id="fixture")
            store.finish_chunk(chunk.chunk_id, "accepted", run_id="run", owner_id="fixture", telegram_message_id="1")
            store.release_lease("delivery", "fixture")

    def test_metrics_missing_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            missing = metrics_snapshot(path, now=datetime(2026, 9, 6, tzinfo=UTC))
            self.assertEqual(missing["state"], "missing")
            self.assertEqual(missing["metrics"]["runs_total"], 0)
            schema = metrics_schema()
            self.assertEqual(schema["schema_version"], 1)
            self.assertIn("chunk_duration_seconds_total", schema["counters"])
            self.assertFalse(path.exists())

    def test_metrics_counts_committed_state_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            self._state(path)
            before = path.read_bytes()
            report = metrics_snapshot(path)
            self.assertEqual(report["state"], "ok")
            self.assertEqual(report["metrics"]["runs_total"], 1)
            self.assertEqual(report["metrics"]["runs_completed"], 1)
            self.assertEqual(report["metrics"]["chunks_sent"], 1)
            self.assertEqual(report["metrics"]["chunk_attempts_total"], 1)
            self.assertEqual(before, path.read_bytes())

    def test_metrics_unreadable_is_not_false_green(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            path.write_bytes(b"not sqlite")
            report = metrics_snapshot(path)
            self.assertEqual(report["state"], "unreadable")

    def test_backup_lock_rejects_live_and_recovers_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "backup.lock"
            with BackupJobLock(lock_path, owner="one"), self.assertRaises(BackupBusy), BackupJobLock(lock_path, owner="two"):
                pass
            lock_path.write_text(
                json.dumps({"owner": "dead", "pid": 99999999, "process_identity": ""}), encoding="utf-8"
            )
            with BackupJobLock(lock_path, owner="three"):
                self.assertTrue(lock_path.exists())
            self.assertFalse(lock_path.exists())

    def test_scheduled_backup_inventory_replication_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            self._state(state)
            backups = root / "backups"
            artifact, receipt = scheduled_backup(state, backups, receipt_path=root / "backup-receipt.json")
            self.assertEqual(receipt["state"], "verified")
            valid, invalid = inventory_backups(backups)
            self.assertEqual(len(valid), 1)
            self.assertEqual(invalid, [])
            plan = build_retention_plan(backups)
            self.assertIn(artifact.database, plan.keep)
            self.assertEqual(plan.delete, ())
            replication = write_replication_receipt(
                artifact,
                root / "replication.json",
                destination="nas://backup",
                transport_receipt="",
                confirmed=True,
            )
            payload = json.loads(replication.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "pending_external_confirmation")

    def test_retention_does_not_delete_invalid_or_newest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            self._state(state)
            backups = root / "backups"
            first = create_backup(state, backups)
            invalid = backups / "not-a-backup.db"
            invalid.write_bytes(b"invalid")
            plan = build_retention_plan(backups, daily=1, weekly=1, monthly=1)
            self.assertIn(first.database, plan.keep)
            self.assertIn(invalid.resolve(), plan.invalid)
            result = apply_retention_plan(plan, directory=backups)
            self.assertEqual(result["deleted"], [])
            self.assertTrue(invalid.exists())

    def test_prune_preview_and_apply_preserves_unresolved_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            self._state(path)
            with StateStore(path) as store:
                store.connection.execute("UPDATE article_history SET sent_at='2020-01-01T00:00:00+00:00'")
                store.connection.commit()
            preview = prune_state_history(path, now=datetime(2026, 9, 6, tzinfo=UTC), apply=False)
            self.assertGreaterEqual(preview["counts"]["article_history"], 1)
            applied = prune_state_history(path, now=datetime(2026, 9, 6, tzinfo=UTC), apply=True)
            self.assertEqual(applied["counts"], preview["counts"])
            with StateStore(path, readonly=True) as store:
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM article_history").fetchone()[0], 0)

    def test_retention_plan_is_previewable_and_reverification_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            self._state(state)
            backups = root / "backups"
            artifacts = [create_backup(state, backups) for _ in range(3)]
            for index, artifact in enumerate(artifacts):
                payload = json.loads(artifact.manifest.read_text(encoding="utf-8"))
                payload["created_at"] = f"202{index + 4}-01-01T00:00:00+00:00"
                artifact.manifest.write_text(json.dumps(payload), encoding="utf-8")
            plan = build_retention_plan(backups, daily=1, weekly=1, monthly=1, referenced={artifacts[0].database.name})
            self.assertIn(artifacts[0].database, plan.keep)
            self.assertTrue(plan.delete)
            self.assertEqual(plan.as_dict()["latest_verified"], str(plan.latest_verified))
            result = apply_retention_plan(plan, directory=backups)
            self.assertEqual(len(result["deleted"]), len(plan.delete))
            self.assertTrue(all(not path.exists() for path in plan.delete))

            outside = root / "outside.db"
            outside.write_bytes(b"outside")
            malicious = RetentionPlan((), (outside,), (), None, ())
            with self.assertRaises(StateError):
                apply_retention_plan(malicious, directory=backups)

    def test_retention_rechecks_tampered_artifact_and_lock_edges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            self._state(state)
            backups = root / "backups"
            first = create_backup(state, backups)
            second = create_backup(state, backups)
            payload = json.loads(second.manifest.read_text(encoding="utf-8"))
            payload["created_at"] = "2020-01-01T00:00:00+00:00"
            second.manifest.write_text(json.dumps(payload), encoding="utf-8")
            plan = build_retention_plan(backups, daily=1, weekly=1, monthly=1, referenced={first.database.name})
            self.assertIn(second.database, plan.delete)
            second.database.write_bytes(b"tampered")
            with self.assertRaises(StateError):
                apply_retention_plan(plan, directory=backups)

            directory_lock = root / "lock-directory"
            directory_lock.mkdir()
            with self.assertRaises(BackupBusy):
                BackupJobLock(directory_lock).__enter__()
            stale = root / "stale.lock"
            stale.write_text("not json", encoding="utf-8")
            with BackupJobLock(stale, owner="recovered"):
                self.assertTrue(stale.exists())
            self.assertFalse(stale.exists())

    def test_replication_receipts_and_operation_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            self._state(state)
            artifact = create_backup(state, root / "backups")
            with self.assertRaises(ValueError):
                write_replication_receipt(artifact, root / "empty.json", destination=" ")
            confirmed = write_replication_receipt(
                artifact,
                root / "confirmed.json",
                destination="nas://backup",
                transport_receipt="remote-1",
                confirmed=True,
            )
            self.assertEqual(json.loads(confirmed.read_text(encoding="utf-8"))["state"], "confirmed")
            artifact.database.write_bytes(b"changed")
            with self.assertRaises(StateError):
                write_replication_receipt(artifact, root / "changed.json", destination="nas://backup")
            with self.assertRaises(ValueError):
                build_retention_plan(root / "backups", daily=0)
            with self.assertRaises(ValueError):
                prune_state_history(state, attempt_retention_days=0)

    def test_operation_helper_edges_and_unverified_inventory(self) -> None:
        self.assertEqual(operations._calendar_key(datetime(2026, 9, 6, tzinfo=UTC), "day"), "2026-09-06")
        self.assertEqual(operations._calendar_key(datetime(2026, 9, 6, tzinfo=UTC), "week"), "2026-W36")
        self.assertEqual(operations._calendar_key(datetime(2026, 9, 6, tzinfo=UTC), "month"), "2026-09")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(inventory_backups(root / "missing"), ([], []))
            invalid = root / "backups"
            invalid.mkdir()
            (invalid / "bad.db").write_bytes(b"not-sqlite")
            valid, rejected = inventory_backups(invalid)
            self.assertEqual(valid, [])
            self.assertEqual(rejected, [(invalid / "bad.db").resolve()])
            empty_plan = build_retention_plan(invalid, referenced={root / "outside.db"})
            self.assertIsNone(empty_plan.latest_verified)
            self.assertEqual(empty_plan.referenced, ())
            with self.assertRaises(StateError):
                apply_retention_plan(RetentionPlan((), (invalid / "wrong.txt",), (), None, ()), directory=invalid)

            receipt = root / "receipt.json"
            operations._atomic_json(receipt, {"state": "ok"})
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["state"], "ok")
            lock = root / "lock.json"
            lock.write_text(
                json.dumps({"owner": "live", "pid": os.getpid(), "process_identity": operations._process_identity(os.getpid())}),
                encoding="utf-8",
            )
            with self.assertRaises(BackupBusy):
                BackupJobLock(lock, owner="contender").__enter__()
            foreign = BackupJobLock(lock, owner="foreign")
            foreign._held = True
            foreign.__exit__(None, None, None)
            self.assertTrue(lock.exists())

    def test_scheduled_backup_without_receipt_and_lock_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            self._state(state)
            artifact, receipt = scheduled_backup(state, root / "backups")
            self.assertNotIn("receipt_file", receipt)
            self.assertTrue(artifact.database.exists())
            lock = root / "write-failure.lock"
            with patch.object(operations.os, "fdopen", side_effect=OSError("cannot write lock")), self.assertRaises(OSError):
                BackupJobLock(lock).__enter__()
            self.assertFalse(lock.exists())


class AlertSinkTests(unittest.TestCase):
    def _record(self, state: str = "firing", key: str = "health:state_missing") -> AlertRecord:
        return AlertRecord(
            alert_id="meco.health.state_missing",
            severity="critical",
            threshold="missing",
            first_seen_at="2026-09-06T00:00:00+00:00",
            state=state,
            dedup_key=key,
            timestamp="2026-09-06T00:00:00+00:00",
            receipt_id="receipt-1" if state == "firing" else "receipt-2",
            details={"token": "secret"},
        )

    def test_memory_sink_deduplicates_and_recovers(self) -> None:
        sink = MemoryAlertSink()
        first = sink.publish(self._record())
        second = sink.publish(self._record())
        self.assertEqual(first.receipt_id, second.receipt_id)
        self.assertNotIn("secret", json.dumps(first.as_dict()))
        recovered = sink.reconcile(set(), now=datetime(2026, 9, 7, tzinfo=UTC))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].state, "recovery")

    def test_jsonl_sink_deduplicates_redacts_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.jsonl"
            sink = JsonlAlertSink(path)
            first = sink.publish(self._record())
            again = JsonlAlertSink(path).publish(self._record())
            self.assertEqual(first.receipt_id, again.receipt_id)
            recovery = JsonlAlertSink(path).reconcile(set(), now=datetime(2026, 9, 7, tzinfo=UTC))
            self.assertEqual(recovery[0].state, "recovery")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("secret", text)

    def test_health_and_backup_evaluators_publish_stable_rules(self) -> None:
        sink = MemoryAlertSink()
        report = {"healthy": False, "reasons": ["state_missing", "unknown_reason"]}
        records = evaluate_health(report, sink, now=datetime(2026, 9, 6, tzinfo=UTC))
        self.assertEqual({record.state for record in records}, {"firing"})
        self.assertTrue(any(record.alert_id == "meco.health.state_missing" for record in records))
        self.assertTrue(evaluate_backup(success=False, sink=sink, detail="failed", now=datetime(2026, 9, 6, tzinfo=UTC)))
        recoveries = evaluate_health({"healthy": True, "reasons": []}, sink, now=datetime(2026, 9, 7, tzinfo=UTC))
        self.assertTrue(any(record.state == "recovery" for record in recoveries))
        self.assertFalse(any(record.dedup_key == "backup:verification" for record in recoveries))
        backup_recovery = evaluate_backup(success=True, sink=sink, now=datetime(2026, 9, 7, tzinfo=UTC))
        self.assertEqual([record.dedup_key for record in backup_recovery], ["backup:verification"])


if __name__ == "__main__":
    unittest.main()
