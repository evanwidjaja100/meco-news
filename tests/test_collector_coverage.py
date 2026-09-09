from __future__ import annotations

from datetime import datetime, UTC
import pickle
from queue import Empty
import threading
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import meco_news.collectors as collectors
from meco_news.collectors import SourceDataError, SourceResult
from meco_news.config import CollectionLimits, NetworkPolicy, load_config
from meco_news.models import NewsItem
from meco_news.network import NetworkError
from meco_news.urls import URLPolicyError


RSS = b"""<?xml version='1.0'?><rss><channel><title>Feed</title>
<item><title>Industrial tank project</title><link>https://example.com/a?utm_source=x</link>
<description>Tank &amp; vessel &lt;b&gt;project&lt;/b&gt;</description><pubDate>Mon, 24 Aug 2026 07:32:32 +0700</pubDate>
<source url='https://publisher.example/feed'>Publisher</source></item>
</channel></rss>"""
ATOM = b"""<feed xmlns='http://www.w3.org/2005/Atom'><title>Atom</title>
<entry><title>Atom project</title><link rel='alternate' href='https://example.com/atom'/>
<summary>Summary</summary><updated>2026-08-24T00:00:00Z</updated></entry></feed>"""


def _source_item() -> NewsItem:
    return NewsItem(title="Industrial tank", url="https://example.com/tank", source="Source", published_at=datetime.now(UTC))


