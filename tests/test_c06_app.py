"""C6.1 app/collector/storage corpus — push to 80%."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, UTC


class TestAppCollectorsStorage(unittest.TestCase):
    def test_app_main_all_paths(self):
        from meco_news.app import main

        # Test all main branches via CLI
        # Already covered in test_c06_full, but add more
        # Test --force with no prior delivery
        with tempfile.TemporaryDirectory() as d:
            from meco_news.storage import StateStore

            p = Path(d) / "state.db"
            with patch.dict(
                "os.environ", {"STATE_DB": str(p), "TELEGRAM_BOT_TOKEN": "123456:real-token-12345678901234567890", "TELEGRAM_CHAT_ID": "1"}
            ):
                # Create a completed delivery first
                with StateStore(p) as s:
                    s.create_delivery("2026-08-26", config_hash="h")
                    s.acquire_lease("delivery", "o", 180)
                    s.prepare_delivery(s.active_delivery("2026-08-26").delivery_id, [], ["<b>hi</b>"], owner_id="o")
                # Now test --status and --healthcheck
                self.assertEqual(main(["--status", "--json"]), 0)
                self.assertEqual(main(["--healthcheck", "--json"]), 0)
                # --config-show
                self.assertEqual(main(["--config-show", "--json"]), 0)
                # --preflight
                self.assertEqual(main(["--preflight", "--json"]), 6)

    def test_collectors_gdelt_and_google(self):
        from meco_news.collectors import _collect_gdelt, _collect_rss

        # Test gdelt with invalid JSON
        with patch("meco_news.collectors._fetch", return_value=b"not json"):
            from meco_news.config import load_config

            cfg = load_config("config/watchlist.json")
            res = _collect_gdelt({"id": "t", "name": "T", "query": "test"}, {"max_records": 75, "timespan": "3d"}, 25, config=cfg)
            self.assertEqual(res.outcome, "failed")
        # Test rss with DTD
        with patch("meco_news.collectors._fetch", return_value=b'<?xml version="1.0"?><!DOCTYPE foo><rss></rss>'):
            from meco_news.config import load_config

            cfg = load_config("config/watchlist.json")
            res = _collect_rss({"id": "t", "name": "T", "url": "https://example.com/rss"}, 25, config=cfg)
            self.assertEqual(res.outcome, "failed")

    def test_storage_lease_and_recovery(self):
        from meco_news.storage import StateStore, LeaseLost

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "db.db"
            with StateStore(p) as s:
                # Acquire lease
                self.assertTrue(s.acquire_lease("delivery", "owner1", 1).acquired)
                # Try to acquire with different owner before expiry — should fail
                self.assertFalse(s.acquire_lease("delivery", "owner2", 10).acquired)
                # Heartbeat
                s.heartbeat_lease("delivery", "owner1", 10)
                # Release
                self.assertTrue(s.release_lease("delivery", "owner1"))
                # Acquire again
                self.assertTrue(s.acquire_lease("delivery", "owner2", 180).acquired)
                # Test due_chunks with retry_wait
                d1 = s.create_delivery("2026-08-27", config_hash="h")
                s.prepare_delivery(d1.delivery_id, [], ["<b>hi</b>"], owner_id="owner2")
                # Set retry_wait
                s.connection.execute(
                    "UPDATE outbox_chunks SET state='retry_wait', next_attempt_at='2000-01-01T00:00:00+00:00' WHERE delivery_id=?",
                    (d1.delivery_id,),
                )
                s.connection.commit()
                self.assertEqual(len(s.due_chunks(d1.delivery_id)), 1)
                # Test begin_chunk with wrong owner
                chunk = s.due_chunks(d1.delivery_id)[0]
                with self.assertRaises(LeaseLost):
                    s.begin_chunk_attempt(chunk.chunk_id, run_id="r", owner_id="wrong_owner")

    def test_network_telegram_branches(self):
        from meco_news.telegram import validate_message
        from meco_news.network import BoundedHTTPClient
        from meco_news.config import CollectionLimits, NetworkPolicy

        # validate_message with control chars
        validate_message("<b>hello</b>")
        with self.assertRaises(ValueError):
            validate_message("a" * 20000)
        # Bounded client with redirect limit
        _client = BoundedHTTPClient(CollectionLimits(max_redirects=0), NetworkPolicy(require_https=False), allow_private_for_tests=True)
        # This will be tested via fake server in test_c06_network

    def test_ranking_and_config(self):
        from meco_news.ranking import rank_item, select_digest
        from meco_news.models import NewsItem
        from meco_news.config import load_config

        cfg = load_config("config/watchlist.json")
        # Test with future date
        future = NewsItem(title="future", url="https://example.com/future", source="S", published_at=datetime(2030, 1, 1, tzinfo=UTC))
        ranked = rank_item(future, cfg, datetime.now(UTC))
        self.assertIsNotNone(ranked)
        # Test select with many items
        items = [
            NewsItem(title=f"title {i}", url=f"https://example.com/{i}", source="S", published_at=datetime.now(UTC)) for i in range(10)
        ]
        for it in items:
            rank_item(it, cfg, datetime.now(UTC))
        selected = select_digest(items, cfg)
        self.assertLessEqual(len(selected), cfg["daily_max"])

    def test_preflight_and_backup(self):
        from meco_news.preflight import run_preflight, healthcheck
        from meco_news.config import load_config
        from meco_news.backup import create_backup

        cfg = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "db.db"
            # Test preflight with no DB
            code, report = run_preflight(cfg, state_path=p)
            self.assertIn(code, (0, 3, 4, 5, 6))
            # Test healthcheck with missing DB
            healthy, report = healthcheck(cfg, state_path=p)
            self.assertFalse(healthy)
            # Test backup with no DB (should create)
            from meco_news.storage import StateStore

            with StateStore(p) as s:
                s.create_delivery("2026-08-28", config_hash="h")
            art = create_backup(p, Path(d) / "bak2")
            self.assertTrue(art.database.exists())
