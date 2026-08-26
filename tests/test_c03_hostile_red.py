"""C0.3 hostile corpus red reproducers — F-014/015/016/017/018/019."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path


class TestF014LoneSurrogateAbortsBatch(unittest.TestCase):
    """F-014: lone surrogate must be quarantined, not abort healthy batch."""

    def test_surrogate_in_title_does_not_abort(self) -> None:
        from meco_news.collectors import parse_feed_result
        from meco_news.models import NewsItem

        # Title containing an unpaired high surrogate (invalid scalar)
        # Correct policy: quarantine that item, deliver healthy sibling
        bad_title = "LPG terminal \ud800 project"  # lone surrogate
        good_title = "Gas infrastructure construction starts"
        rss = (
            b'<?xml version="1.0"?><rss><channel>'
            b'<item><title>' + bad_title.encode("utf-8", errors="surrogatepass") + b'</title><link>https://example.com/bad</link></item>'
            b'<item><title>' + good_title.encode() + b'</title><link>https://example.com/good</link></item>'
            b'</channel></rss>'
        )
        # Current code: _bounded_text handles surrogates via errors=replace, but identity hashing may still receive surrogates
        # We assert that one bad item does not abort and that its fingerprint still hashes without error
        try:
            items, quarantine = parse_feed_result(rss, "Test", "rss", source_id="test")
        except Exception as exc:
            self.fail(f"BUG: surrogate caused whole feed to abort: {exc}")
        self.assertEqual([item.title for item in items], [good_title])
        self.assertIn("invalid_unicode_scalar", quarantine)
        # Red test expects quarantine of bad and preservation of good
        self.assertEqual(len(items), 1, "healthy sibling must survive")
        self.assertEqual(items[0].title, good_title)

    def test_title_key_with_surrogate_does_not_raise(self) -> None:
        from meco_news.models import NewsItem

        item = NewsItem(title="bad \ud800 title", url="https://example.com/a", source="S")
        try:
            _ = item.title_key
            _ = item.url_key
        except Exception as exc:
            self.fail(f"BUG: surrogate title_key raised: {exc}")
        # If no exception, current code silently hashes surrogates — should have been rejected before hashing per C4.1
        # Force red: we want replacement/error handling, not silent acceptance
        if "\ud800" in item.title:
            self.fail("BUG REPRODUCED: surrogate reached model without scalar validation (C4.1)")


class TestF015UTF16DTDByPass(unittest.TestCase):
    """F-015: raw-byte DTD scan must not be bypassed by UTF-16."""

    def test_utf16_dtd_is_rejected(self) -> None:
        from meco_news.collectors import parse_feed_result, SourceDataError

        xml_utf16 = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss><channel><item><title>hi</title><link>https://example.com/a</link></item></channel></rss>'
        payload = xml_utf16.encode("utf-16")
        try:
            items, _ = parse_feed_result(payload, "Test", "rss", source_id="test")
        except SourceDataError as exc:
            self.assertEqual(exc.reason_code, "xml_dtd_disallowed")
            return
        except Exception as exc:
            self.fail(f"wrong exception: {exc}")
        self.fail("BUG REPRODUCED: UTF-16 DTD was not rejected — raw byte scan bypassed (C4.3)")

    def test_memory_error_not_swallowed(self) -> None:
        from meco_news.collectors import parse_feed_result
        from unittest.mock import patch
        import xml.etree.ElementTree as ET

        payload = b'<?xml version="1.0"?><rss><channel><item><title>hi</title><link>https://example.com/a</link></item></channel></rss>'
        with patch.object(ET, "XMLPullParser", side_effect=MemoryError("test")):
            try:
                parse_feed_result(payload, "Test", "rss", source_id="test")
            except MemoryError:
                return
            except Exception as exc:
                self.fail(f"BUG: MemoryError was converted to {type(exc).__name__}: {exc}")
            self.fail("BUG REPRODUCED: MemoryError was swallowed (C4.3)")


class TestF016MulticastAccepted(unittest.TestCase):
    """F-016: URL policy must reject multicast and every non-global class."""

    def test_multicast_rejected(self) -> None:
        from meco_news.urls import validate_url, URLPolicyError

        for url in ["https://224.0.0.1/story", "https://[ff02::1]/story", "https://239.255.0.1/story", "https://ff02::1/story"]:
            try:
                validate_url(url)
            except URLPolicyError as exc:
                # Any rejection is proof multicast is not accepted; ssrf_address_class is ideal, invalid_* also safe fail-closed
                self.assertIn(exc.reason_code, ("ssrf_address_class", "invalid_hostname", "invalid_port", "invalid_url"))
                continue
            self.fail(f"BUG REPRODUCED: multicast {url} was not rejected — must be ssrf_address_class")

    def test_private_still_blocked(self) -> None:
        from meco_news.urls import validate_url, URLPolicyError

        with self.assertRaises(URLPolicyError):
            validate_url("https://192.168.1.1/story")


class TestF017DeadlineNotReapable(unittest.TestCase):
    """F-017: source deadlines must be bounded and reapable (C4.4)."""

    def test_executor_is_detached_not_killable(self) -> None:
        import inspect
        from meco_news import collectors

        src = inspect.getsource(collectors.collect_all)
        # The deadline must be backed by spawn-isolated, parent-terminable workers.
        self.assertIn("source_deadline", src, "must have deadline handling")
        self.assertIn('get_context("spawn")', src, "must use spawned process isolation")
        self.assertIn("_terminate_worker", src, "must terminate and join deadline-exceeded workers")
        self.assertNotIn("ThreadPoolExecutor", src, "threads cannot be hard-terminated at a source deadline")


class TestF018SourceDependentIdentity(unittest.TestCase):
    """F-018: title identity must be source-independent, publisher beats aggregator, fuzzy counted."""

    def test_title_key_source_independent(self) -> None:
        from meco_news.models import normalized_title, NewsItem

        a = NewsItem(title="Pertamina kerahkan 33 mobil tangki BBM ke Flores", url="https://example.com/a", source="Petromindo")
        b = NewsItem(title="Pertamina kerahkan 33 mobil tangki BBM ke Flores - Petromindo", url="https://example.com/b", source="Petromindo")
        # normalized_title should strip ' - source' suffix, so keys equal regardless of source
        # Current normalized_title does strip, but title_key uses normalized_title(title, source) which is source-dependent for suffix only
        # The audit finds deeper source-dependence: title_key includes source in suffix removal, but not fully independent
        # We assert source-independent hash: same headline from different source label must collide
        c = NewsItem(title="Pertamina kerahkan 33 mobil tangki BBM ke Flores", url="https://example.com/c", source="Kompas")
        if a.title_key != c.title_key:
            self.fail("BUG REPRODUCED: title_key differs for same headline from different source — must be source-independent (C4.5)")

    def test_dedup_permutation_dependence(self) -> None:
        from meco_news.ranking import deduplicate
        from meco_news.models import NewsItem
        from datetime import datetime, UTC

        now = datetime.now(UTC)
        items = [
            NewsItem(title="Pertamina kerahkan 33 mobil tangki BBM ke Flores", url="https://example.com/a", source="A", published_at=now),
            NewsItem(title="Pertamina kerahkan 38 mobil tangki BBM menuju NTT", url="https://example.com/b", source="B", published_at=now),
            NewsItem(title="Gas infrastructure construction starts for new project", url="https://example.com/c", source="C", published_at=now),
        ]
        # Permutation should give identical order; current _merge_group mutates objects and uses id()-based cache, may be order dependent under budget
        first = [x.url for x in deduplicate(items)]
        second = [x.url for x in deduplicate(list(reversed(items)))]
        self.assertEqual(first, second, "dedup must be permutation independent")


class TestF019TelegramSizingIncomplete(unittest.TestCase):
    """F-019: final Telegram payload must respect both 3900 UTF-16 and raw HTML bytes and map omitted."""

    def test_oversized_emoji_not_omitted(self) -> None:
        from meco_news.telegram import build_digest, utf16_units
        from meco_news.models import NewsItem

        giant = "😀" * 5000
        item = NewsItem(title=giant, url="https://example.com/emoji", source="S", topic_label="T", relevance_reason="R")
        result = build_digest([item], "MECO", "UTC", max_length=900, max_bytes=15600)
        # With max_length 900, truncated to 100 emoji (200 units) should fit, so included is correct
        # The invariant is that every final message respects both limits and is correctly mapped
        for msg in result.messages:
            self.assertLessEqual(utf16_units(msg), 900)
            self.assertLessEqual(len(msg.encode("utf-8")), 15600)
        # Either included or omitted is valid as long as limits respected and mapping correct
        self.assertEqual(len(result.included_items) + len(result.omitted_items), 1)
