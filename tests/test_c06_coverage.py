"""C6.1 coverage helpers — push overall to >=90% for critical paths."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CoverageHelpers(unittest.TestCase):
    def test_app_cli_branches(self) -> None:
        from meco_news.app import main
        # Invalid combos
        for args in [
            ["--daemon", "--dry-run"],
            ["--daemon", "--force"],
            ["--run-now"],
            ["--top-candidates", "1"],
            ["--ignore-history"],
            ["--force", "--dry-run"],
            ["--online"],
            ["--json"],
            ["--resolve-chunk", "1"],
            ["--max-heartbeat-age", "0"],
            ["--top-candidates", "-1"],
        ]:
            with self.assertRaises(SystemExit) as cm:
                main(args)
            self.assertEqual(cm.exception.code, 2)

    def test_config_boundaries(self) -> None:
        from meco_news.config import load_config, ConfigurationError
        import json, tempfile
        from pathlib import Path
        base = load_config("config/watchlist.json").as_dict()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            # Unknown key
            bad = dict(base); bad["unexpected"] = 1
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(p)
            # Invalid HH:MM
            bad = dict(base); bad["delivery_time"] = "25:00"
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(p)

    def test_network_and_url(self) -> None:
        from meco_news.urls import validate_url, URLPolicyError
        invalid = [
            ("http://example.com/story", "scheme_disallowed"),
            ("https://user:pass@example.com/", "userinfo_disallowed"),
            ("https://example.com:99999/", "invalid_port"),
            (" https://example.com/", "url_whitespace"),
            ("https://example.com/\x00", "url_control_character"),
        ]
        for url, reason in invalid:
            with self.subTest(url=url):
                with self.assertRaises(URLPolicyError) as error:
                    validate_url(url)
                self.assertEqual(error.exception.reason_code, reason)
        # Private with allow_private
        self.assertIsNotNone(validate_url("https://192.168.1.1/story", allow_private=True))

    def test_collectors_and_ranking(self) -> None:
        from meco_news.collectors import parse_feed, SourceDataError
        from meco_news.ranking import filter_fresh, rank_item, deduplicate, select_digest
        from meco_news.models import NewsItem
        from meco_news.config import load_config
        from datetime import datetime, UTC, timedelta
        config = load_config("config/watchlist.json")
        # Parse with limits
        with self.assertRaises(SourceDataError):
            parse_feed(b"", "t", "rss")
        # Freshness
        old = NewsItem(title="old", url="https://example.com/old", source="S", published_at=datetime.now(UTC)-timedelta(days=20))
        fresh, reasons = filter_fresh([old], config, datetime.now(UTC))
        self.assertEqual(len(fresh), 0)
        # Ranking with no topic
        item = NewsItem(title="x", url="https://example.com/x", source="S")
        ranked = rank_item(item, config, datetime.now(UTC))
        self.assertIsNotNone(ranked)
        # Deduplicate
        a = NewsItem(title="a", url="https://example.com/a", source="S")
        b = NewsItem(title="a", url="https://example.com/a", source="S")
        self.assertEqual(len(deduplicate([a,b], config)), 1)
        # Select
        self.assertEqual(select_digest([], config), [])

    def test_storage_and_backup(self) -> None:
        from meco_news.storage import StateStore
        from meco_news.backup import create_backup, restore_backup
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            with StateStore(p) as s:
                s.create_delivery("2026-08-26", config_hash="h")
                s.acquire_lease("delivery", "o", 180)
            art = create_backup(p, Path(d)/"bak")
            restore_backup(art.database, Path(d)/"restored.db")
            self.assertTrue((Path(d)/"restored.db").exists())

    def test_telegram_and_preflight(self) -> None:
        from meco_news.telegram import validate_message, build_digest, utf16_units
        from meco_news.models import NewsItem
        self.assertGreater(utf16_units("😀"), 1)
        with self.assertRaises(ValueError):
            validate_message("")
        with self.assertRaises(ValueError):
            validate_message("a"*20000)
        item = NewsItem(title="t", url="https://example.com/t", source="S", topic_label="T", relevance_reason="R")
        res = build_digest([item], "MECO", "UTC", max_length=900)
        self.assertGreater(len(res.messages), 0)
        from meco_news.config import load_config
        from meco_news.preflight import run_preflight, healthcheck
        cfg = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"db.db"
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
                code, _ = run_preflight(cfg, state_path=p)
            self.assertEqual(code, 3)
            healthy, _ = healthcheck(cfg, state_path=p)
            self.assertIsInstance(healthy, bool)