class CollectorParsingCorpusTests(unittest.TestCase):
    def test_config_adapters_and_xml_edge_paths(self) -> None:
        config = load_config("config/watchlist.json")
        self.assertIs(config.limits, collectors._limits(config))
        self.assertIs(config.network_policy, collectors._network_policy(config))
        self.assertEqual(collectors._limits(None).response_bytes, CollectionLimits().response_bytes)
        self.assertEqual(collectors._limits({"limits": {"response_bytes": 10}}).response_bytes, 10)
        self.assertEqual(collectors._limits({"limits": {"response_bytes": "bad"}}).response_bytes, "bad")
        self.assertEqual(collectors._limits({"limits": "bad"}).response_bytes, CollectionLimits().response_bytes)
        self.assertEqual(collectors._network_policy({"network_policy": {"require_https": False}}).require_https, False)
        self.assertEqual(collectors._network_policy({"network_policy": "bad"}), NetworkPolicy())
        self.assertEqual(collectors._network_policy(None), NetworkPolicy())

        self.assertIsNone(collectors._parse_date(None))
        self.assertIsNotNone(collectors._parse_date("2026-08-24T00:00:00"))
        self.assertEqual(collectors._bounded_text("&#xD800;", 20), ("", True))
        node = ET.fromstring(
            "<entry><title>title</title><guid>https://example.com/guid</guid>"
            "<link rel='preview' href='https://example.com/no'/><source url='bad'>Publisher</source></entry>"
        )
        values = collectors._entry_values(node, CollectionLimits())
        self.assertEqual(values[1], "https://example.com/guid")
        self.assertEqual(values[2:], ("Publisher", "bad", "", ""))

    def test_parse_rss_atom_quarantine_and_scalar_paths(self) -> None:
        items, quarantine = collectors.parse_feed_result(RSS, "Feed", "rss", source_id="rss-1")
        self.assertEqual(len(items), 1)
        self.assertEqual(quarantine, [])
        self.assertEqual(items[0].source_url, "https://publisher.example/feed")
        atom, _ = collectors.parse_feed_result(ATOM, "Atom", "atom", source_id="atom-1")
        self.assertEqual(atom[0].url, "https://example.com/atom")
        invalid = b"<rss><channel><item><title>Bad URL</title><link>not-a-url</link></item><item><link>https://example.com/no-title</link></item></channel></rss>"
        values, quarantine = collectors.parse_feed_result(invalid, "Feed", "rss", source_id="bad")
        self.assertEqual(values, [])
        self.assertEqual(sorted(quarantine), ["invalid_url", "missing_title"])
        self.assertEqual(collectors._local_name("{urn:test}Title"), "title")
        self.assertTrue(collectors._is_invalid_scalar("bad\ufffd"))
        self.assertFalse(collectors._is_invalid_scalar("safe"))
        cleaned, was_truncated = collectors._bounded_text("x" * 100, 10)
        self.assertTrue(was_truncated)
        self.assertTrue(cleaned.endswith("..."))
        self.assertEqual(collectors._bounded_text(1, 10), ("", False))
        self.assertIsNone(collectors._parse_date("not a date"))
        self.assertIsNotNone(collectors._parse_date("2026-08-24T00:00:00Z"))
        self.assertEqual(collectors._clean_html("<b>x</b>"), "x")

    def test_parse_limits_dtd_repair_and_fallback(self) -> None:
        with self.assertRaises(SourceDataError) as error:
            collectors.parse_feed_result(b"<!DOCTYPE rss><rss/>", "x", "rss")
        self.assertEqual(error.exception.reason_code, "xml_dtd_disallowed")
        limits = CollectionLimits(response_bytes=1024, xml_depth=2, xml_nodes=20, entries_per_source=1)
        with self.assertRaises(SourceDataError) as error:
            collectors.parse_feed_result(b"<rss><channel><item><title>x</title></item></channel></rss>", "x", "rss", limits=limits)
        self.assertIn(error.exception.reason_code, {"xml_depth_limit", "entry_limit"})
        with self.assertRaises(SourceDataError):
            collectors.parse_feed_result(b"x" * 1025, "x", "rss", limits=limits)
        self.assertIn(b"&amp;", collectors._repair_xml(b"<rss>& bad</rss>", CollectionLimits()))
        with self.assertRaises(SourceDataError):
            collectors._repair_xml(b"x" * 1025, limits)
        with self.assertRaises(SourceDataError):
            collectors.parse_feed(b"<rss><channel><item><title>x", "x", "rss")
        with self.assertRaises(SourceDataError):
            collectors.parse_feed("not-bytes", "x", "rss")
        with self.assertRaises(SourceDataError):
            collectors.parse_feed_result("not-bytes", "x", "rss")
        self.assertEqual(collectors._entry_values(__import__("xml.etree.ElementTree", fromlist=["Element"]).Element("item"), CollectionLimits())[0], "")

    def test_xml_scalar_and_provenance_quarantine_paths(self) -> None:
        invalid_utf8 = b"<rss><channel><item><title>Good</title><link>https://example.com/a</link><description>\xff</description></item></channel></rss>"
        values, quarantine = collectors.parse_feed_result(invalid_utf8, "Feed", "rss")
        self.assertEqual(values, [])
        self.assertIn("invalid_unicode_scalar", quarantine)
        source_bad = b"<rss><channel><item><title>Good</title><link>https://example.com/a</link><source url='not-a-url'>Publisher</source></item></channel></rss>"
        values, _ = collectors.parse_feed_result(source_bad, "Feed", "rss")
        self.assertEqual(values[0].source_url, "")
        long_source = b"<rss><channel><item><title>Good</title><link>https://example.com/a</link><source>" + b"x" * 300 + b"</source></item></channel></rss>"
        values, _ = collectors.parse_feed_result(long_source, "Feed", "rss")
        self.assertTrue(values[0].source.endswith("..."))
        invalid_scalar = "<rss><channel><item><title>bad\ud800</title><link>https://example.com/a</link></item></channel></rss>".encode("utf-8", "surrogatepass")
        values, quarantine = collectors.parse_feed_result(invalid_scalar, "Feed", "rss")
        self.assertEqual(values, [])
        self.assertIn("invalid_unicode_scalar", quarantine)

    def test_source_collectors_success_quarantine_and_failures(self) -> None:
        feed = {"id": "rss", "name": "RSS", "url": "https://example.com/feed"}
        with patch("meco_news.collectors._fetch", return_value=RSS):
            result = collectors._collect_rss(feed, 2)
        self.assertEqual(result.outcome, "succeeded")
        with patch("meco_news.collectors._fetch", side_effect=NetworkError("http_5xx")):
            failed = collectors._collect_rss(feed, 2)
        self.assertEqual(failed.reason_code, "http_5xx")
        all_bad = b"<rss><channel><item><title></title><link>bad</link></item></channel></rss>"
        with patch("meco_news.collectors._fetch", return_value=all_bad):
            quarantined = collectors._collect_rss(feed, 2)
        self.assertEqual(quarantined.reason_code, "all_items_quarantined")
        with patch("meco_news.collectors._fetch", side_effect=MemoryError), self.assertRaises(MemoryError):
            collectors._collect_rss(feed, 2)
        obj_feed = type("Feed", (), {"id": "obj", "name": "Object", "url": "https://example.com"})()
        self.assertEqual(collectors._feed_dict(obj_feed)["id"], "obj")
        self.assertEqual(collectors._feed_dict({"name": "name"})["id"], "name")
        with self.assertRaises(SourceDataError):
            collectors._feed_dict([])

    def test_worker_entry_and_frame_failure_boundaries(self) -> None:
        class Connection:
            def __init__(self) -> None:
                self.frames: list[bytes] = []
                self.closed = False

            def send_bytes(self, frame: bytes) -> None:
                self.frames.append(frame)

            def close(self) -> None:
                self.closed = True

        connection = Connection()
        collectors._source_process_entry(connection, _good_worker, (), {}, 4096)
        self.assertTrue(connection.closed)
        self.assertEqual(pickle.loads(connection.frames[0])[0], "result")
        connection = Connection()
        collectors._source_process_entry(connection, _raise_worker, (), {}, 4096)
        self.assertEqual(pickle.loads(connection.frames[0])[0], "exception")
        connection = Connection()
        collectors._source_process_entry(connection, _memory_worker, (), {}, 4096)
        self.assertEqual(pickle.loads(connection.frames[0])[0], "memory_error")
        connection = Connection()
        collectors._send_ipc_frame(connection, ("x", object()), 8)
        self.assertEqual(pickle.loads(connection.frames[0])[0], "exception")

        with patch.object(collectors.pickle, "dumps", side_effect=[MemoryError("cannot pickle"), b"fallback"]):
            connection = Connection()
            collectors._send_ipc_frame(connection, ("x",), 4096)
            self.assertEqual(connection.frames, [b"fallback"])

    def test_gdelt_source_schema_and_quarantine(self) -> None:
        query = {"id": "q", "name": "Query", "query": "tank"}
        gdelt = {"max_records": 10, "timespan": "2d"}
        payload = {"articles": [{"title": "Tank project", "url": "https://example.com/a", "domain": "example.com", "seendate": "20260824T000000Z"}, {"title": "", "url": "bad"}, 1]}
        with patch("meco_news.collectors._fetch", return_value=__import__("json").dumps(payload).encode()):
            result = collectors._collect_gdelt(query, gdelt, 2)
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.quarantined_count, 2)
        for body, reason in ((b"bad", "json_parse_error"), (b'{"wrong":[]}', "json_schema_invalid")):
            with patch("meco_news.collectors._fetch", return_value=body):
                failed = collectors._collect_gdelt(query, gdelt, 2)
            self.assertEqual(failed.reason_code, reason)
        with patch("meco_news.collectors._fetch", side_effect=URLPolicyError("dns_resolution_failed")):
            failed = collectors._collect_gdelt(query, gdelt, 2)
        self.assertEqual(failed.reason_code, "dns_resolution_failed")
        self.assertEqual(collectors._google_news_url("tank", {"locale": "en", "country": "US", "edition": "US:en"}, 3).split("?", 1)[0], "https://news.google.com/rss/search")
        self.assertEqual(collectors._job_host(collectors._collect_gdelt, (query, gdelt, 1), "q"), "api.gdeltproject.org")
        self.assertEqual(collectors._job_host(collectors._collect_rss, ({"url": "https://Example.com/a"}, 1), "q"), "example.com")
        self.assertEqual(collectors._job_host(collectors._collect_rss, ({"url": "%%%"}, 1), "fallback"), "fallback")


