"""C4.3 DTD proof: entity/DTD rejection holds across XML encodings."""

from __future__ import annotations

import unittest

from meco_news.collectors import SourceDataError, parse_feed_result

_DOCTYPE_FEED = (
    '<?xml version="1.0" encoding="{decl}"?>'
    '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    "<rss><channel><item><title>hi</title><link>https://example.com/a</link></item></channel></rss>"
)
_ENTITY_ONLY_FEED = (
    '<?xml version="1.0" encoding="{decl}"?>'
    "<rss><channel><item><title>&xxe;</title><link>https://example.com/a</link></item></channel></rss>"
    '<!ENTITY xxe "expanded">'
)
_HEALTHY_FEED = '<?xml version="1.0"?><rss><channel><item><title>hi</title><link>https://example.com/a</link></item></channel></rss>'

# (codec, declaration) pairs.  The raw-byte scan only sees ASCII-compatible
# bytes; the decode loop in _parse_xml_once must catch the rest before the
# parser can expand anything.
DOCTYPE_CASES = (
    ("utf-8", "UTF-8"),
    ("utf-8-sig", "UTF-8"),
    ("utf-16", "UTF-16"),
    ("utf-16-le", "UTF-16"),
    ("utf-16-be", "UTF-16"),
    ("utf-32", "UTF-32"),
    ("utf-32-le", "UTF-32"),
    ("utf-32-be", "UTF-32"),
)


class DtdEncodingTests(unittest.TestCase):
    def test_doctype_rejected_in_every_scanned_encoding(self) -> None:
        for codec, decl in DOCTYPE_CASES:
            with self.subTest(codec=codec):
                payload = _DOCTYPE_FEED.format(decl=decl).encode(codec)
                with self.assertRaises(SourceDataError) as error:
                    parse_feed_result(payload, "Test", "rss", source_id="test")
                self.assertEqual(error.exception.reason_code, "xml_dtd_disallowed")

    def test_entity_reference_rejected_without_doctype(self) -> None:
        for codec, decl in (("utf-8", "UTF-8"), ("utf-16-le", "UTF-16"), ("utf-32-be", "UTF-32")):
            with self.subTest(codec=codec):
                payload = _ENTITY_ONLY_FEED.format(decl=decl).encode(codec)
                with self.assertRaises(SourceDataError) as error:
                    parse_feed_result(payload, "Test", "rss", source_id="test")
                self.assertEqual(error.exception.reason_code, "xml_dtd_disallowed")

    def test_healthy_feed_still_parses(self) -> None:
        items, _ = parse_feed_result(_HEALTHY_FEED.encode("utf-8"), "Test", "rss", source_id="test")
        self.assertEqual(len(items), 1)


if __name__ == "__main__":
    unittest.main()
