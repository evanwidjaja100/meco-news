from __future__ import annotations

from datetime import datetime, UTC
import unittest

from meco_news.models import NewsItem
from meco_news.telegram import build_digest_messages


class TelegramFormattingTests(unittest.TestCase):
    def test_escapes_html_and_splits_on_article_boundaries(self) -> None:
        items = [
            NewsItem(
                title=f"Tank & vessel project <{index}>",
                url=f"https://example.com/story/{index}?a=1&b=2",
                source="Publisher & Co",
                published_at=datetime(2026, 8, 24, tzinfo=UTC),
                topic_label="Process equipment",
                relevance_reason="Potential tank & vessel demand.",
                summary="A" * 240,
            )
            for index in range(8)
        ]
        messages = build_digest_messages(items, "PT Meco Inoxprima", "Asia/Jakarta", max_length=900)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 900 for message in messages))
        self.assertIn("Tank &amp; vessel", messages[0])
        self.assertNotIn("<0>", messages[0])


if __name__ == "__main__":
    unittest.main()
