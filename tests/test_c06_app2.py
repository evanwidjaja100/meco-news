"""Push app and collectors to 80%."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestAppCover(unittest.TestCase):
    def test_run_once_all_sources_failed(self):
        from meco_news.config import load_config
        from meco_news.collectors import CollectionResult, SourceResult
        from datetime import datetime, UTC
        cfg = load_config("config/watchlist.json")
        failed = CollectionResult([], [SourceResult("a", "A", "failed")], datetime.now(UTC), 0)
        # all_sources_failed True
        self.assertTrue(failed.all_sources_failed)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            with patch.dict("os.environ", {"STATE_DB": str(p), "TELEGRAM_BOT_TOKEN": "123456:real-token-12345678901234567890", "TELEGRAM_CHAT_ID": "1"}):
                with patch("meco_news.app.collect_all", return_value=failed):
                    from meco_news.app import run_once
                    res = run_once(cfg)
                    # Should be retry_wait (1)
                    self.assertEqual(int(res) if hasattr(res, "code") else res, 1)

    def test_app_main_backup_restore(self):
        from meco_news.app import main
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"state.db"
            bak = Path(d)/"bak"
            bak.mkdir()
            from meco_news.storage import StateStore
            with StateStore(p) as s:
                s.create_delivery("2026-08-30", config_hash="h")
            with patch.dict("os.environ", {"STATE_DB": str(p)}):
                # backup
                self.assertEqual(main(["--backup", str(bak)]), 0)
                # restore
                # find backup file
                import pathlib
                db_files = list(bak.glob("*.db"))
                self.assertTrue(len(db_files) > 0)
                self.assertEqual(main(["--restore", str(db_files[0])]), 0)

    def test_collectors_repair(self):
        from meco_news.collectors import parse_feed
        # Bare ampersand
        rss = b'<?xml version="1.0"?><rss><channel><item><title>hi & test</title><link>https://example.com/a</link></item></channel></rss>'
        items = parse_feed(rss, "t", "rss")
        self.assertEqual(len(items), 1)
        self.assertIn("hi", items[0].title)

    def test_storage_lease_recovery(self):
        from meco_news.storage import StateStore
        from datetime import datetime, UTC, timedelta
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            with StateStore(p) as s:
                s.acquire_lease("delivery", "owner1", 1)
                delivery = s.create_delivery("2026-08-25", config_hash="h")
                s.prepare_delivery(delivery.delivery_id, [], ["<b>hi</b>"], owner_id="owner1")
                chunk = s.due_chunks(delivery.delivery_id)[0]
                s.begin_chunk_attempt(chunk.chunk_id, run_id="r", owner_id="owner1")
                expired_at = datetime.now(UTC) + timedelta(seconds=2)
                # Expired leases mark in-flight work ambiguous before reuse.
                self.assertTrue(s.recover_expired_lease("delivery", now=expired_at))
                self.assertEqual(s.delivery(delivery.delivery_id).state, "needs_attention")
                self.assertEqual(s.due_chunks(delivery.delivery_id), [])
                # Now can acquire as new owner
                self.assertTrue(s.acquire_lease("delivery", "owner2", 10).acquired)

    def test_telegram_build(self):
        from meco_news.telegram import build_digest
        from meco_news.models import NewsItem
        from datetime import datetime, UTC
        items = [NewsItem(title=f"title {i}", url=f"https://example.com/{i}", source="S", topic_label="T", relevance_reason="R", published_at=datetime.now(UTC)) for i in range(5)]
        res = build_digest(items, "MECO", "UTC", max_length=900)
        self.assertGreater(len(res.messages), 0)

    def test_preflight_online(self):
        from meco_news.config import load_config
        from meco_news.preflight import run_preflight
        cfg = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            class FakeTelegram:
                def __init__(self, *args, **kwargs):
                    pass

                def get_me(self):
                    return {"username": "offline-test"}

            class FakeHTTP:
                def __init__(self, *args, **kwargs):
                    pass

                def fetch(self, url, *, source_id=""):
                    from meco_news.network import FetchResponse
                    from meco_news.urls import validate_url
                    return FetchResponse(validate_url(url), b"<rss/>", 200, "application/rss+xml")

            # Online readiness is tested against deterministic local fakes.
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "valid-test-token", "TELEGRAM_CHAT_ID": "1"}), \
                patch("meco_news.preflight.TelegramClient", FakeTelegram), \
                patch("meco_news.preflight.BoundedHTTPClient", FakeHTTP):
                code, report = run_preflight(cfg, state_path=p, online=True)
                self.assertEqual(code, 0)
                self.assertTrue(report["checks"]["telegram"]["ok"])
                self.assertTrue(all(check["ok"] for check in report["checks"]["sources"]))
