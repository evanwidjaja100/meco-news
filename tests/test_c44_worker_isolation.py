"""C4.4 worker-isolation proof: real spawned processes terminate and reap."""

from __future__ import annotations

import contextlib
import multiprocessing as mp
import pickle
import time
import unittest

from meco_news import collectors
from meco_news.collectors import SourceResult

_FRAME_BYTES = 65536


def _c44_fast_worker() -> SourceResult:
    return SourceResult("c44-fast", "C44 Fast", "succeeded", accepted_count=0)


def _c44_hang_worker() -> SourceResult:
    time.sleep(60)
    return SourceResult("c44-hang", "C44 Hang", "succeeded")


class RealWorkerIsolationTests(unittest.TestCase):
    def test_spawned_fast_worker_returns_typed_result_and_exits(self) -> None:
        context = mp.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=collectors._source_process_entry,
            args=(child, _c44_fast_worker, (), {}, _FRAME_BYTES),
            name="meco-test-c44-fast",
        )
        try:
            process.start()
            child.close()
            self.assertTrue(parent.poll(20), "fast worker produced no IPC frame")
            payload = pickle.loads(parent.recv_bytes())
            self.assertEqual(payload[0], "result")
            self.assertIsInstance(payload[1], SourceResult)
            self.assertEqual(payload[1].outcome, "succeeded")
            process.join(20)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            collectors._terminate_worker(process)
            self.assertFalse(process.is_alive())
        finally:
            with contextlib.suppress(Exception):
                child.close()
            with contextlib.suppress(Exception):
                parent.close()
            if process.is_alive():
                collectors._terminate_worker(process)

    def test_spawned_hang_worker_terminates_reaps_and_next_run_succeeds(self) -> None:
        context = mp.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=collectors._source_process_entry,
            args=(child, _c44_hang_worker, (), {}, _FRAME_BYTES),
            name="meco-test-c44-hang",
        )
        try:
            process.start()
            child.close()
            self.assertFalse(parent.poll(1), "hung worker unexpectedly produced a frame")
            self.assertTrue(process.is_alive(), "hung worker exited before termination")
            collectors._terminate_worker(process)
            self.assertFalse(process.is_alive())
            self.assertIsNotNone(process.exitcode)
            survivors = [p for p in mp.active_children() if p.name.startswith("meco-test-c44")]
            self.assertEqual(survivors, [])
        finally:
            with contextlib.suppress(Exception):
                child.close()
            with contextlib.suppress(Exception):
                parent.close()
            if process.is_alive():
                collectors._terminate_worker(process)
        survivors = [p for p in mp.active_children() if p.name.startswith("meco-test-c44")]
        self.assertEqual(survivors, [])
        parent2, child2 = context.Pipe(duplex=False)
        nxt = context.Process(
            target=collectors._source_process_entry,
            args=(child2, _c44_fast_worker, (), {}, _FRAME_BYTES),
            name="meco-test-c44-next",
        )
        try:
            nxt.start()
            child2.close()
            self.assertTrue(parent2.poll(20), "next run after hang produced no frame")
            payload = pickle.loads(parent2.recv_bytes())
            self.assertEqual(payload[0], "result")
            nxt.join(20)
            self.assertFalse(nxt.is_alive())
            self.assertEqual(nxt.exitcode, 0)
        finally:
            with contextlib.suppress(Exception):
                child2.close()
            with contextlib.suppress(Exception):
                parent2.close()
            if nxt.is_alive():
                collectors._terminate_worker(nxt)


if __name__ == "__main__":
    unittest.main()
