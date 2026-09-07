from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path
import tempfile
import unittest

from meco_news.models import NewsItem
from meco_news.storage import StateStore


def _complete_via_outbox(store: StateStore, delivery_date: str, item: NewsItem) -> None:
    delivery = store.active_delivery(delivery_date)
    if delivery is None:
        delivery = store.create_delivery(delivery_date, owner_id="fixture")
    store.prepare_delivery(
        delivery.delivery_id,
        [item],
        ["<b>fixture delivery</b>"],
        owner_id="fixture",
        item_chunk_indexes={item.fingerprint: 0},
    )
    chunk = store.due_chunks(delivery.delivery_id)[0]
    store.begin_chunk_attempt(chunk.chunk_id, run_id="fixture-run", owner_id="fixture")
    store.finish_chunk(chunk.chunk_id, "accepted", run_id="fixture-run", owner_id="fixture", telegram_message_id="1")


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
                store.acquire_lease("delivery", "fixture", 180)
                store.start_run("2026-08-24", owner_id="fixture")
                _complete_via_outbox(store, "2026-08-24", item)
                store.release_lease("delivery", "fixture")
            with StateStore(path) as store:
                self.assertTrue(store.already_completed("2026-08-24"))
                self.assertEqual(store.sent_fingerprints([item]), {item.fingerprint})


if __name__ == "__main__":
    unittest.main()
