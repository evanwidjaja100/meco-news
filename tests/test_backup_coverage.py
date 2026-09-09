from __future__ import annotations

from datetime import datetime, UTC
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import meco_news.backup as backup
from meco_news.backup import create_backup, restore_backup
from meco_news.models import NewsItem
from meco_news.storage import StateError, StateStore


def _item() -> NewsItem:
    return NewsItem(
        title="LPG tank project",
        url="https://example.com/lpg",
        source="Example",
        published_at=datetime.now(UTC),
        topic="lpg_energy",
        topic_label="LPG",
        relevance_reason="Demand",
        score=12,
    )


def _complete_via_outbox(store: StateStore, delivery_date: str, item: NewsItem, *, owner: str = "owner") -> None:
    delivery = store.active_delivery(delivery_date)
    if delivery is None:
        delivery = store.create_delivery(delivery_date, owner_id=owner)
    store.prepare_delivery(
        delivery.delivery_id,
        [item],
        ["<b>fixture delivery</b>"],
        owner_id=owner,
        item_chunk_indexes={item.fingerprint: 0},
    )
    chunk = store.due_chunks(delivery.delivery_id)[0]
    store.begin_chunk_attempt(chunk.chunk_id, run_id=f"{owner}-run", owner_id=owner)
    store.finish_chunk(chunk.chunk_id, "accepted", run_id=f"{owner}-run", owner_id=owner, telegram_message_id="1")


