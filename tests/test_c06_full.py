"""C6.1 full coverage — push to 90% for critical branches."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, UTC
import json


class TestAppFull(unittest.TestCase):
    def test_app_main_paths(self):
        from meco_news.app import main
        # config-show
        self.assertEqual(main(["--config-show", "--json"]), 0)
        # preflight
        self.assertEqual(main(["--preflight", "--json"]), 3)
        # status
        self.assertEqual(main(["--status", "--json"]), 0)
        # healthcheck
        self.assertEqual(main(["--healthcheck", "--json"]), 1)
        # dry-run with top-candidates
        self.assertEqual(main(["--dry-run", "--top-candidates", "2", "--config", "config/watchlist.json"]), 0)
        # backup/restore
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"bak"
            p.mkdir()
            # need state db
            from meco_news.storage import StateStore
            s = Path(d)/"state.db"
            with StateStore(s) as st:
                st.create_delivery("2026-08-26", config_hash="h")
            with patch.dict("os.environ", {"STATE_DB": str(s)}):
                self.assertEqual(main(["--backup", str(p)]), 0)

    def test_network_and_telegram(self):
        from meco_news.network import BoundedHTTPClient, ResponseTooLarge
        from meco_news.config import CollectionLimits, NetworkPolicy
        from meco_news.urls import validate_url
        # validate_url with allow_private
        self.assertIsNotNone(validate_url("https://example.com/story", allow_private=True))
        # Bounded client
        client = BoundedHTTPClient(CollectionLimits(response_bytes=100), NetworkPolicy())
        # Test that it rejects large content-length
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Length": "999999999"}
        mock_resp.getcode.return_value = 200
        mock_resp.status = 200
        # _read_bounded should raise
        with self.assertRaises(ResponseTooLarge):
            client._read_bounded(mock_resp, 0)

    def test_collectors_edge(self):
        from meco_news.collectors import _bounded_text, _repair_xml, _entry_values, _parse_date
        from meco_news.config import CollectionLimits
        # _bounded_text
        self.assertEqual(_bounded_text("a"*600, 512)[1], True)
        # _repair_xml
        b = b"<rss><channel><item><title>hi & test</title></item></channel></rss>"
        self.assertIsNotNone(_repair_xml(b, CollectionLimits()))
        # _parse_date
        self.assertIsNone(_parse_date("invalid"))
        self.assertIsNotNone(_parse_date("Mon, 24 Aug 2026 07:32:32 +0700"))

    def test_ranking_and_models(self):
        from meco_news.ranking import rank_item, filter_fresh, deduplicate
        from meco_news.models import NewsItem, canonical_url, normalized_title
        from meco_news.config import load_config
        cfg = load_config("config/watchlist.json")
        # canonical
        self.assertEqual(canonical_url("https://example.com/story?utm_source=x&a=1"), "https://example.com/story?a=1")
        self.assertEqual(normalized_title("Hello - Source", "Source"), "hello")
        # ranking with negative
        item = NewsItem(title="resep masak", url="https://example.com/r", source="S")
        ranked = rank_item(item, cfg, datetime.now(UTC))
        self.assertLess(ranked.score, 0)

    def test_preflight_and_health(self):
        from meco_news.preflight import run_preflight, healthcheck
        from meco_news.config import load_config
        cfg = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            # healthcheck missing
            healthy, _ = healthcheck(cfg, state_path=p)
            self.assertFalse(healthy)
            # run_preflight with online (will fail due to no network, but should not crash)
            code, _ = run_preflight(cfg, state_path=p, online=False)
            self.assertEqual(code, 3)

    def test_storage_transitions(self):
        from meco_news.storage import StateStore
        from meco_news.models import NewsItem
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            with StateStore(p) as s:
                d1 = s.create_delivery("2026-08-27", config_hash="h")
                s.acquire_lease("delivery", "o1", 180)
                s.prepare_delivery(d1.delivery_id, [], ["<b>hi</b>"], owner_id="o1")
                self.assertEqual(s.delivery(d1.delivery_id).state, "prepared_empty")
                s.due_chunks(d1.delivery_id)
                # due_chunks with retry_wait
                s.connection.execute("UPDATE outbox_chunks SET state='retry_wait', next_attempt_at='2000-01-01T00:00:00+00:00' WHERE delivery_id=?", (d1.delivery_id,))
                s.connection.commit()
                self.assertGreater(len(s.due_chunks(d1.delivery_id)), 0)

    def test_telegram_rendering(self):
        from meco_news.telegram import build_digest, _fit_block, validate_message, utf16_units
        from meco_news.models import NewsItem
        from meco_news.timezones import get_timezone
        item = NewsItem(title="t"*600, url="https://example.com/t", source="S"*200, summary="s"*3000, topic_label="T", relevance_reason="R")
        block, reason = _fit_block(1, item, get_timezone("UTC"), 3900, 15600)
        self.assertIsNotNone(block)
        # validate_message
        with self.assertRaises(ValueError):
            validate_message("")
        # build_digest with issues
        res = build_digest([item], "MECO", "UTC", issues=["test"], coverage_notice="cov")
        self.assertGreater(len(res.messages), 0)
        self.assertEqual(utf16_units("😀"), 2)
