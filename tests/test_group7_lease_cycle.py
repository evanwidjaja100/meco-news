"""Group 7 (lease-vs-cycle validation) regression tests.

Locks in the production-readiness fix verified by
docs/reviews/2026-09-07-independent/reproduce.py: a lease_ttl_seconds
shorter than the collection cycle budget was accepted, so the delivery
lease expired mid-collection (collection blocks with no heartbeat until
the delivery phase) and stranded the delivery in collecting with
failed_terminal. The lease must cover the whole cycle deadline plus a
margin, enforced fail-fast at config load.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meco_news.config import ConfigurationError, load_config

ROOT = Path(__file__).resolve().parents[1]


def _config_with(*, lease: int, cycle: int) -> Path:
    raw = json.loads((ROOT / "config" / "watchlist.json").read_text(encoding="utf-8"))
    raw["lease_ttl_seconds"] = lease
    raw["limits"]["cycle_deadline_seconds"] = cycle
    directory = tempfile.mkdtemp(prefix="meco-group7-")
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


class Group7LeaseCycleTests(unittest.TestCase):
    def test_short_lease_for_cycle_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_config(_config_with(lease=65, cycle=120))

    def test_lease_needs_thirty_second_margin_over_cycle(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_config(_config_with(lease=149, cycle=120))
        config = load_config(_config_with(lease=150, cycle=120))
        self.assertEqual(config.lease_ttl_seconds, 150)

    def test_shipped_config_still_loads(self) -> None:
        config = load_config(ROOT / "config" / "watchlist.json")
        self.assertGreaterEqual(config.lease_ttl_seconds, config.limits.cycle_deadline_seconds + 30)


if __name__ == "__main__":
    unittest.main()
