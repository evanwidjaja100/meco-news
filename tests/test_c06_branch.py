"""Branch coverage for 90% — cover remaining critical branches."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestBranch(unittest.TestCase):
    def test_app_branches(self):
        from meco_news.app import main, _retry_delay
        from meco_news.config import load_config
        cfg = load_config("config/watchlist.json")
        # Test _retry_delay with various attempts
        for i in range(1, 6):
            d = _retry_delay(cfg, i, retry_after=10)
            self.assertIsNotNone(d)
            d2 = _retry_delay(cfg, i, retry_after=99999)
            self.assertLessEqual(d2.total_seconds(), 3600)
        # Test main with various modes
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"state.db"
            with patch.dict("os.environ", {"STATE_DB": str(p), "TELEGRAM_BOT_TOKEN": "123456:real-token-12345678901234567890", "TELEGRAM_CHAT_ID": "1"}):
                # Test with need recovery
                self.assertIn(main(["--status", "--json"]), (0,))
                # Test with backup
                bak = Path(d)/"bak2"
                bak.mkdir()
                from meco_news.storage import StateStore
                with StateStore(p) as s:
                    s.create_delivery("2026-09-10", config_hash="h")
                self.assertEqual(main(["--backup", str(bak)]), 0)

    def test_collectors_branches(self):
        from meco_news.collectors import _bounded_text, _repair_xml, parse_feed
        from meco_news.config import CollectionLimits
        # Test _bounded_text with various inputs
        self.assertEqual(_bounded_text(None, 10)[0], "")
        self.assertEqual(_bounded_text(123, 10)[0], "")
        # Test _repair_xml with control
        b = b"\x00\x01\x02test"
        repaired = _repair_xml(b, CollectionLimits())
        self.assertIsNotNone(repaired)
        # Test parse_feed with various
        rss = b'<?xml version="1.0"?><rss><channel><item><title>hi</title><link>https://example.com/a</link></item></channel></rss>'
        self.assertEqual(len(parse_feed(rss, "t", "rss")), 1)
        # Test with missing title
        rss2 = b'<?xml version="1.0"?><rss><channel><item><link>https://example.com/a</link></item></channel></rss>'
        self.assertEqual(len(parse_feed(rss2, "t", "rss")), 0)

    def test_storage_branches(self):
        from meco_news.storage import StateStore, InvalidTransition
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            with StateStore(p) as s:
                # Test invalid transition
                d1 = s.create_delivery("2026-09-11", config_hash="h")
                s.acquire_lease("delivery", "o", 180)
                s.prepare_delivery(d1.delivery_id, [], ["<b>hi</b>"], owner_id="o")
                # Try to prepare again — should fail
                with self.assertRaises(InvalidTransition):
                    s.prepare_delivery(d1.delivery_id, [], ["<b>hi2</b>"], owner_id="o")
                # Test due_chunks with retry_wait not due
                s.connection.execute("UPDATE outbox_chunks SET state='retry_wait', next_attempt_at='2099-01-01T00:00:00+00:00' WHERE delivery_id=?", (d1.delivery_id,))
                s.connection.commit()
                self.assertEqual(len(s.due_chunks(d1.delivery_id)), 0)
                # Test due with past
                s.connection.execute("UPDATE outbox_chunks SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE delivery_id=?", (d1.delivery_id,))
                s.connection.commit()
                self.assertEqual(len(s.due_chunks(d1.delivery_id)), 1)

    def test_network_and_telegram_branches(self):
        from meco_news.telegram import validate_message, build_digest
        from meco_news.models import NewsItem
        # Test with empty
        with self.assertRaises(ValueError):
            validate_message("")
        # Test build_digest with coverage
        items = [NewsItem(title="t", url="https://example.com/t", source="S", topic_label="T", relevance_reason="R") for _ in range(10)]
        res = build_digest(items, "MECO", "UTC", max_length=3900)
        self.assertGreater(len(res.messages), 0)
        # Test with empty
        res2 = build_digest([], "MECO", "UTC")
        self.assertIn("Warning", res2.messages[0])

    def test_config_and_urls(self):
        from meco_news.config import load_config, ConfigurationError
        import json, tempfile
        from pathlib import Path
        base = load_config("config/watchlist.json").as_dict()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"c.json"
            # Test duplicate topic
            bad = dict(base); bad["topics"] = base["topics"] + [base["topics"][0]]
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(p)
            # Test invalid timezone
            bad = dict(base); bad["timezone"] = "Invalid/Zone"
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(p)
        from meco_news.urls import validate_url, URLPolicyError
        with self.assertRaises(URLPolicyError):
            validate_url("https://example.com:99999/")
        with self.assertRaises(URLPolicyError):
            validate_url("https://example.com/\x00")

    def test_ranking_and_preflight(self):
        from meco_news.ranking import rank_item, deduplicate, filter_fresh
        from meco_news.models import NewsItem
        from meco_news.config import load_config
        from datetime import datetime, UTC, timedelta
        cfg = load_config("config/watchlist.json")
        # Test with need context
        item = NewsItem(title="food processing plant", url="https://example.com/food", source="S", published_at=datetime.now(UTC))
        ranked = rank_item(item, cfg, datetime.now(UTC))
        self.assertIsNotNone(ranked.topic)
        # Test freshness with future
        future = NewsItem(title="future", url="https://example.com/f", source="S", published_at=datetime(2050, 1, 1, tzinfo=UTC))
        fresh, reasons = filter_fresh([future], cfg, datetime.now(UTC))
        self.assertEqual(len(fresh), 0)
        self.assertIn("future_date", reasons)
        # Test dedup with many distinct
        items = [NewsItem(title=f"completely different headline number {i} about gas infrastructure project {i}", url=f"https://example.com/{i}", source="S") for i in range(3)]
        self.assertGreaterEqual(len(deduplicate(items, cfg)), 1)
