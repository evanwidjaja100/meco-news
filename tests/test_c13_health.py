"""C1.3 health truth-table RED suite - separate status from health, no false green.

Covers closure-plan C1.3: latest_delivery (terminal-inclusive) exposed
separately from active_delivery, required status fields, unhealthy verdicts
for maintenance / terminal-attention / retry exhaustion / incompatible schema /
corrupt state / stale-invalid heartbeat / overdue (including no-history
overdue) / state-disk floor, multi-failure reason preservation, and read-only
probes (state bytes and mtime invariance).
"""

from __future__ import annotations

import contextlib
import datetime as dt_mod
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meco_news import preflight
from meco_news.config import load_config
from meco_news.maintenance import MaintenanceContext
from meco_news.migrations import CURRENT_SCHEMA_VERSION
from meco_news.preflight import MIN_FREE_BYTES, healthcheck
from meco_news.storage import StateStore


class _FrozenDateTime(dt_mod.datetime):
    """Deterministic clock for health due-time tests (patched over preflight)."""

    _frozen: dt_mod.datetime | None = None

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        frozen = cls._frozen
        assert frozen is not None, "frozen clock not set"
        if tz is None:
            return frozen.replace(tzinfo=None)
        return frozen.astimezone(tz)


@contextlib.contextmanager
def frozen_local(hour: int, minute: int = 0):
    """Freeze preflight time to 2026-09-06 hour:minute Asia/Jakarta (+07:00)."""

    tz = dt_mod.timezone(dt_mod.timedelta(hours=7))
    _FrozenDateTime._frozen = dt_mod.datetime(2026, 9, 6, hour, minute, tzinfo=tz)
    try:
        with patch.object(preflight, "datetime", _FrozenDateTime):
            yield
    finally:
        _FrozenDateTime._frozen = None


def _news_item(url: str = "https://example.com/lpg-terminal"):
    from datetime import UTC, datetime

    from meco_news.models import NewsItem

    return NewsItem(
        title="LPG terminal project",
        url=url,
        source="S",
        published_at=datetime.now(UTC),
        score=10,
        topic="lpg_energy",
    )


def _prepared_delivery(path: Path, owner: str = "owner"):
    """Create one prepared delivery with a single pending chunk; return ids."""

    with StateStore(path) as store:
        store.acquire_lease("delivery", owner, 180)
        delivery = store.create_delivery("2026-08-25", config_hash="h")
        item = _news_item()
        store.prepare_delivery(
            delivery.delivery_id,
            [item],
            ["<b>hello</b>"],
            owner_id=owner,
            item_chunk_indexes={item.fingerprint: 0},
        )
        chunk = store.due_chunks(delivery.delivery_id)[0]
        return delivery.delivery_id, chunk.chunk_id


