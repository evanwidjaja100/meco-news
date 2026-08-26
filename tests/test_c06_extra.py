"""Extra coverage for 80%."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestExtra(unittest.TestCase):
    def test_app_main_all(self):
        from meco_news.app import main
        from meco_news.app import RunOutcome
        # Exercise dispatch without allowing a live collection or Telegram call.
        with patch("meco_news.app.run_once", return_value=RunOutcome(0, "forced-test")) as run:
            self.assertEqual(main(["--force"]), 0)
            run.assert_called_once()
            self.assertTrue(run.call_args.kwargs["force"])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"state.db"
            with patch.dict("os.environ", {"STATE_DB": str(p), "TELEGRAM_BOT_TOKEN": "123456:real-token-12345678901234567890", "TELEGRAM_CHAT_ID": "1"}):
                # Test --run-if-due when not due (fresh DB, no prior delivery, not due)
                # Use a time where _is_due is False by mocking
                with patch("meco_news.app._is_due", return_value=False), patch("meco_news.app._has_recovery_work", return_value=False):
                    self.assertEqual(main(["--run-if-due"]), 0)

    def test_collectors_comprehensive(self):
        from meco_news.collectors import parse_feed, _bounded_text, _repair_xml, _entry_values, _parse_date, _collect_gdelt
        from meco_news.config import CollectionLimits
        # Test _bounded_text with html
        txt, _ = _bounded_text("<b>hello</b>", 512)
        self.assertIn("hello", txt)
        # Test _repair_xml
        b = b"<rss><channel><item><title>hi &amp; test</title></item></channel></rss>"
        repaired = _repair_xml(b, CollectionLimits())
        self.assertIn(b"hi", repaired)
        # Test _entry_values with atom
        import xml.etree.ElementTree as ET
        xml = b'<entry><title>Atom</title><link href="https://example.com/b" rel="alternate"/><summary>sum</summary></entry>'
        el = ET.fromstring(xml)
        title, link, src, src_url, summary, pub = _entry_values(el, CollectionLimits())
        self.assertEqual(title, "Atom")
        self.assertEqual(link, "https://example.com/b")

    def test_storage_all(self):
        from meco_news.storage import StateStore
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            with StateStore(p) as s:
                # Test all transitions
                d1 = s.create_delivery("2026-09-01", config_hash="h")
                s.acquire_lease("delivery", "o", 180)
                s.prepare_delivery(d1.delivery_id, [], ["<b>hi</b>"], owner_id="o")
                # Test heartbeat
                s.heartbeat_lease("delivery", "o", 180)
                # Test due_chunks
                self.assertEqual(len(s.due_chunks(d1.delivery_id)), 1)
                # Test lease_info
                self.assertIsNotNone(s.lease_info("delivery"))
                # Test recover
                s.release_lease("delivery", "o")
                s.acquire_lease("delivery", "o2", 1)
                import time
                time.sleep(1.1)
                s.recover_expired_lease("delivery")
                # Test fail_delivery
                d2 = s.create_delivery("2026-09-02", config_hash="h")
                s.acquire_lease("delivery", "o2", 180)
                s.prepare_delivery(d2.delivery_id, [], ["<b>hi2</b>"], owner_id="o2")
                chunk = s.due_chunks(d2.delivery_id)[0]
                s.begin_chunk_attempt(chunk.chunk_id, run_id="r", owner_id="o2")
                s.finish_chunk(chunk.chunk_id, "rejected_terminal", run_id="r", owner_id="o2")
                self.assertEqual(s.delivery(d2.delivery_id).state, "failed_terminal")
                # Test set_collection_retry
                d3 = s.create_delivery("2026-09-03", config_hash="h")
                from datetime import datetime, UTC, timedelta
                s.set_collection_retry(d3.delivery_id, next_attempt_at=datetime.now(UTC)+timedelta(seconds=10), error="test")
                self.assertEqual(s.delivery(d3.delivery_id).state, "retry_wait")

    def test_telegram_all(self):
        from meco_news.telegram import build_digest, validate_message, _fit_block, _clean_display, DEFAULT_MESSAGE_BYTES
        from meco_news.models import NewsItem
        from meco_news.timezones import get_timezone
        # Test _clean_display
        self.assertEqual(_clean_display("  hello  ", 10), "hello")
        # Test validate_message with empty and oversized
        with self.assertRaises(ValueError):
            validate_message("")
        with self.assertRaises(ValueError):
            validate_message("a"*(DEFAULT_MESSAGE_BYTES+1))
        # Test build_digest with multiple
        items = [NewsItem(title=f"t{i}", url=f"https://example.com/{i}", source="S", topic_label="T", relevance_reason="R") for i in range(3)]
        res = build_digest(items, "MECO", "UTC", max_length=3900)
        self.assertGreater(len(res.messages), 0)

    def test_urls_all(self):
        from meco_news.urls import validate_url, URLPolicyError, same_or_allowed_redirect, sanitize_url_for_log
        # Test same_or_allowed
        a = validate_url("https://example.com/a")
        b = validate_url("https://example.com/b")
        self.assertTrue(same_or_allowed_redirect(a, b))
        # Test sanitize
        self.assertEqual(sanitize_url_for_log("https://example.com/a"), "https://example.com:443")
        self.assertEqual(sanitize_url_for_log("invalid"), "<invalid-url>")
        # Test with port
        self.assertIsNotNone(validate_url("https://example.com:8080/a"))

    def test_config_all(self):
        from meco_news.config import load_config, ConfigurationError
        import json, tempfile
        from pathlib import Path
        base = load_config("config/watchlist.json").as_dict()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"c.json"
            # Test invalid port
            bad = dict(base); bad["lease_ttl_seconds"] = 10
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(p)
            # Test trusted_domains with slash
            bad = dict(base); bad["trusted_domains"] = ["example.com/path"]
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(p)
