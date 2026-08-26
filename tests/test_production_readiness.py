from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from meco_news.app import main
from meco_news.backup import create_backup, restore_backup
from meco_news.collectors import CollectionResult, SourceResult
from meco_news.config import ConfigurationError, load_config
from meco_news.models import NewsItem
from meco_news.ranking import deduplicate, filter_fresh
from meco_news.storage import StateStore
from meco_news.telegram import build_digest, utf16_units
from meco_news.timezones import get_timezone
from meco_news.urls import URLPolicyError, validate_url


class ConfigurationAndURLTests(unittest.TestCase):
    def test_config_is_typed_and_rejects_unknown_keys(self) -> None:
        config = load_config("config/watchlist.json")
        self.assertEqual(config.timezone, "Asia/Jakarta")
        self.assertEqual(config["daily_min"], 5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            raw = config.as_dict()
            raw["unexpected"] = True
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_url_identity_sorts_query_and_rejects_userinfo(self) -> None:
        first = validate_url("https://Example.com/story?b=2&utm_source=x&a=1#fragment")
        second = validate_url("https://example.com/story?a=1&b=2")
        self.assertEqual(first.normalized_url, second.normalized_url)
        with self.assertRaises(URLPolicyError):
            validate_url("https://user:password@example.com/story")
        with self.assertRaises(URLPolicyError):
            validate_url("https://127.0.0.1/story")


class FreshnessAndDedupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config("config/watchlist.json")
        cls.now = datetime(2026, 8, 24, 3, tzinfo=UTC)

    def _item(self, title: str, hours: int = 1, url: str = "https://example.com/story") -> NewsItem:
        return NewsItem(title=title, url=url, source="Example", published_at=self.now - timedelta(hours=hours))

    def test_old_missing_and_future_items_are_excluded(self) -> None:
        items = [
            self._item("Gas infrastructure project", hours=24 * 8),
            NewsItem(title="Gas infrastructure project no date", url="https://example.com/missing", source="Example"),
            self._item("Gas infrastructure project future", hours=-24),
            self._item("Gas infrastructure project fresh", hours=1),
        ]
        fresh, reasons = filter_fresh(items, self.config, self.now)
        self.assertEqual([item.title for item in fresh], ["Gas infrastructure project fresh"])
        self.assertEqual(reasons["stale"], 1)
        self.assertEqual(reasons["missing_date"], 1)
        self.assertEqual(reasons["future_date"], 1)

    def test_dedup_is_permutation_independent(self) -> None:
        items = [
            self._item("Pertamina kerahkan 33 mobil tangki BBM ke Flores", url="https://example.com/a"),
            self._item("Pertamina kerahkan 38 mobil tangki BBM menuju NTT", url="https://example.com/b"),
            self._item("Gas infrastructure construction starts", url="https://example.com/c"),
        ]
        first = [(item.title, item.url) for item in deduplicate(items, self.config)]
        second = [(item.title, item.url) for item in deduplicate(list(reversed(items)), self.config)]
        self.assertEqual(first, second)


class LeaseAndOutboxTests(unittest.TestCase):
    def _item(self) -> NewsItem:
        return NewsItem(
            title="New LPG terminal project",
            url="https://example.com/lpg",
            source="Example",
            published_at=datetime.now(UTC),
            score=15,
            topic="lpg_energy",
            topic_label="LPG",
            relevance_reason="Tank demand",
        )

    def test_only_one_owner_and_confirmed_chunk_is_not_due_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as first, StateStore(path) as second:
                self.assertTrue(first.acquire_lease("delivery", "one", 180).acquired)
                self.assertFalse(second.acquire_lease("delivery", "two", 180).acquired)
                delivery = first.create_delivery("2026-08-24", config_hash="hash")
                first.prepare_delivery(
                    delivery.delivery_id, [self._item()], ["<b>hello</b>"], owner_id="one", item_chunk_indexes={self._item().fingerprint: 0}
                )
                chunk = first.due_chunks(delivery.delivery_id)[0]
                _, attempt = first.begin_chunk_attempt(chunk.chunk_id, run_id="run", owner_id="one")
                self.assertEqual(attempt, 1)
                completed = first.finish_chunk(chunk.chunk_id, "accepted", run_id="run", owner_id="one", telegram_message_id="42")
                self.assertEqual(completed.state, "completed")
                self.assertEqual(first.due_chunks(delivery.delivery_id), [])
                self.assertEqual(first.sent_fingerprints([self._item()]), {self._item().fingerprint})

    def test_status_exposes_scheduler_lease_and_active_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                self.assertTrue(store.acquire_lease("scheduler", "daemon", 180).acquired)
                self.assertTrue(store.acquire_lease("delivery", "owner", 180).acquired)
                delivery = store.create_delivery("2026-08-24", config_hash="hash")
                item = self._item()
                store.prepare_delivery(delivery.delivery_id, [item], ["<b>hello</b>"], owner_id="owner", item_chunk_indexes={item.fingerprint: 0})
                chunk = store.due_chunks(delivery.delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="run", owner_id="owner")
                snapshot = store.status_snapshot()
                self.assertEqual(snapshot["scheduler_lease"]["owner_id"], "daemon")
                self.assertEqual(snapshot["active_chunk"]["state"], "in_flight")

    def test_ambiguous_chunk_requires_manual_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                self.assertTrue(store.acquire_lease("delivery", "owner", 180).acquired)
                item = self._item()
                delivery = store.create_delivery("2026-08-24", config_hash="hash")
                store.prepare_delivery(delivery.delivery_id, [item], ["<b>hello</b>"], owner_id="owner", item_chunk_indexes={item.fingerprint: 0})
                chunk = store.due_chunks(delivery.delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="run", owner_id="owner")
                current = store.finish_chunk(
                    chunk.chunk_id, "ambiguous", run_id="run", owner_id="owner", error_class="telegram_ambiguous", error_text="unknown"
                )
                self.assertEqual(current.state, "needs_attention")
                self.assertEqual(store.due_chunks(delivery.delivery_id), [])
                resolved = store.resolve_chunk(chunk.chunk_id, "retry", owner_id="owner", reason="confirmed not delivered", operator="tester")
                self.assertEqual(resolved.state, "sending")
                self.assertEqual(len(store.due_chunks(delivery.delivery_id)), 1)

    def test_legacy_schema_is_adopted_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE sent_articles (fingerprint TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, source TEXT NOT NULL, topic TEXT NOT NULL, score INTEGER NOT NULL, sent_at TEXT NOT NULL, delivery_date TEXT NOT NULL);
                CREATE TABLE runs (delivery_date TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL, item_count INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
                """
            )
            connection.execute(
                "INSERT INTO sent_articles VALUES ('title-key','Old story','https://example.com/old','Example','energy',10,'2026-08-24T00:00:00+00:00','2026-08-24')"
            )
            connection.execute(
                "INSERT INTO runs VALUES ('2026-08-24','2026-08-24T00:00:00+00:00','2026-08-24T01:00:00+00:00','completed',1,'')"
            )
            connection.commit()
            connection.close()
            with StateStore(path) as store:
                self.assertEqual(store.schema_version, 3)
                self.assertTrue(store.already_completed("2026-08-24"))
                self.assertEqual(store.integrity_check(), "ok")

    def test_online_backup_manifest_and_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "state.db"
            restored = root / "restored.db"
            item = self._item()
            with StateStore(source) as store:
                store.start_run("2026-08-24")
                store.complete_run("2026-08-24", [item])
            artifact = create_backup(source, root / "backups", config_hash="config")
            self.assertTrue(artifact.manifest.exists())
            restore_backup(artifact.database, restored)
            with StateStore(restored, readonly=True) as store:
                self.assertTrue(store.already_completed("2026-08-24"))


class CLIAndMessageTests(unittest.TestCase):
    def test_digest_uses_frozen_delivery_date(self) -> None:
        result = build_digest([], "MECO", "UTC", minimum_count=1, delivery_date="2020-01-02")
        self.assertIn("02 January 2020", result.messages[0])

    def test_live_run_freezes_and_completes_through_outbox(self) -> None:
        config = load_config("config/watchlist.json")
        item = NewsItem(
            title="Gas infrastructure construction starts for new project",
            url="https://example.com/live",
            source="Example",
            published_at=datetime.now(UTC),
            topic="energy_projects",
        )
        collection = CollectionResult(
            [item], [SourceResult("fake", "fake", "succeeded", items=[item], accepted_count=1)], datetime.now(UTC), 1
        )

        class FakeTelegram:
            def __init__(self, *_args, **_kwargs):
                self.sent: list[str] = []

            def send_html(self, text: str) -> str:
                self.sent.append(text)
                return str(len(self.sent))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"STATE_DB": str(Path(directory) / "state.db"), "TELEGRAM_BOT_TOKEN": "123456:real-token-value", "TELEGRAM_CHAT_ID": "1"},
                clear=False,
            ),
            patch("meco_news.app.collect_all", return_value=collection),
            patch("meco_news.app.TelegramClient", FakeTelegram),
        ):
            self.assertEqual(__import__("meco_news.app", fromlist=["run_once"]).run_once(config), 0)
            with StateStore(Path(directory) / "state.db", readonly=True) as store:
                delivery_date = datetime.now(get_timezone(config.timezone)).date().isoformat()
                self.assertTrue(store.already_completed(delivery_date))

    def test_invalid_daemon_dry_run_is_rejected_before_run(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["--daemon", "--dry-run"])
        self.assertEqual(raised.exception.code, 2)

    def test_dry_run_does_not_create_state(self) -> None:
        result = CollectionResult([], [SourceResult("fake", "fake", "succeeded")], datetime.now(UTC), 0)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"STATE_DB": str(Path(directory) / "state.db")}, clear=False),
            patch("meco_news.app.collect_all", return_value=result),
        ):
            self.assertEqual(main(["--dry-run", "--config", "config/watchlist.json"]), 0)
            self.assertFalse((Path(directory) / "state.db").exists())

    def test_emoji_message_uses_utf16_bound_and_omits_oversized_item(self) -> None:
        item = NewsItem(
            title="Emoji " + "😀" * 5_000,
            url="https://example.com/emoji",
            source="Example",
            topic_label="Topic",
            relevance_reason="Reason",
        )
        result = build_digest([item], "MECO", "UTC", max_length=900)
        self.assertTrue(all(utf16_units(message) <= 900 for message in result.messages))
        self.assertEqual(len(result.included_items), 1)


if __name__ == "__main__":
    unittest.main()
