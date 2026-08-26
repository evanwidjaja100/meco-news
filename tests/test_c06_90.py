"""Push to 90% â€” cover remaining branches."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, UTC


class Test90(unittest.TestCase):
    def test_app_main_all(self):
        from meco_news.app import main
        # Test all branches
        self.assertEqual(main(["--config-show", "--json"]), 0)
        self.assertIn(main(["--preflight", "--json"]), (0,3,4,5,6,7))
        self.assertEqual(main(["--status", "--json"]), 0)
        self.assertEqual(main(["--healthcheck", "--json"]), 1)
        # Test backup/restore
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"state.db"
            from meco_news.storage import StateStore
            with StateStore(p) as s:
                s.create_delivery("2026-10-01", config_hash="h")
            with patch.dict("os.environ", {"STATE_DB": str(p)}):
                bak = Path(d)/"bak"
                bak.mkdir()
                self.assertEqual(main(["--backup", str(bak)]), 0)
                db = list(bak.glob("*.db"))[0]
                self.assertEqual(main(["--restore", str(db)]), 0)
                # Test resolve with proper lease and ambiguous
                with StateStore(p) as s:
                    s.acquire_lease("delivery", "o2", 180)
                    d1 = s.create_delivery("2026-10-02", config_hash="h")
                    s.prepare_delivery(d1.delivery_id, [], ["<b>hi</b>"], owner_id="o2")
                    chunk = s.due_chunks(d1.delivery_id)[0]
                    s.begin_chunk_attempt(chunk.chunk_id, run_id="r", owner_id="o2")
                    s.finish_chunk(chunk.chunk_id, "ambiguous", run_id="r", owner_id="o2")
                    s.release_lease("delivery", "o2")
                with StateStore(p) as s:
                    chunk_id = s.connection.execute("SELECT chunk_id FROM outbox_chunks WHERE state='ambiguous'").fetchone()[0]
                    self.assertEqual(main(["--resolve-chunk", str(chunk_id), "--resolution", "retry", "--reason", "test", "--operator", "tester"]), 0)

    def test_collectors_all(self):
        from meco_news.collectors import parse_feed, _bounded_text, _repair_xml, _collect_rss, _collect_gdelt, collect_all
        from meco_news.config import CollectionLimits, load_config
        # Test various payloads
        rss = b'<?xml version="1.0"?><rss><channel><item><title>Test</title><link>https://example.com/a</link><description><![CDATA[desc]]></description></item></channel></rss>'
        self.assertEqual(len(parse_feed(rss, "t", "rss")), 1)
        # Test with DTD
        with patch("meco_news.collectors._fetch", return_value=b'<?xml version="1.0"?><!DOCTYPE foo><rss></rss>'):
            cfg = load_config("config/watchlist.json")
            res = _collect_rss({"id": "t", "name": "T", "url": "https://example.com/rss"}, 25, config=cfg)
            self.assertEqual(res.outcome, "failed")
        # Test gdelt with valid
        mock_data = {"articles": [{"title": "Test", "url": "https://example.com/a", "domain": "example.com", "seendate": "20260824T000000Z"}]}
        with patch("meco_news.collectors._fetch", return_value=json_dumps(mock_data)):
            cfg = load_config("config/watchlist.json")
            res = _collect_gdelt({"id": "t", "name": "T", "query": "test"}, {"max_records": 75, "timespan": "3d"}, 25, config=cfg)
            self.assertEqual(res.outcome, "succeeded")

    def test_storage_all(self):
        from meco_news.storage import StateStore, InvalidTransition
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            with StateStore(p) as s:
                # Test create with generation
                d1 = s.create_delivery("2026-10-03", config_hash="h", generation=5)
                self.assertEqual(d1.generation, 5)
                # Test already_completed
                s.acquire_lease("delivery", "o", 180)
                s.prepare_delivery(d1.delivery_id, [], ["<b>hi</b>"], owner_id="o")
                chunk = s.due_chunks(d1.delivery_id)[0]
                s.begin_chunk_attempt(chunk.chunk_id, run_id="r", owner_id="o")
                s.finish_chunk(chunk.chunk_id, "accepted", run_id="r", owner_id="o", telegram_message_id="1")
                self.assertTrue(s.already_completed("2026-10-03"))
                # Test latest_generation
                self.assertEqual(s.latest_generation("2026-10-03"), 5)
                # Test active_delivery
                self.assertIsNone(s.active_delivery("2026-10-03"))
                # Test identity_keys
                from meco_news.models import NewsItem
                item = NewsItem(title="test", url="https://example.com/test", source="S")
                urls, titles = s.identity_keys([item])
                self.assertIsInstance(urls, set)

    def test_network_telegram(self):
        from meco_news.telegram import TelegramClient, validate_message, build_digest
        from meco_news.models import NewsItem
        # Test validate with empty
        with self.assertRaises(ValueError):
            validate_message("")
        # Test build with many
        items = [NewsItem(title=f"t{i}", url=f"https://example.com/{i}", source="S", topic_label="T", relevance_reason="R") for i in range(5)]
        res = build_digest(items, "MECO", "UTC", max_length=3900)
        self.assertGreater(len(res.messages), 0)
        # Test TelegramClient with placeholder
        with self.assertRaises(ValueError):
            TelegramClient("replace_with_token", "1")
        with self.assertRaises(ValueError):
            TelegramClient("", "1")

    def test_config_urls(self):
        from meco_news.config import load_config, ConfigurationError
        import json, tempfile
        from pathlib import Path
        base = load_config("config/watchlist.json").as_dict()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"c.json"
            # Test invalid lookback
            bad = dict(base); bad["lookback_days"] = 100
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(p)
            # Test invalid url
            bad = dict(base); bad["rss_feeds"] = [{"name": "test", "url": "http://example.com/rss"}]
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(p)

def json_dumps(obj):
    import json
    return json.dumps(obj).encode()

