"""C4.1 scalar proof: hostile Unicode is quarantined/stripped, healthy text survives."""

from __future__ import annotations

import unittest

from meco_news.collectors import parse_feed_result

_BIDI = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f"


def _feed(*items: str) -> bytes:
    return ('<?xml version="1.0"?><rss><channel>' + "".join(items) + "</channel></rss>").encode("utf-8")


def _item(title: str, link: str, summary: str = "") -> str:
    return f"<item><title>{title}</title><link>{link}</link><description>{summary}</description></item>"


class ScalarBoundaryTests(unittest.TestCase):
    def test_bidi_overrides_stripped_from_title_and_summary(self) -> None:
        title = "Market update \u202eEVIL\u202c normal"
        summary = "Summary \u2066isolated\u2069 text"
        items, quarantine = parse_feed_result(_feed(_item(title, "https://example.com/a", summary)), "Test", "rss", source_id="test")
        self.assertEqual(quarantine, [])
        self.assertEqual(len(items), 1)
        for ch in items[0].title + items[0].summary:
            self.assertNotIn(ch, _BIDI)
        self.assertIn("EVIL", items[0].title)
        self.assertIn("isolated", items[0].summary)

    def test_lone_surrogate_in_each_field_quarantines_with_healthy_sibling(self) -> None:
        good = _item("Healthy sibling", "https://example.com/good", "All well")
        cases = (
            _item("Bad \ud800 title", "https://example.com/b1", "ok"),
            _item("Bad link", "https://example.com/\ud800bad", "ok"),
            _item("Bad summary", "https://example.com/b3", "Bad \udc00 summary"),
        )
        for bad in cases:
            with self.subTest(bad=bad[:40]):
                payload = (
                    _feed(bad, good).encode("utf-8", errors="surrogatepass")
                    if False
                    else ('<?xml version="1.0"?><rss><channel>' + bad + good + "</channel></rss>").encode("utf-8", errors="surrogatepass")
                )
                items, quarantine = parse_feed_result(payload, "Test", "rss", source_id="test")
                self.assertEqual([i.title for i in items], ["Healthy sibling"])
                self.assertIn("invalid_unicode_scalar", quarantine)

    def test_valid_astral_emoji_and_combining_marks_preserved(self) -> None:
        title = "Gas terminal \U0001f3ed opens with caf\u00e9 and na\u0308ive demand"
        items, quarantine = parse_feed_result(_feed(_item(title, "https://example.com/a")), "Test", "rss", source_id="test")
        self.assertEqual(quarantine, [])
        self.assertEqual(len(items), 1)
        self.assertIn("\U0001f3ed", items[0].title)
        self.assertIn("caf\u00e9", items[0].title)

    def test_c0_c1_controls_stripped(self) -> None:
        # DEL and NEL are legal XML but prohibited in output; NUL/SOH are
        # not legal XML at all, so a document containing them fails closed.
        items, _ = parse_feed_result(_feed(_item("Hello\x7fWorld\x85!", "https://example.com/a")), "Test", "rss", source_id="test")
        self.assertEqual(len(items), 1)
        self.assertNotIn("\x7f", items[0].title)
        self.assertNotIn("\x85", items[0].title)
        self.assertIn("Hello", items[0].title)
        self.assertIn("World", items[0].title)

    def test_illegal_xml_control_fails_closed(self) -> None:
        from meco_news.collectors import SourceDataError

        with self.assertRaises(SourceDataError):
            parse_feed_result(_feed(_item("Hello\x01World!", "https://example.com/a")), "Test", "rss", source_id="test")


if __name__ == "__main__":
    unittest.main()