class _PipeEnd:
    def __init__(self) -> None:
        self.peer: _PipeEnd | None = None
        self.frames: list[bytes] = []
        self.closed = False

    def send_bytes(self, data: bytes) -> None:
        assert self.peer is not None
        self.peer.frames.append(data)

    def poll(self) -> bool:
        return bool(self.frames)

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        data = self.frames.pop(0)
        if maxlength is not None and len(data) > maxlength:
            raise OSError("frame too large")
        return data

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, target, args, *, name: str = "", alive: bool = False, start_error: BaseException | None = None) -> None:
        self.target = target
        self.args = args
        self.alive = alive
        self.start_error = start_error
        self.started = False
        self.name = name

    def start(self) -> None:
        if self.start_error:
            raise self.start_error
        self.started = True
        if not self.alive:
            self.target(*self.args)

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        return None

    def terminate(self) -> None:
        self.alive = False

    def kill(self) -> None:
        self.alive = False


class _FakeContext:
    def __init__(self, *, alive: bool = False, start_error: BaseException | None = None) -> None:
        self.alive = alive
        self.start_error = start_error
        self.processes: list[_FakeProcess] = []

    def Pipe(self, duplex: bool = False):
        parent, child = _PipeEnd(), _PipeEnd()
        parent.peer = child
        child.peer = parent
        return parent, child

    def Process(self, *, target, args, name):
        process = _FakeProcess(target, args, name=name, alive=self.alive, start_error=self.start_error)
        self.processes.append(process)
        return process


