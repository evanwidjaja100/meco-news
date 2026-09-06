"""C2.2a shared/exclusive guard + fence tests (closure plan C2.2)."""
from __future__ import annotations
import json
import tempfile
from pathlib import Path
import unittest
from meco_news.maintenance import (
    MaintenanceBusy,
    MaintenanceContext,
    MaintenanceError,
    RuntimeContext,
    assert_maintenance_fence,
    maintenance_fence,
)
from meco_news.storage import StateStore


def _fresh_db(path: Path) -> Path:
    with StateStore(path):
        pass
    return path


class TestSharedExclusiveGuard(unittest.TestCase):
    def test_runtime_acquire_fails_when_exclusive_held(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="maint-1"), self.assertRaises(MaintenanceBusy):
                RuntimeContext.acquire(path, owner="runtime-1")

    def test_maintenance_acquire_fails_when_runtime_held(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with RuntimeContext.acquire(path, owner="runtime-1"), self.assertRaises(MaintenanceBusy):
                MaintenanceContext.acquire(path, owner="maint-1")

    def test_multiple_runtimes_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with (
                RuntimeContext.acquire(path, owner="r1"),
                RuntimeContext.acquire(path, owner="r2"),
                self.assertRaises(MaintenanceBusy),
            ):
                MaintenanceContext.acquire(path, owner="maint-1")

    def test_runtime_release_unblocks_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            ctx = RuntimeContext.acquire(path, owner="runtime-1")
            try:
                with self.assertRaises(MaintenanceBusy):
                    MaintenanceContext.acquire(path, owner="maint-1")
            finally:
                ctx.release()
            with MaintenanceContext.acquire(path, owner="maint-1") as mctx:
                self.assertTrue(mctx.live)

    def test_exclusive_blocks_writable_open(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="maint-1"), self.assertRaises(MaintenanceBusy):
                StateStore(path)

    def test_exclusive_does_not_block_readonly_open(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="maint-1"), StateStore(path, readonly=True) as store:
                self.assertGreaterEqual(store.schema_version, 3)

    def test_stale_exclusive_takeover_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with (
                MaintenanceContext.acquire(path, owner="crashed", ttl_seconds=-1),
                MaintenanceContext.acquire(path, owner="op-2") as ctx2,
            ):
                self.assertTrue(ctx2.live)

    def test_dead_pid_runtime_does_not_block_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with RuntimeContext.acquire(path, owner="crashed-runtime") as rctx:
                self.assertTrue(rctx.live)
                files = list((Path(str(path) + ".runtime.d")).glob("*.json"))
                self.assertEqual(len(files), 1)
                data = json.loads(files[0].read_text(encoding="utf-8"))
                data["pid"] = 999999
                files[0].write_text(json.dumps(data), encoding="utf-8")
                self.assertFalse(rctx.live)
                with MaintenanceContext.acquire(path, owner="maint-1") as mctx:
                    self.assertTrue(mctx.live)

    def test_stale_runtime_does_not_block_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with (
                RuntimeContext.acquire(path, owner="old-runtime", ttl_seconds=-1),
                MaintenanceContext.acquire(path, owner="maint-1") as mctx,
            ):
                self.assertTrue(mctx.live)


class TestFenceApi(unittest.TestCase):
    def test_fence_requires_live_context(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="m1") as ctx:
                fence = maintenance_fence(ctx)
                self.assertTrue(str(fence))
            with self.assertRaises(MaintenanceError):
                assert_maintenance_fence(ctx, db_path=path)

    def test_fence_rejects_wrong_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            first = _fresh_db(Path(d) / "first.db")
            second = _fresh_db(Path(d) / "second.db")
            with MaintenanceContext.acquire(first, owner="m1") as ctx, self.assertRaises(MaintenanceError):
                assert_maintenance_fence(ctx, db_path=second)

    def test_fence_rejects_wrong_scope(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="m1", scope="restore") as ctx, self.assertRaises(MaintenanceError):
                assert_maintenance_fence(ctx, db_path=path, scope="maintenance")

    def test_fence_accepts_live_context(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _fresh_db(Path(d) / "state.db")
            with MaintenanceContext.acquire(path, owner="m1") as ctx:
                token = assert_maintenance_fence(ctx, db_path=path)
                self.assertEqual(token, maintenance_fence(ctx))


if __name__ == "__main__":
    unittest.main()