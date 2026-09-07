"""Group 4 (frozen-input exit-2) regression tests.

Locks in the production-readiness fix verified by
docs/reviews/2026-09-07-independent/reproduce.py: malformed frozen
input must fail with exit code 2 before state or network
initialization, with a clean machine-readable error and no traceback.
It must never surface as an unhandled exception (exit 1), create a log
file, or touch the state database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = str(ROOT / "config" / "watchlist.json")
VALID_FROZEN = str(ROOT / "tests" / "fixtures" / "frozen-empty-v1.json")
ENV = {"TELEGRAM_BOT_TOKEN": "123456:synthetic-test-token", "TELEGRAM_CHAT_ID": "12345"}


def _run_dry_run(frozen: Path, log: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "meco_news", "--config", CONFIG, "--dry-run", "--frozen-input", str(frozen), *extra],
        cwd=str(ROOT),
        env={**os.environ, **ENV, "PYTHONPATH": str(ROOT), "STATE_DB": str(log.parent / "unused.db"), "LOG_FILE": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )


class Group4FrozenInputTests(unittest.TestCase):
    def test_malformed_frozen_input_json_mode_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = root / "invalid.json"
            frozen.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            log = root / "invalid-input.log"
            process = _run_dry_run(frozen, log, "--json", "--log-file", str(log))
            self.assertEqual(process.returncode, 2, f"stderr: {process.stderr[-500:]}")
            payload: dict[str, Any] = json.loads(process.stdout)
            self.assertEqual(payload["code"], 2)
            self.assertEqual(payload["error_class"], "ConfigurationError")
            self.assertNotIn("Traceback", process.stderr)
            self.assertFalse(log.exists(), "invalid input must fail before log creation")
            self.assertFalse((root / "unused.db").exists(), "dry-run must never touch state")

    def test_malformed_frozen_input_human_mode_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = root / "invalid.json"
            frozen.write_text("not json at all", encoding="utf-8")
            log = root / "invalid-input.log"
            process = _run_dry_run(frozen, log, "--log-file", str(log))
            self.assertEqual(process.returncode, 2, f"stderr: {process.stderr[-500:]}")
            self.assertNotIn("Traceback", process.stderr)
            self.assertIn("frozen input", process.stderr.lower())
            self.assertFalse(log.exists())

    def test_valid_frozen_input_still_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "valid-input.log"
            process = _run_dry_run(Path(VALID_FROZEN), log, "--json")
            self.assertEqual(process.returncode, 0, f"stderr: {process.stderr[-500:]}")
            payload: dict[str, Any] = json.loads(process.stdout)
            self.assertEqual(payload["code"], 0)
            self.assertEqual(payload["outcome"], "dry_run")


if __name__ == "__main__":
    unittest.main()