class BackupCoverageTests(unittest.TestCase):
    def test_filename_manifest_and_path_helpers(self) -> None:
        self.assertTrue(backup._safe_filename("state.db"))
        for value in ("", ".", "..", "a/b", "a\\b", "a\x00b", 1):
            self.assertFalse(backup._safe_filename(value))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            explicit = root / "one.db"
            self.assertEqual(backup._new_backup_path(explicit), explicit)
            explicit.touch()
            with self.assertRaises(FileExistsError):
                backup._new_backup_path(explicit)
            folder = root / "backups"
            reserved = backup._new_backup_path(folder)
            self.assertEqual(reserved.parent, folder)
            with patch("meco_news.backup.uuid.uuid4", return_value=__import__("uuid").UUID("00000000-0000-0000-0000-000000000000")):
                reserved_name = backup._new_backup_path(folder)
                self.assertNotEqual(reserved_name, reserved)

    def test_create_backup_requires_source_and_publishes_exact_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.db"
            with self.assertRaises(FileNotFoundError):
                create_backup(missing, root / "backups")
            state = root / "state.db"
            with StateStore(state) as store:
                store.acquire_lease("delivery", "owner", 180)
                store.create_delivery("2026-09-06", owner_id="owner")
                store.release_lease("delivery", "owner")
            artifact = create_backup(state, root / "backups", config_hash="config")
            manifest = json.loads(artifact.manifest.read_text(encoding="utf-8"))
            self.assertEqual(set(manifest), backup._BACKUP_MANIFEST_FIELDS)
            self.assertEqual(manifest["sha256"], backup._sha256(artifact.database))
            explicit = root / "explicit.sqlite"
            second = create_backup(state, explicit)
            self.assertEqual(second.database, explicit.resolve())
            with self.assertRaises(FileExistsError):
                create_backup(state, explicit)

    def test_manifest_rejection_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            with StateStore(state) as store:
                store.acquire_lease("delivery", "owner", 180)
                store.create_delivery("2026-09-06", owner_id="owner")
                store.release_lease("delivery", "owner")
            artifact = create_backup(state, root / "backups")
            original = json.loads(artifact.manifest.read_text(encoding="utf-8"))
            for mutation in (
                lambda value: value.pop("sha256"),
                lambda value: value.update({"unsupported": 1}),
                lambda value: value.update({"database": "other.db"}),
                lambda value: value.update({"backup_id": "bad"}),
                lambda value: value.update({"sha256": "0" * 64}),
                lambda value: value.update({"integrity": "bad"}),
                lambda value: value.update({"schema_version": 999}),
                lambda value: value.update({"created_at": "no-date"}),
            ):
                changed = dict(original)
                mutation(changed)
                artifact.manifest.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises((StateError, FileNotFoundError)):
                    backup._load_manifest(artifact.manifest, artifact.database)
            artifact.manifest.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(backup._load_manifest(artifact.manifest, artifact.database)["backup_id"], original["backup_id"])
            with self.assertRaises(FileNotFoundError):
                backup._load_manifest(artifact.manifest, root / "missing.db")

    def test_target_recovery_and_lease_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(backup._target_recovery_state(root / "missing.db"), ([], []))
            empty = root / "empty.db"
            empty.touch()
            self.assertEqual(backup._target_recovery_state(empty), ([], []))
            bad = root / "bad.db"
            bad.write_bytes(b"bad")
            with self.assertRaises(StateError):
                backup._target_recovery_state(bad)
            self.assertEqual(backup._active_leases([]), [])
            self.assertEqual(backup._active_leases([{"scope": "delivery", "expires_at": "2000-01-01T00:00:00+00:00"}]), [])
            with self.assertRaises(StateError):
                backup._active_leases([{"scope": "delivery", "expires_at": "bad"}])

    def test_restore_rejects_same_source_and_active_or_unresolved_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            with StateStore(source) as store:
                store.acquire_lease("delivery", "owner", 180)
                store.create_delivery("2026-09-06", owner_id="owner")
                store.release_lease("delivery", "owner")
            artifact = create_backup(source, root / "backups")
            with self.assertRaises(StateError):
                restore_backup(artifact.database, artifact.database)
            target = root / "target.db"
            with StateStore(target) as store:
                store.acquire_lease("delivery", "active", 180)
                store.create_delivery("2026-09-06", owner_id="active")
                with self.assertRaises(StateError):
                    restore_backup(artifact.database, target)
                store.release_lease("delivery", "active")

    def test_history_reconciliation_preserves_post_backup_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            item = _item()
            with StateStore(source) as store:
                store.acquire_lease("delivery", "owner", 180)
                store.create_delivery("2026-09-06", owner_id="owner")
                _complete_via_outbox(store, "2026-09-06", item)
                store.release_lease("delivery", "owner")
            artifact = create_backup(source, root / "backups")
            target = root / "target.db"
            with StateStore(target) as store:
                store.acquire_lease("delivery", "owner", 180)
                other = store.create_delivery("2026-09-07", owner_id="owner")
                _complete_via_outbox(store, "2026-09-07", _item())
                store.release_lease("delivery", "owner")
            restore_backup(artifact.database, target)
            with StateStore(target, readonly=True) as store:
                self.assertGreaterEqual(store.connection.execute("SELECT COUNT(*) FROM article_history").fetchone()[0], 1)
                self.assertTrue(store.already_completed("2026-09-06"))
            self.assertEqual(other.generation, 0)

    def test_restore_moves_sqlite_sidecars_and_quarantines_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            with StateStore(source) as store:
                store.acquire_lease("delivery", "owner", 180)
                _complete_via_outbox(store, "2026-09-06", _item())
                store.release_lease("delivery", "owner")
            artifact = create_backup(source, root / "backups")

            target = root / "target.db"
            with StateStore(target):
                pass
            raw = sqlite3.connect(target)
            try:
                raw.execute("PRAGMA journal_mode=WAL")
                raw.execute("PRAGMA wal_autocheckpoint=100000")
                raw.execute("CREATE TABLE sidecar_sentinel(value TEXT)")
                raw.execute("INSERT INTO sidecar_sentinel VALUES ('old')")
                raw.commit()
                for suffix in ("-wal", "-shm"):
                    shutil.copyfile(f"{target}{suffix}", f"{target}{suffix}.saved")
            finally:
                raw.close()
            for suffix in ("-wal", "-shm"):
                shutil.copyfile(f"{target}{suffix}.saved", f"{target}{suffix}")

            restore_backup(artifact.database, target)
            previous = target.with_suffix(target.suffix + ".pre-restore.bak")
            self.assertTrue(previous.exists())
            self.assertTrue(all(Path(f"{previous}{suffix}").exists() for suffix in ("-wal", "-shm")))
            self.assertFalse(any(Path(f"{target}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")))
            with StateStore(target, readonly=True) as store:
                self.assertTrue(store.already_completed("2026-09-06"))

            orphan_target = root / "orphan.db"
            Path(f"{orphan_target}-wal").write_bytes(b"orphan wal")
            Path(f"{orphan_target}-shm").write_bytes(b"orphan shm")
            restore_backup(artifact.database, orphan_target)
            self.assertTrue(orphan_target.exists())
            self.assertFalse(Path(f"{orphan_target}-wal").exists())
            self.assertFalse(Path(f"{orphan_target}-shm").exists())
            self.assertTrue(list(root.glob("orphan.db.pre-restore-orphan-*")))

    def test_backup_publication_and_manifest_failure_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reserved = root / "reserved.db"
            self.assertTrue(backup._reserve_file(reserved))
            self.assertFalse(backup._reserve_file(reserved))
            with patch.object(backup.os, "open", side_effect=FileExistsError):
                self.assertFalse(backup._reserve_file(root / "already.db"))

            explicit = root / "explicit.db"
            explicit.with_suffix(".db.manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                backup._new_backup_path(explicit)
            output = root / "backups"
            with patch.object(backup, "_reserve_file", side_effect=[False, False, False, False, False]), self.assertRaises(StateError):
                backup._new_backup_path(output)

            manifest = root / "manifest.json"
            with patch.object(backup.os, "replace", side_effect=OSError("publish failed")), self.assertRaises(OSError):
                backup._write_manifest_atomic(manifest, {"x": 1})

            state = root / "state.db"
            with StateStore(state) as store:
                store.acquire_lease("delivery", "owner", 180)
                store.create_delivery("2026-09-06", owner_id="owner")
                store.release_lease("delivery", "owner")
            with patch.object(backup, "_reserve_file", side_effect=[True, False]), self.assertRaises(FileExistsError):
                create_backup(state, root / "reserved-output.db")

    def test_manifest_target_and_restore_rollback_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            with StateStore(state) as store:
                store.acquire_lease("delivery", "owner", 180)
                store.create_delivery("2026-09-06", owner_id="owner")
                store.release_lease("delivery", "owner")
            artifact = create_backup(state, root / "backups")
            original = json.loads(artifact.manifest.read_text(encoding="utf-8"))
            invalid_fields = (
                {"source_database": "a/b.db"},
                {"source_database": 1},
                {"backup_id": "bad"},
                {"application_version": 1},
                {"created_at": "2026-09-06T00:00:00"},
                {"config_hash": 1},
            )
            for change in invalid_fields:
                changed = dict(original)
                changed.update(change)
                artifact.manifest.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(StateError):
                    backup._load_manifest(artifact.manifest, artifact.database)
            artifact.manifest.write_text(json.dumps(original), encoding="utf-8")

            incomplete = root / "incomplete.db"
            incomplete_connection = sqlite3.connect(incomplete)
            incomplete_connection.execute("CREATE TABLE only_one(value TEXT)")
            incomplete_connection.close()
            with self.assertRaises(StateError):
                backup._target_recovery_state(incomplete)
            with patch.object(backup.sqlite3, "connect", side_effect=sqlite3.DatabaseError("cannot open")), self.assertRaises(StateError):
                backup._target_recovery_state(state)

            target = root / "target.db"
            with StateStore(target):
                pass
            with patch.object(backup, "_fsync_directory", side_effect=OSError("directory sync failed")), self.assertRaises(OSError):
                restore_backup(artifact.database, target)
            with StateStore(target, readonly=True) as restored:
                self.assertEqual(restored.integrity_check(), "ok")

    def test_reconciliation_and_path_reservation_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.db"
            restored = root / "restored.db"
            self.assertEqual(backup._merge_post_backup_history(target, restored), 0)
            target.touch()
            restored.touch()
            with patch.object(backup.sqlite3, "connect", side_effect=sqlite3.DatabaseError("cannot read")), self.assertRaises(sqlite3.DatabaseError):
                backup._merge_post_backup_history(target, restored)
            with patch.object(backup, "_path_exists", return_value=True), self.assertRaises(StateError):
                backup._restore_previous_path(target)
            with patch.object(backup, "_path_exists", return_value=True), self.assertRaises(StateError):
                backup._restore_orphan_sidecar_path(target, "-wal")


if __name__ == "__main__":
    unittest.main()
