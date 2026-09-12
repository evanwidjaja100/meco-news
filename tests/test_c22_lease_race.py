"""C2.0 true-process lease race: 50 spawned processes contend for one delivery lease.

Closure plan CG2 requires a 50-process lease test; C6.1 requires repeated
true-process lease/send races. Exactly one process may win; every loser must
observe already_running (never an exception or a second winner), and the
database must stay consistent afterwards.
"""

from __future__ import annotations

import multiprocessing as mp
import tempfile
import time
import unittest
from pathlib import Path

from meco_news.storage import StateStore

_RACERS = 50
_HOLD_SECONDS = 15.0


def _racer(db_path: str, index: int, outdir: str) -> None:
    owner = f"racer-{index}"
    try:
        with StateStore(db_path) as store:
            acquired = store.acquire_lease("delivery", owner, 120)
            Path(outdir, f"{index}.txt").write_text(
                f"{'won' if acquired.acquired else 'lost:' + acquired.owner_id}",
                encoding="utf-8",
            )
            if acquired.acquired:
                time.sleep(_HOLD_SECONDS)
    except BaseException as exc:  # noqa: BLE001 - must surface as a result file
        Path(outdir, f"{index}.txt").write_text(f"error:{type(exc).__name__}:{exc}", encoding="utf-8")
        raise


class TrueProcessLeaseRaceTests(unittest.TestCase):
    def test_fifty_processes_yield_exactly_one_lease_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "race.db"
            with StateStore(db_path):
                pass
            outdir = root / "results"
            outdir.mkdir()
            context = mp.get_context("spawn")
            processes = [
                context.Process(target=_racer, args=(str(db_path), index, str(outdir)))
                for index in range(_RACERS)
            ]
            for process in processes:
                process.start()
            deadline = time.time() + 240.0
            while time.time() < deadline:
                if len(list(outdir.glob("*.txt"))) >= _RACERS:
                    break
                time.sleep(0.5)
            for process in processes:
                process.join(timeout=60.0)
            results = {}
            for index in range(_RACERS):
                marker = outdir / f"{index}.txt"
                self.assertTrue(marker.is_file(), f"racer {index} produced no result")
                results[index] = marker.read_text(encoding="utf-8")
            for process in processes:
                self.assertEqual(process.exitcode, 0, f"racer pid={process.pid} exitcode={process.exitcode}")
            winners = [index for index, text in results.items() if text == "won"]
            errors = {index: text for index, text in results.items() if text.startswith("error:")}
            self.assertEqual(errors, {}, f"racer errors: {errors}")
            self.assertEqual(len(winners), 1, f"expected exactly one winner: {results}")
            for index, text in results.items():
                if index != winners[0]:
                    self.assertTrue(
                        text.startswith("lost:racer-"), f"racer {index} saw {text!r}, not already_running"
                    )
            with StateStore(db_path, readonly=True) as store:
                self.assertEqual(store.integrity_check(), "ok")
                lease = store.lease_info("delivery")
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertEqual(lease["owner_id"], f"racer-{winners[0]}")


if __name__ == "__main__":
    unittest.main()