def _good_worker(*_args, **_kwargs) -> SourceResult:
    return SourceResult("worker", "Worker", "succeeded", items=[_source_item()], accepted_count=1)


def _raise_worker(*_args, **_kwargs) -> SourceResult:
    raise RuntimeError("worker boom")


def _memory_worker(*_args, **_kwargs) -> SourceResult:
    raise MemoryError("worker memory")


class CollectorSupervisorCorpusTests(unittest.TestCase):
    def _config(self) -> dict[str, object]:
        return {
            "rss_feeds": [{"id": "rss", "name": "RSS", "url": "https://example.com/feed"}],
            "google_news": {"enabled": False, "queries": []},
            "gdelt": {"enabled": False, "queries": []},
            "request_timeout_seconds": 2,
            "limits": {"source_deadline_seconds": 1, "cycle_deadline_seconds": 2, "ipc_frame_bytes": 1024 * 1024},
        }

    def test_supervisor_reaps_success_and_worker_failure(self) -> None:
        config = self._config()
        context = _FakeContext()
        with patch("meco_news.collectors.mp.get_context", return_value=context), patch("meco_news.collectors._fetch", return_value=RSS):
            result = collectors.collect_all(config)
        self.assertEqual(result.successful_sources, 1)
        self.assertFalse(any(process.is_alive() for process in context.processes))
        failing_context = _FakeContext()
        with patch("meco_news.collectors.mp.get_context", return_value=failing_context), patch("meco_news.collectors._collect_rss", _raise_worker):
            result = collectors.collect_all(config)
        self.assertEqual(result.failed_sources, 1)
        self.assertEqual(result.source_results[0].reason_code, "source_exception")

    def test_supervisor_handles_invalid_partial_memory_and_start_failure(self) -> None:
        config = self._config()
        for replacement, _reason in ((_memory_worker, "memory"),):
            context = _FakeContext()
            with patch("meco_news.collectors.mp.get_context", return_value=context), patch("meco_news.collectors._collect_rss", replacement), self.assertRaises(MemoryError):
                collectors.collect_all(config)
        for sender in (
            lambda connection, *_args: connection.send_bytes(b"x" * (1024 * 1024 + 1)),
            lambda connection, *_args: None,
            lambda connection, *_args: connection.send_bytes(pickle.dumps(("invalid",))),
        ):
            context = _FakeContext()
            with patch("meco_news.collectors.mp.get_context", return_value=context), patch("meco_news.collectors._source_process_entry", sender):
                result = collectors.collect_all(config)
            self.assertEqual(result.failed_sources, 1)
        context = _FakeContext(start_error=OSError("cannot start"))
        with patch("meco_news.collectors.mp.get_context", return_value=context):
            result = collectors.collect_all(config)
        self.assertEqual(result.source_results[0].reason_code, "process_start_failed")

    def test_supervisor_cycle_timeout_and_empty_jobs(self) -> None:
        empty = {"rss_feeds": [], "google_news": {"enabled": False}, "gdelt": {"enabled": False}}
        result = collectors.collect_all(empty)
        self.assertEqual(result.source_results, [])
        config = self._config()
        config["limits"] = {"source_deadline_seconds": 5, "cycle_deadline_seconds": 1, "ipc_frame_bytes": 1024}
        context = _FakeContext(alive=True)
        with patch("meco_news.collectors.mp.get_context", return_value=context):
            result = collectors.collect_all(config)
        self.assertEqual(result.source_results[0].reason_code, "cycle_deadline_exceeded")
        self.assertFalse(context.processes[0].is_alive())

    def test_supervisor_builds_google_and_gdelt_jobs_from_mapping_config(self) -> None:
        config = {
            "rss_feeds": [],
            "google_news": {
                "enabled": True,
                "locale": "en",
                "country": "US",
                "edition": "US:en",
                "queries": [{"id": "google", "name": "Google", "query": "tank"}],
            },
            "gdelt": {
                "enabled": True,
                "queries": [{"id": "gdelt", "name": "GDELT", "query": "tank"}],
                "max_records": 5,
                "timespan": "1d",
            },
            "request_timeout_seconds": 1,
            "lookback_days": 2,
            "limits": {"max_sources": 5, "source_deadline_seconds": 1, "cycle_deadline_seconds": 2, "ipc_frame_bytes": 4096},
        }

        def sender(connection, *_args):
            connection.send_bytes(pickle.dumps(("result", SourceResult("worker", "Worker", "succeeded"))))

        context = _FakeContext()
        with patch("meco_news.collectors.mp.get_context", return_value=context), patch(
            "meco_news.collectors._source_process_entry", sender
        ):
            result = collectors.collect_all(config)
        self.assertEqual(len(result.source_results), 2)
        self.assertEqual({process.name for process in context.processes}, {"meco-source-gdelt-gdelt", "meco-source-google-google"})

    def test_supervisor_terminates_live_then_kills_unreaped_worker(self) -> None:
        process = _FakeProcess(lambda: None, (), alive=True)
        collectors._terminate_worker(process)
        self.assertFalse(process.is_alive())

        class StuckProcess(_FakeProcess):
            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        stuck = StuckProcess(lambda: None, (), alive=True)
        with self.assertRaises(RuntimeError):
            collectors._terminate_worker(stuck)

    def test_ipc_receiver_keeps_partial_frame_off_supervisor_thread(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingConnection:
            def recv_bytes(self, *, maxlength: int) -> bytes:
                self.maxlength = maxlength
                started.set()
                release.wait(1)
                return b"complete-frame"

        connection = BlockingConnection()
        receiver = collectors._start_ipc_receiver(connection, 128)
        self.assertTrue(started.wait(1))
        began = time.monotonic()
        with self.assertRaises(Empty):
            receiver.get(timeout=0.05)
        self.assertLess(time.monotonic() - began, 0.25)
        release.set()
        self.assertEqual(receiver.get(timeout=1), ("frame", b"complete-frame"))
        self.assertEqual(connection.maxlength, 128)


if __name__ == "__main__":
    unittest.main()
