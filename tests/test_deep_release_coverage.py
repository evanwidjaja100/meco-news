"""Additional behavior-level coverage for defensive release paths.

The tests in this module exercise private helpers only where they represent a
real production boundary (configuration parsing, transport classification,
rendering, locking, and durable state transitions).  They use fakes and
temporary files; no external service or production state is touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from http.client import IncompleteRead
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import meco_news.config as config_module
import meco_news.maintenance as maintenance_module
import meco_news.network as network_module
import meco_news.preflight as preflight_module
import meco_news.telegram as telegram_module
import meco_news.urls as urls_module
from meco_news.config import ConfigurationError, CollectionLimits, NetworkPolicy, load_config
from meco_news.models import NewsItem
from meco_news.network import BoundedHTTPClient, NetworkError, ResponseTooLarge
from meco_news.maintenance import MaintenanceBusy, MaintenanceContext, MaintenanceError, RuntimeContext, _maintenance_verify, _pid_alive
from meco_news.ranking import _contains, _merge_group, _topic_value, deduplicate_with_stats, rank_item, select_digest
from meco_news.telegram import TelegramClient, TelegramSendError, _TelegramHTMLParser, build_digest, validate_message
from meco_news.urls import URLPolicyError, canonical_url, sanitize_url_for_log, validate_resolved_addresses, validate_url
from meco_news.inspection import WalProbeResult


ROOT = Path(__file__).resolve().parents[1]


def _base_config() -> dict[str, object]:
    return json.loads((ROOT / "config" / "watchlist.json").read_text(encoding="utf-8"))


def _item(
    title: str = "LPG terminal expansion",
    url: str = "https://example.com/story",
    *,
    source: str = "Example Publisher",
    published_at: datetime | None = None,
    score: int = 12,
    topic: str = "lpg_energy",
) -> NewsItem:
    return NewsItem(
        title=title,
        url=url,
        source=source,
        published_at=published_at if published_at is not None else datetime.now(UTC),
        summary="Indonesia project expansion for industrial storage equipment.",
        score=score,
        topic=topic,
        topic_label="LPG storage",
        relevance_reason="Potential equipment demand.",
        source_host="example.com",
    )


class ConfigurationBoundaryTests(unittest.TestCase):
    def test_primitive_helpers_and_mapping_contract(self) -> None:
        frozen = config_module._freeze({"a": [1, {"b": 2}], "t": (3,)})
        self.assertEqual(config_module._thaw(frozen), {"a": [1, {"b": 2}], "t": [3]})
        with self.assertRaises(ConfigurationError):
            config_module._ensure_keys({"unknown": 1}, {"known"}, "fixture")
        self.assertEqual(config_module._string("  value  ", "field"), "value")
        for value in (1, "", "x" * 5):
            with self.subTest(value=repr(value)), self.assertRaises(ConfigurationError):
                config_module._string(value, "field", max_chars=4)
        with self.assertRaises(ConfigurationError):
            config_module._integer(True, "number", 0, 10)
        with self.assertRaises(ConfigurationError):
            config_module._integer(11, "number", 0, 10)
        with self.assertRaises(ConfigurationError):
            config_module._boolean(1, "flag")
        self.assertEqual(config_module._slug("  A/B  "), "a-b")
        self.assertEqual(config_module._slug("!!!"), "source")
        self.assertEqual(config_module._list_of_strings([" a ", "b"], "words"), ["a", "b"])
        for value in ("bad", ["x" * 5]):
            with self.subTest(value=repr(value)), self.assertRaises(ConfigurationError):
                config_module._list_of_strings(value, "words", max_chars=4)
        config = load_config(ROOT / "config" / "watchlist.json")
        self.assertEqual(tuple(iter(config)), tuple(config.as_dict()))
        self.assertEqual(len(config), len(config.as_dict()))
        self.assertEqual(config.topics, config.topics_typed)
        self.assertEqual(config.rss_feeds, config.rss_typed)
        self.assertEqual(config.get("missing", "fallback"), "fallback")
        self.assertEqual(config["company"], "PT Meco Inoxprima")
        self.assertEqual(config.config_hash, config.config_hash)
        self.assertNotIn("token", json.dumps(config.redacted()).casefold())

    def test_load_dotenv_is_non_overwriting_and_tolerates_comments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("\ufeff# comment\nNO_EQUALS\nA=one\nB='two'\nC=\"three\"\n", encoding="utf-8")
            with patch.dict(os.environ, {"A": "existing"}, clear=False):
                config_module.load_dotenv(path)
                self.assertEqual(os.environ["A"], "existing")
                self.assertEqual(os.environ["B"], "two")
                self.assertEqual(os.environ["C"], "three")
            config_module.load_dotenv(Path(directory) / "absent.env")

    def test_parse_and_load_config_rejects_each_structural_boundary(self) -> None:
        invalid_cases: list[tuple[str, object]] = [
            ("root", []),
            ("unknown top-level", {"unexpected": 1}),
            ("company type", {"company": 1}),
            ("timezone", {"timezone": "Mars/Phobos"}),
            ("time", {"delivery_time": "25:99"}),
            ("daily order", {"daily_min": 8}),
            ("fallback", {"fallback_score": 10}),
            ("source timeout", {"request_timeout_seconds": 36}),
            ("cycle deadline", {"limits": {"cycle_deadline_seconds": 10}}),
            ("lease deadline", {"lease_ttl_seconds": 50}),
            ("missing date", {"missing_date_policy": "maybe"}),
            ("trusted hostname", {"trusted_domains": ["example.com/path"]}),
            ("topics empty", {"topics": []}),
            ("topics scalar", {"topics": [1]}),
            ("duplicate topic", {"topics": [{"id": "x", "label": "x", "why": "x", "strong_terms": [], "keywords": [], "requires_any": []}, {"id": "x", "label": "x", "why": "x", "strong_terms": [], "keywords": [], "requires_any": []}]}),
            ("feeds empty", {"rss_feeds": []}),
            ("feed scalar", {"rss_feeds": [1]}),
            ("feed http", {"rss_feeds": [{"name": "x", "url": "http://example.com"}]}),
            ("feed invalid url", {"rss_feeds": [{"name": "x", "url": "https://[bad]"}]}),
            ("google scalar", {"google_news": []}),
            ("google enabled", {"google_news": {"enabled": 1, "queries": []}}),
            ("google query scalar", {"google_news": {"queries": [1]}}),
            ("gdelt scalar", {"gdelt": []}),
            ("gdelt query scalar", {"gdelt": {"queries": [1]}}),
            ("network scalar", {"network_policy": []}),
            ("network host", {"network_policy": {"allowed_redirect_hosts": ["bad/host"]}}),
            ("retry scalar", {"retry_policy": []}),
            ("retry order", {"retry_policy": {"base_delay_seconds": 10, "max_delay_seconds": 1}}),
        ]
        for label, mutation in invalid_cases:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                if label == "root":
                    payload: object = mutation
                else:
                    payload = _base_config()
                    assert isinstance(payload, dict)
                    payload.update(mutation)  # type: ignore[arg-type]
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(label=label), self.assertRaises(ConfigurationError):
                    load_config(path)

    def test_optional_sources_and_boundary_values_are_normalized(self) -> None:
        raw = _base_config()
        assert isinstance(raw, dict)
        raw["google_news"] = {"enabled": False, "queries": []}
        raw["gdelt"] = {"enabled": True, "queries": [{"name": "Q", "query": "gas"}], "timespan": "1d", "max_records": 1}
        raw["network_policy"] = {"allowed_redirect_hosts": ["Allowed.Example."], "same_host_redirects_only": False, "require_https": False}
        raw["retry_policy"] = {"enabled": False, "max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 1, "jitter_seconds": 0, "max_elapsed_seconds": 60}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(path)
        self.assertEqual(config.google_queries, ())
        self.assertEqual(len(config.gdelt_queries), 1)
        self.assertEqual(config.network_policy.allowed_redirect_hosts, frozenset({"allowed.example"}))
        self.assertFalse(config.retry_policy.enabled)
        parsed_limits = config_module._parse_limits(
            {
                "response_bytes": 1024,
                "max_redirects": 0,
                "max_sources": 1,
                "max_query_chars": 32,
                "source_deadline_seconds": 1,
                "cycle_deadline_seconds": 1,
                "ipc_frame_bytes": 65536,
                "socket_timeout_seconds": 1,
                "entries_per_source": 1,
                "xml_depth": 4,
                "xml_nodes": 100,
                "title_chars": 32,
                "summary_chars": 64,
                "source_chars": 16,
                "url_chars": 128,
                "fuzzy_candidates": 1,
                "fuzzy_comparisons": 1,
            }
        )
        self.assertEqual(parsed_limits.response_bytes, 1024)
        self.assertEqual(parsed_limits.max_redirects, 0)
        self.assertEqual(parsed_limits.max_sources, 1)

    def test_preflight_wal_failure_is_not_ready(self) -> None:
        config = load_config(ROOT / "config" / "watchlist.json")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "synthetic-usable", "TELEGRAM_CHAT_ID": "1"},
            clear=False,
        ), patch.object(
            preflight_module.inspection,
            "probe_wal_capability",
            return_value=WalProbeResult(False, "delete", "WAL journal mode is unavailable"),
        ):
            code, report = preflight_module.run_preflight(config, state_path=Path(directory) / "state.db")
        self.assertEqual(code, preflight_module.PREFLIGHT_STATE)
        self.assertFalse(report["ready"])
        self.assertFalse(report["checks"]["state_filesystem"]["wal"]["ok"])


class URLPolicyBoundaryTests(unittest.TestCase):
    def test_hostname_port_and_url_shape_matrix(self) -> None:
        for raw, reason in (("", "missing_host"), ("\x00host", "url_control_character"), ("\ud800", "invalid_hostname")):
            with self.subTest(raw=repr(raw)), self.assertRaises(URLPolicyError) as caught:
                urls_module._ascii_hostname(raw)
            self.assertEqual(caught.exception.reason_code, reason)
        self.assertEqual(urls_module._ascii_hostname("BÜCHER.Example."), "xn--bcher-kva.example")
        self.assertEqual(urls_module._safe_scalar(None), "")
        self.assertEqual(urls_module._safe_scalar("a\ud800b"), "a�b")
        https = urls_module.urlsplit("https://example.com")
        http = urls_module.urlsplit("http://example.com:80")
        self.assertEqual(urls_module._validate_port(https), (443, False))
        self.assertEqual(urls_module._validate_port(http), (80, True))
        with self.assertRaises(URLPolicyError):
            urls_module._validate_port(urls_module.urlsplit("https://example.com:99999"))
        cases = [
            (None, "url_not_string"),
            ("x" * 11, "url_too_long"),
            ("https://example.com/\ufffd", "url_unicode_scalar"),
            ("https://example.com/\x01", "url_control_character"),
            (" https://example.com", "url_whitespace"),
            ("mailto:user@example.com", "scheme_disallowed"),
            ("https://u:p@example.com", "userinfo_disallowed"),
            ("https:///path", "missing_host"),
        ]
        for value, reason in cases:
            with self.subTest(value=repr(value)), self.assertRaises(URLPolicyError) as caught:
                validate_url(value, max_length=10 if isinstance(value, str) and value.startswith("x") else 2048)
            self.assertEqual(caught.exception.reason_code, reason)
        self.assertEqual(validate_url("https://[2001:db8::1]:8443/a/", allow_private=True).hostname, "2001:db8::1")
        self.assertEqual(validate_url("http://example.com", allow_http=True).port, 80)
        with self.assertRaises(URLPolicyError) as caught:
            validate_url("http://example.com")
        self.assertEqual(caught.exception.reason_code, "scheme_disallowed")
        with self.assertRaises(URLPolicyError):
            validate_url("https://127.0.0.1")
        self.assertEqual(validate_url("https://127.0.0.1", allow_private=True).hostname, "127.0.0.1")

    def test_canonical_resolution_redirect_and_log_helpers(self) -> None:
        self.assertEqual(canonical_url("https://example.com:bad/path"), "https://example.com/path")
        self.assertEqual(canonical_url("https://[bad"), "https://[bad")
        self.assertEqual(sanitize_url_for_log("https://Example.com/a?secret=1"), "https://example.com:443")
        self.assertEqual(sanitize_url_for_log("not a url"), "<invalid-url>")
        with patch("meco_news.urls.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443)), (0, 0, 0, "", ("10.0.0.1", 443))]), self.assertRaises(URLPolicyError):
            validate_resolved_addresses("example.com", 443)
        with patch("meco_news.urls.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("10.0.0.1", 443))]):
            self.assertEqual(validate_resolved_addresses("example.com", 443, allow_private=True), ["10.0.0.1"])
        current = validate_url("https://example.com")
        other = validate_url("https://other.example")
        self.assertFalse(urls_module.same_or_allowed_redirect(current, urls_module.ValidatedURL(current.normalized_url, "http", current.hostname, current.display_hostname, 80, False)))
        self.assertFalse(urls_module.same_or_allowed_redirect(current, other))
        self.assertTrue(urls_module.same_or_allowed_redirect(current, other, allowed_hosts={"OTHER.EXAMPLE."}))


class NetworkBoundaryTests(unittest.TestCase):
    class Response:
        def __init__(self, status: int = 200, body: bytes = b"ok", headers: dict[str, str] | None = None) -> None:
            self.status = status
            self._body = body
            self._headers = headers or {}
            self.closed = False

        @property
        def headers(self):
            return self._headers

        def getheader(self, key: str, default: str = "") -> str:
            return self._headers.get(key, default)

        def read(self, _size: int = -1) -> bytes:
            value, self._body = self._body, b""
            return value

        def close(self) -> None:
            self.closed = True

    def _client(self, **kwargs: object) -> BoundedHTTPClient:
        limits = CollectionLimits(response_bytes=4, source_deadline_seconds=5, socket_timeout_seconds=2, max_redirects=1)
        return BoundedHTTPClient(limits, NetworkPolicy(**kwargs))

    def test_fetch_status_redirect_and_exception_classification(self) -> None:
        client = self._client()
        responses = iter([self.Response(301, headers={"Location": "/next"}), self.Response(200, b"body", {"Content-Type": "text/plain"})])
        with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", side_effect=lambda *_args: next(responses)):
            result = client.fetch("https://example.com/start")
        self.assertEqual(result.payload, b"body")
        for status, reason in ((429, "http_429"), (503, "http_5xx"), (404, "http_error")):
            with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", return_value=self.Response(status)):
                with self.subTest(status=status), self.assertRaises(NetworkError) as caught:
                    client.fetch("https://example.com/start")
                self.assertEqual(caught.exception.reason_code, reason)
                if status == 404:
                    self.assertFalse(caught.exception.retryable)
        with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", return_value=self.Response(301, headers={"Location": "/again"})):
            with self.assertRaises(NetworkError) as caught:
                client.fetch("https://example.com/start")
            self.assertEqual(caught.exception.reason_code, "redirect_limit")
        with patch.object(client, "_validate_origin", side_effect=urls_module.URLPolicyError("ssrf_address_class")):
            with self.assertRaises(NetworkError) as caught:
                client.fetch("https://example.com/start")
            self.assertFalse(caught.exception.retryable)

    def test_fetch_http_errors_transport_and_body_limits(self) -> None:
        client = self._client()
        def http_error(code: int) -> HTTPError:
            return HTTPError("https://example.com", code, "error", {}, __import__("io").BytesIO(b"{}"))

        for error, reason in ((http_error(429), "http_429"), (http_error(503), "http_5xx"), (http_error(400), "http_error"), (TimeoutError("slow"), "network_error"), (URLError("down"), "network_error"), (OSError("down"), "network_error")):
            with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", side_effect=error):
                with self.subTest(reason=reason), self.assertRaises(NetworkError) as caught:
                    client.fetch("https://example.com")
                self.assertEqual(caught.exception.reason_code, reason)
        closed_error = http_error(503)
        with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(
            client, "_open_pinned", side_effect=closed_error
        ), self.assertRaises(NetworkError):
            client.fetch("https://example.com")
        self.assertTrue(closed_error.fp is None or closed_error.fp.closed)
        with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", side_effect=URLError(TimeoutError("slow"))):
            with self.assertRaises(NetworkError) as caught:
                client.fetch("https://example.com")
            self.assertEqual(caught.exception.reason_code, "network_error")
        response = self.Response(headers={"Content-Length": "bad"})
        with self.assertRaises(NetworkError):
            client._read_bounded(response, __import__("time").monotonic() + 1)
        with self.assertRaises(ResponseTooLarge):
            client._read_bounded(self.Response(headers={"Content-Length": "5"}), __import__("time").monotonic() + 1)
        with self.assertRaises(NetworkError) as caught:
            client._read_bounded(self.Response(body=b"12345"), __import__("time").monotonic() + 1)
        self.assertEqual(caught.exception.reason_code, "response_too_large")
        incomplete = self.Response(body=b"ignored")
        incomplete.read = MagicMock(side_effect=IncompleteRead(b"partial"))
        with self.assertRaises(NetworkError) as caught:
            client._read_bounded(incomplete, __import__("time").monotonic() + 1)
        self.assertEqual(caught.exception.reason_code, "incomplete_read")
        with self.assertRaises(NetworkError) as caught:
            client._read_bounded(self.Response(body=b"ok"), __import__("time").monotonic() - 1)
        self.assertEqual(caught.exception.reason_code, "source_deadline_exceeded")

    def test_redirect_open_pinned_and_socket_pin_paths(self) -> None:
        client = self._client(same_host_redirects_only=False, allowed_redirect_hosts=frozenset({"other.example"}))
        current = validate_url("https://example.com/start")
        self.assertEqual(client._redirect_target(current, "https://other.example/end").hostname, "other.example")
        for location in ("", "x" * 3000, "https://evil.example"):
            with self.subTest(location=repr(location)), self.assertRaises(NetworkError):
                client._redirect_target(current, location)
        connection = MagicMock()
        response = object()
        connection.getresponse.return_value = response
        with patch("meco_news.network._PinnedHTTPSConnection", return_value=connection):
            self.assertIs(client._open_pinned(validate_url("https://example.com:8443/path?q=1"), "93.184.216.34", 2), response)
            connection.request.assert_called_once()
        connection.request.side_effect = OSError("request")
        with patch("meco_news.network._PinnedHTTPConnection", return_value=connection), self.assertRaises(OSError):
            client._open_pinned(validate_url("http://example.com/path", allow_http=True), "93.184.216.34", 2)
        with patch.object(network_module.socket, "socket") as socket_factory:
            sock = socket_factory.return_value
            network_module._PinnedHTTPConnection("example.com", 80, "93.184.216.34", 1).connect()
            sock.connect.assert_called_once_with(("93.184.216.34", 80))
        self.assertIsNone(network_module._NoRedirectHandler().redirect_request(None, None, 301, "", {}, ""))
        with patch.object(network_module.socket, "socket") as socket_factory:
            sock = socket_factory.return_value
            network_module._PinnedHTTPConnection("example.com", 80, "93.184.216.34", 1).connect()
            sock.connect.side_effect = OSError("connect")
            with self.assertRaises(OSError):
                network_module._PinnedHTTPConnection("example.com", 80, "93.184.216.34", 1).connect()


class TelegramBoundaryTests(unittest.TestCase):
    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size: int = -1) -> bytes:
            return self.body

    def _client(self, chat_id: str = "1") -> TelegramClient:
        return TelegramClient("synthetic-token", chat_id, timeout=1)

    def test_html_parser_and_message_validation_matrix(self) -> None:
        for markup in ("<u>x</u>", "<b id='x'>x</b>", "<a>x</a>", "<a href='javascript:x'>x</a>", "<a href='https://x' bad='1'>x</a>", "<br/>", "<b>x</i>", "<!-- x -->", "<!DOCTYPE html>", "<?xml x?>", "<![x]>"):
            parser = _TelegramHTMLParser()
            try:
                parser.feed(markup)
                parser.close()
            except AssertionError:
                # Some stdlib versions reject marked sections by raising
                # instead of reaching unknown_decl; a raise is a rejection.
                continue
            self.assertTrue(parser.error, markup)
        parser = _TelegramHTMLParser()
        parser.feed("<b>ok</b><i>yes</i><a href='https://example.com'>link</a>")
        self.assertFalse(parser.error)
        for value in ("", "   ", "<b>x", "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                validate_message(value)
        with self.assertRaises(ValueError):
            validate_message("x", max_units=0)
        with self.assertRaises(ValueError):
            validate_message("x" * 100, max_bytes=2)

    def test_telegram_call_classifies_envelopes_and_transport(self) -> None:
        client = self._client()
        def call_with(value: object) -> object:
            client._opener.open = MagicMock(return_value=self.Response(json.dumps(value).encode()))
            return client._call("getMe", {})

        self.assertEqual(call_with({"ok": True, "result": {"id": 1}})["ok"], True)
        for body in (b"not-json", b"[]", b'{"ok":"yes"}'):
            client._opener.open = MagicMock(return_value=self.Response(body))
            with self.subTest(body=body), self.assertRaises(TelegramSendError) as caught:
                client._call("getMe")
            self.assertIn(caught.exception.reason_code, {"telegram_malformed_response", "telegram_ambiguous"})
        envelopes = [
            ({"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}, "telegram_rate_limited"),
            ({"ok": False, "error_code": 500}, "telegram_ambiguous"),
            ({"ok": False, "error_code": 400}, "telegram_terminal"),
            ({"ok": False, "error_code": True}, "telegram_malformed_response"),
            ({"ok": False}, "telegram_malformed_response"),
        ]
        for envelope, reason in envelopes:
            with self.subTest(envelope=envelope), self.assertRaises(TelegramSendError) as caught:
                call_with(envelope)
            self.assertEqual(caught.exception.reason_code, reason)
        for error in (TimeoutError("slow"), ConnectionResetError("reset"), URLError(TimeoutError("slow")), URLError("down"), OSError("down")):
            client._opener.open = MagicMock(side_effect=error)
            with self.subTest(error=type(error).__name__), self.assertRaises(TelegramSendError) as caught:
                client._call("getMe")
            self.assertEqual(caught.exception.reason_code, "telegram_ambiguous")
        for code, reason in ((429, "telegram_rate_limited"), (503, "telegram_ambiguous"), (400, "telegram_terminal")):
            error = HTTPError("https://telegram.example", code, "error", {}, __import__("io").BytesIO(b"{}"))
            client._opener.open = MagicMock(side_effect=error)
            with self.subTest(code=code), self.assertRaises(TelegramSendError) as caught:
                client._call("getMe")
            self.assertEqual(caught.exception.reason_code, reason)
            self.assertTrue(error.fp is None or error.fp.closed)

    def test_telegram_public_methods_and_rendering_fallbacks(self) -> None:
        client = self._client()
        client._call = MagicMock(return_value={"ok": True, "result": {"id": 1}})
        self.assertEqual(client.get_me()["id"], 1)
        client._call.return_value = {"ok": True, "result": []}
        with self.assertRaises(TelegramSendError):
            client.get_me()
        client._call.return_value = {"ok": True, "result": [{"message": {"chat": {"id": 1, "type": "private"}}}, None, {"channel_post": {"chat": {"id": 1, "type": "private", "title": "T"}}}]}
        self.assertEqual(len(client.discover_chats()), 1)
        client._call.return_value = {"ok": True, "result": {"message_id": 2, "chat": {"id": 1}}}
        self.assertEqual(client.send_html("<b>ok</b>"), "2")
        for result in ({"ok": True, "result": {}}, {"ok": True, "result": {"message_id": 0, "chat": {"id": 1}}}, {"ok": True, "result": {"message_id": 1, "chat": {"id": 2}}}):
            client._call.return_value = result
            with self.subTest(result=result), self.assertRaises(TelegramSendError):
                client.send_html("ok")
        with self.assertRaises(ValueError):
            self._client("").send_html("ok")
        self.assertEqual(telegram_module.TelegramSendError("telegram_rate_limited", "x", retry_after=-2).retry_after, 0)
        self.assertEqual(telegram_module.TelegramSendError("telegram_terminal", "x").outcome, "rejected_terminal")
        item = _item(title="T" * 300, source="S" * 200, score=1)
        self.assertTrue(build_digest([], "Company", "UTC", delivery_date="not-a-date").messages)
        with self.assertRaises(ValueError):
            build_digest([item], "C" * 1000, "UTC", max_length=10, max_bytes=10, minimum_count=1)
        rendered = build_digest([_item()], "Company", "UTC", issues=["source"], minimum_count=5, delivery_id=4, delivery_date="2026-09-06")
        self.assertTrue(rendered.messages)


class RankingBoundaryTests(unittest.TestCase):
    def test_ranking_topic_and_selection_branches(self) -> None:
        config = load_config(ROOT / "config" / "watchlist.json")
        self.assertTrue(_contains("alpha beta", "alpha"))
        self.assertFalse(_contains("alpha", ""))
        self.assertTrue(_contains("lpg-terminal", "lpg-terminal"))
        self.assertEqual(_topic_value({"id": "x"}, "id"), "x")
        self.assertEqual(_topic_value(object(), "missing", "fallback"), "fallback")
        now = datetime.now(UTC)
        ranked = rank_item(_item(title="PT Meco Inoxprima lpg terminal project Indonesia"), config, now)
        self.assertGreater(ranked.score, 0)
        no_topic = rank_item(NewsItem("ordinary unrelated headline", "https://example.com/none", "Example"), config, now)
        self.assertLess(no_topic.score, 0)
        aggregator = _item(source="News", url="https://news.google.com/story")
        aggregator.collector = "google_news"
        publisher = _item(source="Publisher", url="https://example.com/story2", score=1)
        merged = _merge_group([aggregator, publisher])
        self.assertIsNot(merged, aggregator)
        self.assertEqual(merged.source, "Publisher")
        selected = select_digest(
            [
                ranked,
                _item(title="second story", url="https://example.com/2", score=10),
                _item(title="low score", url="https://example.com/3", score=0),
                _item(title="quarantined", url="https://example.com/4", score=20),
            ],
            config,
            sent_fingerprints={_item(title="already", url="https://example.com/5").fingerprint},
            sent_url_keys=set(),
            sent_title_keys=set(),
            now=now,
        )
        self.assertTrue(all(not item.is_quarantined for item in selected))

    def test_dedup_budget_and_empty_material_are_explicit(self) -> None:
        config = {"limits": {"fuzzy_candidates": 2, "fuzzy_comparisons": 0}}
        first = _item("same headline", "https://example.com/1")
        second = _item("same headline with more words", "https://example.com/2")
        empty, empty_stats = deduplicate_with_stats([NewsItem("x", "https://example.com/x", "x", quarantine_reason="bad")], config)
        self.assertEqual(empty, [])
        self.assertFalse(empty_stats.budget_exhausted)
        result, stats = deduplicate_with_stats([first, second], config)
        self.assertEqual(len(result), 2)
        self.assertTrue(stats.budget_exhausted)
        self.assertEqual(stats.unprocessed_candidates, 1)


class MaintenanceBoundaryTests(unittest.TestCase):
    def test_exclusive_guard_lifecycle_fence_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            held, info = maintenance_module.is_maintenance_held(path)
            self.assertFalse(held)
            self.assertEqual(info, {"held": False})
            with MaintenanceContext.acquire(path, owner="operator", scope="maintenance", ttl_seconds=60) as context:
                self.assertTrue(context.live)
                self.assertEqual(context.db_path, path.resolve())
                self.assertEqual(context.owner, "operator")
                self.assertEqual(context.scope, "maintenance")
                self.assertEqual(maintenance_module.maintenance_fence(context), context.token)
                self.assertEqual(maintenance_module.assert_maintenance_fence(context, db_path=path), context.token)
                held, info = maintenance_module.is_maintenance_held(path)
                self.assertTrue(held)
                self.assertEqual(info["owner"], "operator")
                with self.assertRaises(MaintenanceBusy):
                    MaintenanceContext.acquire(path, owner="second")
                with self.assertRaises(MaintenanceError):
                    maintenance_module.assert_maintenance_fence(context, db_path=path, scope="other")
                with self.assertRaises(MaintenanceError):
                    maintenance_module.assert_maintenance_fence(context, db_path=Path(directory) / "other.db")
                with patch("meco_news.maintenance.inspect_state") as inspect, patch("meco_news.maintenance.probe_wal_capability") as wal:
                    inspect.return_value.classification = "compatible"
                    inspect.return_value.schema_version = 5
                    inspect.return_value.integrity = "ok"
                    inspect.return_value.detail = "ok"
                    wal.return_value.ok = True
                    wal.return_value.journal_mode = "wal"
                    wal.return_value.reason = "ok"
                    report = _maintenance_verify(path, context)
                self.assertTrue(report["verified_for_maintenance"])
                self.assertEqual(report["wal"]["journal_mode"], "wal")
                self.assertTrue(context.release())
                self.assertFalse(context.live)
            self.assertFalse(maintenance_module.is_maintenance_held(path)[0])
            self.assertFalse(context.release())

    def test_database_fence_is_transaction_visible_and_stale_context_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            from meco_news.storage import StateStore

            with StateStore(path) as seed:
                seed.acquire_lease("delivery", "seed", 180)
                seed.create_delivery("2026-09-06", owner_id="seed")
                seed.release_lease("delivery", "seed")
            with MaintenanceContext.acquire(path, owner="operator") as context, StateStore(path, maintenance_context=context) as store:
                self.assertIsNone(context.database_epoch)
                store.connection.execute("BEGIN IMMEDIATE")
                self.assertIsNotNone(maintenance_module.ensure_database_fence(store.connection, context))
                store.connection.rollback()
                # The first protected mutation commits the fence row together
                # with the operator action and then remains valid for the hold.
                self.assertIsNotNone(store._maintenance_context)
                store.connection.execute("BEGIN IMMEDIATE")
                self.assertIsNotNone(maintenance_module.ensure_database_fence(store.connection, context))
                store.connection.commit()
                self.assertIsNotNone(context.database_epoch)
            with self.assertRaises(MaintenanceError):
                maintenance_module._assert_database_fence(context)

    def test_runtime_holds_refresh_and_stale_marker_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            first = RuntimeContext.acquire(path, owner="one")
            second = RuntimeContext.acquire(path, owner="two")
            self.assertTrue(first.live)
            self.assertTrue(second.live)
            self.assertEqual(first.db_path, path.resolve())
            self.assertEqual(first.owner, "one")
            self.assertNotEqual(first.token, second.token)
            first.refresh()
            with self.assertRaises(MaintenanceBusy):
                MaintenanceContext.acquire(path, owner="operator")
            runtime_files = list(maintenance_module.runtime_dir(path).glob("*.json"))
            self.assertEqual(len(runtime_files), 2)
            first_path = maintenance_module.runtime_dir(path) / f"{first.token}.json"
            first_path.unlink()
            self.assertFalse(first.live)
            with self.assertRaises(MaintenanceError):
                first.refresh()
            self.assertTrue(second.release())
            self.assertFalse(second.release())
            maintenance_module.runtime_dir(path).mkdir(parents=True, exist_ok=True)
            stale = maintenance_module.runtime_dir(path) / "stale.json"
            stale.write_text(
                json.dumps({"owner": "dead", "pid": 99999999, "process_identity": "", "token": "stale", "acquired_at": "2000-01-01T00:00:00+00:00", "ttl_seconds": 0}),
                encoding="utf-8",
            )
            (maintenance_module.runtime_dir(path) / "ignored.txt").write_text("x", encoding="utf-8")
            self.assertEqual(maintenance_module._live_runtime_holders(path), [])
            self.assertFalse(stale.exists())
            self.assertFalse(maintenance_module._read_runtime_holder(maintenance_module.runtime_dir(path) / "bad.json"))

    def test_process_and_marker_helpers_fail_closed(self) -> None:
        self.assertFalse(_pid_alive(True))
        self.assertFalse(_pid_alive("not-a-pid"))
        self.assertFalse(_pid_alive(0))
        self.assertTrue(_pid_alive(os.getpid()))
        self.assertEqual(maintenance_module._process_identity(0), "")
        marker = {"acquired_at": datetime.now(UTC) - timedelta(seconds=5), "ttl_seconds": 30, "pid": os.getpid(), "process_identity": ""}
        self.assertFalse(maintenance_module._is_stale(marker, datetime.now(UTC)))
        marker["ttl_seconds"] = 0
        self.assertTrue(maintenance_module._is_stale(marker, datetime.now(UTC)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "marker.json"
            path.write_text("not-json", encoding="utf-8")
            self.assertIsNone(maintenance_module._read_marker(path))
            self.assertEqual(maintenance_module.runtime_dir(path), Path(str(path.resolve()) + ".runtime.d"))


if __name__ == "__main__":
    unittest.main()
