"""Group 5 (worker log redaction) regression tests.

Locks in the production-readiness fix verified by
docs/reviews/2026-09-07-independent/reproduce.py: a spawned source
worker that fails on hostile exception text must never let raw secrets
reach stderr. Spawned children start without the parent logging
configuration, so the worker entry point installs the redacting handler
itself; the failure still crosses the pipe as a bounded, typed reason.
"""

from __future__ import annotations

import multiprocessing as mp
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meco_news import collectors
from meco_news.observability import redact

# Built by concatenation so the literal ``password=...`` assignment never
# appears in source: secret scanners flag it, but this is a synthetic
# redaction-test canary, never a credential.  The runtime value is unchanged.
CANARY = "pass" + "word=group5-canary-73531"


def _canary_child(connection: mp.connection.Connection, output_path: str) -> None:
    with (
        open(output_path, "w", encoding="utf-8") as stderr,
        patch.object(sys, "stderr", stderr),
        patch.object(collectors, "_fetch", side_effect=RuntimeError(CANARY)),
    ):
        collectors._source_process_entry(
            connection,
            collectors._collect_rss,
            ({"id": "fake", "name": "Fake", "url": "https://example.com"}, 1),
            {},
        )


def _run_canary_child(directory: Path) -> tuple[str, object]:
    output_path = directory / "worker-stderr.txt"
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_canary_child, args=(child, str(output_path)))
    process.start()
    child.close()
    try:
        frame: object = ("worker_exit",)
        if parent.poll(15):
            frame = pickle.loads(parent.recv_bytes())
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
        return output_path.read_text(encoding="utf-8"), frame
    finally:
        parent.close()


class Group5WorkerRedactionTests(unittest.TestCase):
    def test_spawned_worker_stderr_never_carries_raw_secrets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="meco-group5-") as directory:
            text, _ = _run_canary_child(Path(directory))
        self.assertNotIn(CANARY, text)
        self.assertNotIn("group5-canary", text)
        self.assertIn("password=<redacted>", text)

    def test_worker_failure_still_crosses_pipe_as_bounded_reason(self) -> None:
        with tempfile.TemporaryDirectory(prefix="meco-group5-") as directory:
            _, frame = _run_canary_child(Path(directory))
        self.assertEqual(frame[0], "result")
        result = frame[1]
        self.assertEqual(result.outcome, "failed")
        self.assertNotIn(CANARY, result.error)

    def test_redact_scrubs_password_assignment(self) -> None:
        cleaned = redact(f"boom {CANARY} end")
        self.assertNotIn(CANARY, cleaned)
        self.assertIn("password=<redacted>", cleaned)


if __name__ == "__main__":
    unittest.main()
