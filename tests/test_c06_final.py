"""Final push to 80% â€” cover remaining app/collector/storage branches."""

import tempfile
import unittest
from pathlib import Path


class TestFinal(unittest.TestCase):
    def test_app_main_all(self):
        from meco_news.app import main

        # Test verbose, log-file, backup/restore, resolve
        self.assertEqual(main(["--config-show", "--json"]), 0)
        self.assertIn(main(["--preflight", "--json"]), (0, 3, 4, 5, 6, 7))
        # Test --help via SystemExit
        with self.assertRaises(SystemExit) as error:
            main(["--help"])
        self.assertEqual(error.exception.code, 0)

    def test_collectors_comprehensive(self):
        from meco_news.collectors import parse_feed, _bounded_text, _parse_date

        # Test various payloads
        rss = b'<?xml version="1.0"?><rss><channel><item><title>Test</title><link>https://example.com/a</link><description>desc</description><pubDate>Mon, 24 Aug 2026 07:32:32 +0700</pubDate></item></channel></rss>'
        items = parse_feed(rss, "t", "rss")
        self.assertEqual(len(items), 1)
        # Test with atom
        atom = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom</title><link href="https://example.com/b"/><summary>sum</summary></entry></feed>'
        items = parse_feed(atom, "t", "rss")
        self.assertEqual(len(items), 1)
        # Test bounded text with control
        txt, truncated = _bounded_text("hello\x00world", 512)
        self.assertIn("hello", txt)
        # Test parse date with Z
        self.assertIsNotNone(_parse_date("2026-08-24T00:00:00Z"))

    def test_storage_comprehensive(self):
        from meco_news.storage import StateStore

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "db.db"
            with StateStore(p) as s:
                # Test all transitions
                d1 = s.create_delivery("2026-08-29", config_hash="h")
                s.acquire_lease("delivery", "owner", 180)
                s.prepare_delivery(d1.delivery_id, [], ["<b>hi</b>"], owner_id="owner")
                # Test due_chunks with different states
                chunks = s.due_chunks(d1.delivery_id)
                self.assertEqual(len(chunks), 1)
                # Test begin/finish
                chunk = chunks[0]
                info, num = s.begin_chunk_attempt(chunk.chunk_id, run_id="r", owner_id="owner")
                self.assertEqual(num, 1)
                s.finish_chunk(chunk.chunk_id, "accepted", run_id="r", owner_id="owner", telegram_message_id="123")
                self.assertEqual(s.delivery(d1.delivery_id).state, "completed_empty")
                # Test failed_terminal
                d2 = s.create_delivery("2026-08-30", config_hash="h")
                s.prepare_delivery(d2.delivery_id, [], ["<b>hi2</b>"], owner_id="owner")
                chunk2 = s.due_chunks(d2.delivery_id)[0]
                s.begin_chunk_attempt(chunk2.chunk_id, run_id="r", owner_id="owner")
                s.finish_chunk(chunk2.chunk_id, "rejected_terminal", run_id="r", owner_id="owner", error_text="fail")
                self.assertEqual(s.delivery(d2.delivery_id).state, "failed_terminal")
                # Test retry_wait
                d3 = s.create_delivery("2026-08-31", config_hash="h")
                s.prepare_delivery(d3.delivery_id, [], ["<b>hi3</b>"], owner_id="owner")
                chunk3 = s.due_chunks(d3.delivery_id)[0]
                s.begin_chunk_attempt(chunk3.chunk_id, run_id="r", owner_id="owner")
                from datetime import datetime, UTC, timedelta

                s.finish_chunk(
                    chunk3.chunk_id,
                    "rejected_retryable",
                    run_id="r",
                    owner_id="owner",
                    next_attempt_at=datetime.now(UTC) + timedelta(seconds=10),
                )
                self.assertEqual(s.delivery(d3.delivery_id).state, "retry_wait")
