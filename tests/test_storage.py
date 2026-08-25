from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path
import tempfile
import unittest

from meco_news.models import NewsItem
from meco_news.storage import StateStore


class StorageTests(unittest.TestCase):
    def test_completed_run_and_sent_article_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            item = NewsItem(
                title="New LPG terminal project",
                url="https://example.com/lpg",
                source="Example",
                published_at=datetime.now(UTC),
                score=15,
                topic="lpg_energy",
            )
            with StateStore(path) as store:
                store.start_run("2026-08-24")
                store.complete_run("2026-08-24", [item])
            with StateStore(path) as store:
                self.assertTrue(store.already_completed("2026-08-24"))
                self.assertEqual(store.sent_fingerprints([item]), {item.fingerprint})


if __name__ == "__main__":
    unittest.main()
