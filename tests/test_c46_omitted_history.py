"""C4.6 final-payload isolation: an omitted item never reaches outbox or sent history."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, UTC
from pathlib import Path

from meco_news.models import NewsItem
from meco_news.storage import StateStore
from meco_news.telegram import build_digest, validate_message


def _healthy(title: str, number: int) -> NewsItem:
    return NewsItem(
        title=title,
        url=f"https://example.com/market/{number}",
        source="Example Publisher",
        published_at=datetime(2026, 8, 24, tzinfo=UTC),
        topic_label="LPG terminal",
        relevance_reason="Potential equipment demand.",
        summary="Ringkasan singkat.",
    )


class OmittedHistoryIsolationTests(unittest.TestCase):
    def test_omitted_fingerprint_absent_from_delivery_and_sent_history(self) -> None:
        healthy_a = _healthy("Cerita kilang minyak pertama tentang konstruksi terminal", 11)
        oversized = _healthy("Cerita raksasa yang terpotong karena tautan sangat panjang", 12)
        oversized.url = "https://example.com/" + "u" * 2040
        healthy_b = _healthy("Cerita kilang minyak kedua tentang konstruksi terminal", 13)

        built = build_digest(
            [healthy_a, oversized, healthy_b],
            "MECO",
            "UTC",
            max_length=3900,
            max_bytes=1200,
        )
        self.assertEqual(len(built.omitted_items), 1)
        self.assertEqual(built.omitted_items[0][0].fingerprint, oversized.fingerprint)
        included_fingerprints = {item.fingerprint for item in built.included_items}
        self.assertEqual(included_fingerprints, {healthy_a.fingerprint, healthy_b.fingerprint})
        self.assertEqual(set(built.item_chunk_indexes), included_fingerprints)
        self.assertTrue(built.messages)
        for message in built.messages:
            validate_message(message, max_units=3900, max_bytes=1200)
        for chunk_index in built.item_chunk_indexes.values():
            self.assertGreaterEqual(chunk_index, 0)
            self.assertLess(chunk_index, len(built.messages))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "c46.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "fixture-owner", 180)
                delivery = store.create_delivery(
                    "2026-08-24",
                    config_hash="fixture-config",
                    owner_id="fixture-owner",
                    state="collecting",
                )
                store.prepare_delivery(
                    delivery.delivery_id,
                    built.included_items,
                    built.messages,
                    owner_id="fixture-owner",
                    item_chunk_indexes=built.item_chunk_indexes,
                )
                persisted_items = {
                    str(row[0])
                    for row in store.connection.execute(
                        "SELECT fingerprint FROM delivery_items WHERE delivery_id=?",
                        (delivery.delivery_id,),
                    ).fetchall()
                }
                self.assertEqual(persisted_items, included_fingerprints)
                self.assertNotIn(oversized.fingerprint, persisted_items)
                payloads = [
                    str(row[0])
                    for row in store.connection.execute(
                        "SELECT payload FROM outbox_chunks WHERE delivery_id=? ORDER BY sequence",
                        (delivery.delivery_id,),
                    ).fetchall()
                ]
                self.assertEqual(len(payloads), len(built.messages))
                for payload in payloads:
                    self.assertNotIn("u" * 32, payload)

                message_seq = 100
                while True:
                    due = store.due_chunks(delivery.delivery_id)
                    if not due:
                        break
                    for chunk in due:
                        store.begin_chunk_attempt(chunk.chunk_id, run_id="fixture-run", owner_id="fixture-owner")
                        store.finish_chunk(
                            chunk.chunk_id,
                            "accepted",
                            run_id="fixture-run",
                            owner_id="fixture-owner",
                            telegram_message_id=str(message_seq),
                        )
                        message_seq += 1

                history = {
                    str(row[0])
                    for row in store.connection.execute(
                        "SELECT fingerprint FROM article_history WHERE delivery_id=?",
                        (delivery.delivery_id,),
                    ).fetchall()
                }
                self.assertEqual(history, included_fingerprints)
                self.assertNotIn(oversized.fingerprint, history)
                final = store.delivery(delivery.delivery_id)
                assert final is not None
                self.assertEqual(final.state, "completed")
                store.release_lease("delivery", "fixture-owner")


if __name__ == "__main__":
    unittest.main()
