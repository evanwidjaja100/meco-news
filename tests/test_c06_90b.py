"""Push to 90% — cover remaining app/collector/storage branches."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, UTC, timedelta


class Test90b(unittest.TestCase):
    def test_app_all_branches(self):
        from meco_news.app import main, _retry_delay, _next_delivery, _is_due, _has_recovery_work
        from meco_news.config import load_config
        cfg = load_config("config/watchlist.json")
        # Test _retry_delay with jitter
        for i in [1,2,3,4,5]:
            d = _retry_delay(cfg, i, retry_after=0)
            self.assertGreater(d.total_seconds(), 0)
            d2 = _retry_delay(cfg, i, retry_after=99999)
            self.assertLessEqual(d2.total_seconds(), 3600)
        # Test _next_delivery
        nd = _next_delivery(cfg)
        self.assertIsNotNone(nd)
        # Test _is_due and _has_recovery_work
        self.assertIsInstance(_is_due(cfg), bool)
        self.assertIsInstance(_has_recovery_work(cfg), bool)
        # Test main with various modes
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"state.db"
            from meco_news.storage import StateStore
            with StateStore(p) as s:
                s.create_delivery("2026-10-05", config_hash="h")
            with patch.dict("os.environ", {"STATE_DB": str(p)}):
                self.assertEqual(main(["--status", "--json"]), 0)
                self.assertEqual(main(["--healthcheck", "--json"]), 0)

    def test_collectors_all(self):
        from meco_news.collectors import parse_feed, _bounded_text, _repair_xml, _collect_rss
        # Test _bounded_text with control and truncation
        txt, truncated = _bounded_text("a"*600, 512)
        self.assertTrue(truncated)
        txt2, _ = _bounded_text("hello\x00world", 512)
        self.assertIn("hello", txt2)
        # Test _repair_xml with bare &
        b = b"<rss><channel><item><title>hi & test</title></item></channel></rss>"
        from meco_news.config import CollectionLimits
        repaired = _repair_xml(b, CollectionLimits())
        self.assertIn(b"hi", repaired)
        # Exercise source-local failure without starting a live collection.
        with patch("meco_news.collectors._fetch", side_effect=Exception("mocked")):
            result = _collect_rss({"id": "local", "name": "Local", "url": "https://example.invalid/rss"}, 1)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.reason_code, "source_exception")

    def test_storage_all(self):
        from meco_news.storage import StateStore, InvalidTransition
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            with StateStore(p) as s:
                # Test failed_terminal -> retry_wait
                d1 = s.create_delivery("2026-10-06", config_hash="h")
                s.acquire_lease("delivery", "o", 180)
                s.prepare_delivery(d1.delivery_id, [], ["<b>hi</b>"], owner_id="o")
                chunk = s.due_chunks(d1.delivery_id)[0]
                s.begin_chunk_attempt(chunk.chunk_id, run_id="r", owner_id="o")
                s.finish_chunk(chunk.chunk_id, "rejected_terminal", run_id="r", owner_id="o")
                self.assertEqual(s.delivery(d1.delivery_id).state, "failed_terminal")
                # Test due with retry_wait past
                d2 = s.create_delivery("2026-10-07", config_hash="h")
                s.prepare_delivery(d2.delivery_id, [], ["<b>hi2</b>"], owner_id="o")
                s.connection.execute("UPDATE outbox_chunks SET state='retry_wait', next_attempt_at='2000-01-01T00:00:00+00:00' WHERE delivery_id=?", (d2.delivery_id,))
                s.connection.commit()
                self.assertEqual(len(s.due_chunks(d2.delivery_id)), 1)
                # Test invalid transition
                with self.assertRaises(InvalidTransition):
                    s.prepare_delivery(d2.delivery_id, [], ["<b>hi3</b>"], owner_id="o")

    def test_telegram_all(self):
        from meco_news.telegram import build_digest, validate_message, _fit_block
        from meco_news.models import NewsItem
        from meco_news.timezones import get_timezone
        # Test _fit_block with various
        item = NewsItem(title="t"*100, url="https://example.com/t", source="S", topic_label="T", relevance_reason="R")
        block, reason = _fit_block(1, item, get_timezone("UTC"), 3900, 15600)
        self.assertIsNotNone(block)
        # Test validate with empty
        with self.assertRaises(ValueError):
            validate_message("")
        # Test build with empty
        res = build_digest([], "MECO", "UTC")
        self.assertIn("Warning", res.messages[0])

    def test_config_and_urls(self):
        from meco_news.config import load_config, ConfigurationError
        import json, tempfile
        from pathlib import Path
        base = load_config("config/watchlist.json").as_dict()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"c.json"
            # Test invalid daily_min
            bad = dict(base); bad["daily_min"] = 0
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(p)
            # Test invalid max_per_topic
            bad = dict(base); bad["max_per_topic"] = 100
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(p)
        from meco_news.urls import validate_url, URLPolicyError
        with self.assertRaises(URLPolicyError):
            validate_url("https://example.com:99999/")
        with self.assertRaises(URLPolicyError):
            validate_url("https://example.com/\x00")
