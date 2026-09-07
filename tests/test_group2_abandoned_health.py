"""Group 2 (honest health) regression tests.

Locks in the F05 production-readiness fix verified by
docs/reviews/2026-09-07-independent/reproduce.py: an abandoned
non-terminal delivery must fail healthcheck with a stable reason that
produces an external alert, while fresh work and live retries stay
healthy.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import patch

from meco_news.alerts import MemoryAlertSink, evaluate_health
from meco_news.config import load_config
from meco_news.models import NewsItem
from meco_news.preflight import healthcheck
from meco_news.storage import StateStore

CONFIG = load_config("config/watchlist.json")


def _item() -> NewsItem:
    return NewsItem(
        title="Industrial project",
        url="https://example.com/industrial",
        source="Fixture",
        published_at=datetime.now(UTC),
    )


def _collecting_delivery(path: Path, date: str, *, started_at: str = "") -> int:
    with StateStore(path) as store:
        store.acquire_lease("delivery", "setup", 180)
        delivery = store.create_delivery(date, owner_id="setup")
        if started_at:
            store.connection.execute(
                "UPDATE deliveries SET started_at=? WHERE delivery_id=?",
                (started_at, delivery.delivery_id),
            )
            store.connection.commit()
        store.release_lease("delivery", "setup")
    return delivery.delivery_id


def _check(path: Path):
    with patch("meco_news.preflight._disk_sufficient", return_value=True):
        return healthcheck(CONFIG, state_path=path)


class Group2AbandonedHealthTests(unittest.TestCase):
    def test_abandoned_first_delivery_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.db"
            delivery_id = _collecting_delivery(
                path, "2026-01-01", started_at="2026-01-01T00:00:00+00:00"
            )
            healthy, report = _check(path)
        self.assertFalse(healthy)
        self.assertIn("abandoned_delivery", report["reasons"])
        self.assertEqual(report["abandoned_delivery"]["delivery_id"], delivery_id)
        self.assertEqual(report["abandoned_delivery"]["state"], "collecting")

    def test_recently_started_work_stays_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.db"
            _collecting_delivery(path, datetime.now(UTC).strftime("%Y-%m-%d"))
            healthy, report = _check(path)
        self.assertTrue(healthy, f"fresh collecting work must stay healthy: {report}")
        self.assertEqual(report["reasons"], [])

    def test_retry_wait_with_attempts_left_stays_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "sender", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="sender")
                store.prepare_delivery(
                    delivery.delivery_id, [_item()], ["<b>digest</b>"], owner_id="sender"
                )
                chunk = store.due_chunks(delivery.delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="first", owner_id="sender")
                store.finish_chunk(
                    chunk.chunk_id,
                    "rejected_retryable",
                    run_id="first",
                    owner_id="sender",
                    next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
                )
                store.release_lease("delivery", "sender")
            healthy, report = _check(path)
        self.assertTrue(healthy, f"live retry with attempts left must stay healthy: {report}")
        self.assertNotIn("abandoned_delivery", report["reasons"])

    def test_process_death_before_first_success_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.db"
            stale = (datetime.now(UTC) - timedelta(days=3)).isoformat()
            _collecting_delivery(
                path, datetime.now(UTC).strftime("%Y-%m-%d"), started_at=stale
            )
            healthy, report = _check(path)
        self.assertFalse(healthy)
        self.assertIn("abandoned_delivery", report["reasons"])

    def test_old_work_behind_newer_delivery_still_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.db"
            old_id = _collecting_delivery(
                path, "2026-01-01", started_at="2026-01-01T00:00:00+00:00"
            )
            _collecting_delivery(path, datetime.now(UTC).strftime("%Y-%m-%d"))
            healthy, report = _check(path)
        self.assertFalse(healthy)
        self.assertIn("abandoned_delivery", report["reasons"])
        self.assertEqual(report["abandoned_delivery"]["delivery_id"], old_id)

    def test_abandoned_delivery_produces_firing_alert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.db"
            _collecting_delivery(
                path, "2026-01-01", started_at="2026-01-01T00:00:00+00:00"
            )
            healthy, report = _check(path)
            self.assertFalse(healthy)
            sink = MemoryAlertSink()
            records = evaluate_health(report, sink, now=datetime.now(UTC))
        firing = [record for record in records if record.state == "firing"]
        abandoned = [
            record for record in firing if record.dedup_key == "health:abandoned_delivery"
        ]
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0].severity, "critical")
        self.assertEqual(abandoned[0].threshold, "active delivery work is abandoned")


if __name__ == "__main__":
    unittest.main()