class TestC13HealthyBaseline(unittest.TestCase):
    def test_fresh_db_before_due_is_healthy(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertTrue(healthy, f"fresh DB before delivery_time must be healthy: {report}")
        self.assertEqual(report["reasons"], [])


class TestC13NoHistoryOverdue(unittest.TestCase):
    def test_no_history_after_due_is_overdue(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            with frozen_local(12, 0):
                healthy, report = healthcheck(config, state_path=path)
        if healthy or "overdue_delivery" not in report["reasons"]:
            self.fail(
                "BUG: fresh state past delivery_time must be unhealthy overdue_delivery "
                f"(no-history-overdue, C1.3). report={report}"
            )


class TestC13DueUnknown(unittest.TestCase):
    def test_due_computation_failure_does_not_flip_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            with frozen_local(12, 0), patch(
                "meco_news.preflight.get_timezone", side_effect=KeyError("no-tz")
            ):
                healthy, report = healthcheck(config, state_path=path)
        self.assertTrue(healthy, f"due-unknown must not flip health: {report}")
        self.assertEqual(report["due"], {"known": False, "reason": "due_computation_unavailable"})


class TestC13Maintenance(unittest.TestCase):
    def test_maintenance_in_progress_fails_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            with MaintenanceContext.acquire(path, owner="test-owner"), frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        if healthy or "maintenance_in_progress" not in report["reasons"]:
            self.fail(f"BUG: health must fail during maintenance (C1.3). report={report}")


class TestC13TerminalAndAttention(unittest.TestCase):
    def test_failed_terminal_latest_fails_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _prepared_delivery(path)
            with StateStore(path) as store:
                store.connection.execute(
                    "UPDATE deliveries SET state='failed_terminal', terminal_error='boom'"
                )
                store.connection.commit()
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("unresolved_delivery_failure", report["reasons"])

    def test_needs_attention_active_fails_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _prepared_delivery(path)
            with StateStore(path) as store:
                chunk = store.due_chunks(1)[0]
                store.acquire_lease("delivery", "owner", 180)
                store.begin_chunk_attempt(chunk.chunk_id, run_id="test", owner_id="owner")
                store.finish_chunk(
                    chunk.chunk_id,
                    "ambiguous",
                    run_id="test",
                    owner_id="owner",
                    error_class="telegram_ambiguous",
                    error_text="unknown",
                )
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("unresolved_delivery_failure", report["reasons"])


class TestC13RetryExhaustion(unittest.TestCase):
    def test_exhausted_chunk_retry_fails_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _prepared_delivery(path)
            with StateStore(path) as store:
                store.connection.execute(
                    "UPDATE outbox_chunks SET state='retry_wait', attempt_count=10, "
                    "next_attempt_at='2026-09-06T12:00:00+00:00'"
                )
                store.connection.commit()
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        if healthy or "chunk_retry_exhausted" not in report["reasons"]:
            self.fail(f"BUG: exhausted chunk retry must fail health (C1.3). report={report}")

    def test_transient_retry_stays_healthy(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _prepared_delivery(path)
            with StateStore(path) as store:
                store.connection.execute(
                    "UPDATE outbox_chunks SET state='retry_wait', attempt_count=1, "
                    "next_attempt_at='2026-09-07T12:00:00+00:00'"
                )
                store.connection.commit()
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertTrue(healthy, f"transient retry with attempts left must stay healthy: {report}")


class TestC13IncompatibleSchema(unittest.TestCase):
    def test_newer_schema_fails_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            with StateStore(path) as store:
                store.connection.execute("UPDATE schema_migrations SET version=99 WHERE version=3")
                store.connection.commit()
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        if healthy or "incompatible_schema" not in report["reasons"]:
            self.fail(f"BUG: newer-than-app schema must fail health (C1.3). report={report}")


class TestC13Heartbeats(unittest.TestCase):
    def test_stale_heartbeat_fails_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                store.connection.execute(
                    "UPDATE run_leases SET heartbeat_at='2020-01-01T00:00:00+00:00' WHERE scope='delivery'"
                )
                store.connection.commit()
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("stale_delivery_heartbeat", report["reasons"])

    def test_invalid_heartbeat_fails_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                store.connection.execute(
                    "UPDATE run_leases SET heartbeat_at='not-a-time' WHERE scope='delivery'"
                )
                store.connection.commit()
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("invalid_delivery_heartbeat", report["reasons"])


class TestC13DiskFloor(unittest.TestCase):
    def _usage(self, total: int, free: int):
        from collections import namedtuple

        Usage = namedtuple("usage", ["total", "used", "free"])
        return Usage(total, total - free, free)

    def test_disk_at_boundary_is_healthy(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            usage = self._usage(10 * MIN_FREE_BYTES, MIN_FREE_BYTES)
            with frozen_local(5, 0), patch("shutil.disk_usage", return_value=usage):
                healthy, report = healthcheck(config, state_path=path)
        self.assertTrue(healthy, f"exact disk floor must be healthy: {report}")

    def test_disk_one_byte_below_absolute_floor_fails(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            usage = self._usage(10 * MIN_FREE_BYTES, MIN_FREE_BYTES - 1)
            with frozen_local(5, 0), patch("shutil.disk_usage", return_value=usage):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("state_disk_low", report["reasons"])

    def test_disk_one_byte_below_ratio_floor_fails(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            usage = self._usage(10 * MIN_FREE_BYTES + 1, MIN_FREE_BYTES)
            with frozen_local(5, 0), patch("shutil.disk_usage", return_value=usage):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("state_disk_low", report["reasons"])


class TestC13SimultaneousFailures(unittest.TestCase):
    def test_all_stable_reasons_preserved(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _, chunk_id = _prepared_delivery(path)
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                store.connection.execute(
                    "UPDATE run_leases SET heartbeat_at='2020-01-01T00:00:00+00:00' WHERE scope='delivery'"
                )
                store.connection.commit()
                store.begin_chunk_attempt(chunk_id, run_id="test", owner_id="owner")
                store.finish_chunk(
                    chunk_id,
                    "ambiguous",
                    run_id="test",
                    owner_id="owner",
                    error_class="telegram_ambiguous",
                    error_text="unknown",
                )
                store.connection.commit()
            with MaintenanceContext.acquire(path, owner="test-owner"), frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        for reason in (
            "maintenance_in_progress",
            "stale_delivery_heartbeat",
            "unresolved_delivery_failure",
        ):
            if reason not in report["reasons"]:
                self.fail(f"BUG: simultaneous failures must preserve {reason}. report={report}")


class TestC13StatusContent(unittest.TestCase):
    def test_status_exposes_latest_active_and_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _, chunk_id = _prepared_delivery(path)
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                store.begin_chunk_attempt(chunk_id, run_id="test", owner_id="owner")
                store.finish_chunk(
                    chunk_id,
                    "ambiguous",
                    run_id="test",
                    owner_id="owner",
                    error_class="telegram_ambiguous",
                    error_text="unknown",
                )
            with StateStore(path, readonly=True) as store:
                status = store.status_snapshot()
        for key in (
            "schema_version",
            "application_version",
            "lease",
            "scheduler_lease",
            "active_delivery",
            "latest_delivery",
            "unresolved_ambiguity_count",
            "last_attempt_at",
            "last_success_at",
            "next_retry_at",
            "last_attempt",
            "active_chunk",
            "integrity",
        ):
            self.assertIn(key, status, f"status must expose {key} (C1.3)")
        self.assertEqual(status["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertTrue(status["application_version"])
        self.assertIsNotNone(status["active_delivery"])
        self.assertIsNotNone(status["latest_delivery"])
        self.assertEqual(status["active_delivery"]["delivery_id"], status["latest_delivery"]["delivery_id"])
        self.assertGreaterEqual(status["unresolved_ambiguity_count"], 1)
        self.assertIsNotNone(status["last_attempt"])
        self.assertIn("error_class", status["last_attempt"])
        self.assertIsNotNone(status["active_chunk"])
        self.assertIn("attempt_count", status["active_chunk"])
        self.assertEqual(status["integrity"], "ok")


class TestC13FullTruthTable(unittest.TestCase):
    """Close every remaining healthcheck decision branch (C1.3 exit)."""

    def test_corrupt_integrity_fails_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            with StateStore(path, readonly=True) as probe:
                bad = dict(probe.status_snapshot())
            bad["integrity"] = "database disk image is malformed"
            with frozen_local(5, 0), patch.object(preflight, "StateStore") as mock_store:
                mock_store.return_value.__enter__.return_value.status_snapshot.return_value = bad
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("db_corrupt", report["reasons"])

    def test_unwritable_parent_fails_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            with frozen_local(5, 0), patch("os.access", return_value=False):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("state_unwritable", report["reasons"])

    def test_disk_unavailable_fails_health(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            with frozen_local(5, 0), patch("shutil.disk_usage", side_effect=OSError("nope")):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("state_disk_unavailable", report["reasons"])

    def test_recent_success_is_healthy(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _prepared_delivery(path)
            with StateStore(path) as store:
                store.connection.execute(
                    "UPDATE deliveries SET state='completed_empty', "
                    "completed_at='2026-09-05T20:00:00+00:00'"
                )
                store.connection.commit()
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertTrue(healthy, f"recent completed_empty must be healthy: {report}")
        self.assertEqual(report["reasons"], [])

    def test_ancient_success_is_overdue(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _prepared_delivery(path)
            with StateStore(path) as store:
                store.connection.execute(
                    "UPDATE deliveries SET state='completed', "
                    "completed_at='2020-01-01T00:00:00+00:00'"
                )
                store.connection.commit()
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("overdue_delivery", report["reasons"])

    def test_bogus_success_timestamp_is_invalid(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _prepared_delivery(path)
            with StateStore(path) as store:
                store.connection.execute(
                    "UPDATE deliveries SET state='completed', completed_at='not-a-time'"
                )
                store.connection.commit()
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("invalid_last_success", report["reasons"])

    def test_all_sources_failed_retry_exhausted(self) -> None:
        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _prepared_delivery(path)
            with StateStore(path) as store:
                store.connection.execute(
                    "UPDATE deliveries SET state='retry_wait', "
                    "terminal_error='all_sources_failed: petromindo, antara', "
                    "next_attempt_at='2026-09-07T00:00:00+00:00'"
                )
                store.connection.commit()
            with frozen_local(5, 0):
                healthy, report = healthcheck(config, state_path=path)
        self.assertFalse(healthy)
        self.assertIn("all_sources_failed_retry_exhausted", report["reasons"])
        self.assertNotIn("chunk_retry_exhausted", report["reasons"])


class TestC13ProbesReadOnly(unittest.TestCase):
    def test_probes_leave_bytes_and_mtime_unchanged(self) -> None:
        import os

        from meco_news.preflight import run_preflight

        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _prepared_delivery(path)
            before_stat = os.stat(path)
            before_bytes = path.read_bytes()
            with StateStore(path, readonly=True) as store:
                _ = store.status_snapshot()
            with frozen_local(5, 0):
                _ = healthcheck(config, state_path=path)
                _ = run_preflight(config, state_path=path)
            after_stat = os.stat(path)
            after_bytes = path.read_bytes()
        self.assertEqual(before_bytes, after_bytes, "probes must not change state bytes (C1.3)")
        self.assertEqual(
            before_stat.st_mtime_ns, after_stat.st_mtime_ns, "probes must not change state mtime (C1.3)"
        )
        leftovers = [
            str(sibling)
            for sibling in Path(directory).glob("state.db*")
            if sibling.name != "state.db"
        ]
        self.assertEqual(leftovers, [], f"probes must not create sidecars: {leftovers}")


if __name__ == "__main__":
    unittest.main()