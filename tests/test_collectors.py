from __future__ import annotations

import unittest

from meco_news.collectors import parse_feed


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item>
  <title>Gas infrastructure construction starts in Indonesia</title>
  <link>https://example.com/story?utm_source=rss</link>
  <description><![CDATA[New tanks &amp; processing equipment are planned.]]></description>
  <source url="https://publisher.example">Publisher</source>
  <pubDate>Mon, 24 Aug 2026 07:32:32 +0700</pubDate>
</item></channel></rss>"""


class FeedParserTests(unittest.TestCase):
    def test_parses_rss_metadata(self) -> None:
        items = parse_feed(RSS, "Example feed", "rss")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "Publisher")
        self.assertEqual(items[0].published_at.isoformat(), "2026-08-24T00:32:32+00:00")
        self.assertIn("processing equipment", items[0].summary)

    def test_repairs_bare_ampersand(self) -> None:
        broken = RSS.replace(b"New tanks &amp; processing", b"New tanks & processing")
        items = parse_feed(broken, "Example feed", "rss")
        self.assertEqual(len(items), 1)
        self.assertIn("tanks & processing", items[0].summary)


if __name__ == "__main__":
    unittest.main()
