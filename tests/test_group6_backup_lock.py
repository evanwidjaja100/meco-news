"""Group 6 (backup lock exclusivity) regression tests.

Locks in the production-readiness fix verified by
docs/reviews/2026-09-07-independent/reproduce.py: stale-marker recovery
in BackupJobLock was a check-then-act unlink, so a contender that
published after our inspection lost its marker and both contenders ended
up holding the lock. Recovery is now compare-and-swap on the exact
inspected bytes: anything changed underneath us is a refusal, never a
second holder.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meco_news import operations
from meco_news.operations import BackupBusy, BackupJobLock


class Group6BackupLockTests(unittest.TestCase):
    def test_race_after_stale_read_leaves_exactly_one_holder(self) -> None:
        with tempfile.TemporaryDirectory(prefix="meco-group6-") as directory:
            path = Path(directory) / "backup.lock"
            path.write_text(json.dumps({"pid": 0, "owner": "dead"}), encoding="utf-8")
            first = BackupJobLock(path, owner="first")
            second = BackupJobLock(path, owner="second")
            real_alive = operations._pid_alive

            def race_after_stale_read(*_args: object) -> bool:
                with patch.object(operations, "_pid_alive", real_alive):
                    second.__enter__()
                return False

            try:
                with (
                    patch.object(operations, "_pid_alive", side_effect=race_after_stale_read),
                    self.assertRaises(BackupBusy),
                ):
                    first.__enter__()
                self.assertFalse(first._held)
                self.assertTrue(second._held)
                marker = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(marker["owner"], "second")
            finally:
                first.__exit__(None, None, None)
                second.__exit__(None, None, None)
            self.assertFalse(path.exists())

    def test_uncontended_stale_recovery_still_works(self) -> None:
        with tempfile.TemporaryDirectory(prefix="meco-group6-") as directory:
            path = Path(directory) / "stale.lock"
            path.write_text(json.dumps({"pid": 0, "owner": "dead"}), encoding="utf-8")
            with BackupJobLock(path, owner="recovered"):
                marker = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(marker["owner"], "recovered")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
