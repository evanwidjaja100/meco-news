"""Behavioral corpus for release paths that are not exercised by the happy path.

These tests intentionally use disposable state and in-process transport doubles.  They
exercise the same public boundaries used by the CLI while keeping credentials and
network access out of the test process.
"""

from __future__ import annotations

from datetime import datetime, UTC
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from meco_news.app import (
    RunOutcome,
    _FrozenHistory,
    _collect_rank_select,
    _delivery_date,
    _frozen_integer,
    _frozen_item,
    _frozen_text,
    _has_recovery_work,
    _history_reader,
    _is_due,
    _load_frozen_input,
    _next_delivery,
    _next_durable_retry,
    _parse_iso,
    _reject_duplicate_json_keys,
    _retry_delay,
    _retry_policy_snapshot,
    _state_status,
    _target_snapshot,
    build_parser,
    main,
    run_once,
)
from meco_news.collectors import CollectionResult, SourceResult
from meco_news.config import ConfigurationError, RetryPolicy, load_config
from meco_news.models import NewsItem
from meco_news.network import (
    BoundedHTTPClient,
    FetchResponse,
    NetworkError,
    ResponseTooLarge,
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
    fetch_bytes,
)
from meco_news.telegram import (
    TelegramClient,
    TelegramSendError,
    _TelegramHTMLParser,
    _article_block,
    _build_header,
    _clean_display,
    _continuation_header,
    _fit_block,
    _looks_placeholder,
    _timezone_label,
    _truncate,
    build_digest,
    utf16_units,
    validate_message,
)
from meco_news.timezones import get_timezone
from meco_news.urls import URLPolicyError, canonical_url, same_or_allowed_redirect, validate_resolved_addresses, validate_url
from meco_news.storage import StateStore


def _config():
    return load_config("config/watchlist.json")


def _item(title: str = "LPG terminal project", *, url: str = "https://example.com/story", published: datetime | None = None) -> NewsItem:
    return NewsItem(
        title=title,
        url=url,
        source="Example Publisher",
        published_at=published or datetime.now(UTC),
        summary="A new industrial storage project is under construction.",
        topic="lpg_energy",
        topic_label="LPG storage",
        relevance_reason="Potential tank demand.",
        score=15,
        source_host="example.com",
    )


class AppHelperCorpusTests(unittest.TestCase):
    def test_app_helper_boundaries_and_frozen_history(self) -> None:
        config = _config()
        with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "123", "TELEGRAM_API_BASE_URL": "https://telegram.example"}, clear=False):
            first = _target_snapshot(config)
            self.assertEqual(len(first), 64)
        self.assertEqual(_retry_policy_snapshot(config)["max_attempts"], config.retry_policy.max_attempts)
        self.assertIsNotNone(_parse_iso("2026-09-06T01:02:03+07:00"))
        self.assertIsNone(_parse_iso("invalid"))
        self.assertIsNone(_parse_iso(None))
        self.assertEqual(_retry_delay({}, 1).seconds, 67)
        self.assertEqual(_retry_delay({"retry_policy": {"jitter_seconds": 0, "base_delay_seconds": 2, "max_delay_seconds": 3}}, 3).seconds, 3)
        self.assertEqual(_retry_delay({"retry_policy": {"jitter_seconds": 1, "base_delay_seconds": 1, "max_delay_seconds": 3600}}, 1, retry_after=99999).seconds, 3600)
        self.assertEqual(_delivery_date(config), datetime.now(get_timezone(config.timezone)).date().isoformat())
        self.assertEqual(_frozen_text("x", "field", 1), "x")
        self.assertEqual(_frozen_integer(3, "number", maximum=3), 3)
        with self.assertRaises(ConfigurationError):
            _frozen_text(1, "field", 3)
        with self.assertRaises(ConfigurationError):
            _frozen_text("", "field", 3, required=True)
        with self.assertRaises(ConfigurationError):
            _frozen_text("x\x00", "field", 3)
        with self.assertRaises(ConfigurationError):
            _frozen_text("\ud800", "field", 3)
        with self.assertRaises(ConfigurationError):
            _frozen_integer(True, "number")
        with self.assertRaises(ValueError):
            _reject_duplicate_json_keys([("a", 1), ("a", 2)])
        item = _item()
        history = _FrozenHistory([{"url_key": item.url_key, "title_key": item.title_key}])
        self.assertEqual(history.identity_keys([item])[0], {item.url_key})
        self.assertEqual(history.identity_keys([])[1], {item.title_key})
        self.assertEqual(RunOutcome(0, "ok"), 0)
        self.assertEqual(int(RunOutcome(1, "bad")), 1)

    def test_frozen_input_accepts_valid_and_rejects_shape_variants(self) -> None:
        item = _item()
        raw = {
            "schema_version": 1,
            "collected_at": "2026-09-06T00:00:00+00:00",
            "items": [{"title": item.title, "url": item.url, "source": item.source, "published_at": item.published_at.isoformat(), "score": 12, "matches": ["lpg"]}],
            "source_results": [{"source_id": "source", "source_name": "Source", "outcome": "succeeded", "duration_ms": 2, "bytes_read": 3, "accepted_count": 1, "quarantined_count": 0}],
            "issues": [],
            "duration_ms": 2,
            "history": [{"url_key": item.url_key, "title_key": item.title_key}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            collection, history = _load_frozen_input(path)
            self.assertEqual(len(collection.items), 1)
            self.assertEqual(collection.successful_sources, 1)
            self.assertEqual(history.identity_keys([])[0], {item.url_key})

            cases = [
                {**raw, "unexpected": 1},
                {key: value for key, value in raw.items() if key != "items"},
                {**raw, "schema_version": True},
                {**raw, "collected_at": "2026-09-06T00:00:00"},
                {**raw, "items": [1]},
                {**raw, "source_results": [{"source_id": "s", "source_name": "S", "outcome": "bad"}]},
                {**raw, "issues": [1]},
                {**raw, "duration_ms": -1},
                {**raw, "history": [{"url_key": "only"}]},
            ]
            for index, invalid in enumerate(cases):
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.subTest(index=index), self.assertRaises(ConfigurationError):
                    _load_frozen_input(path)
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                _load_frozen_input(path)
            path.write_text('{"schema_version":1,"collected_at":"2026-09-06T00:00:00+00:00","items":[],"source_results":[],"issues":["down"] ,"duration_ms":0,"history":[]}', encoding="utf-8")
            collection, _ = _load_frozen_input(path)
            self.assertEqual(collection.failed_sources, 1)

    def test_frozen_item_and_state_readers_cover_invalid_fields(self) -> None:
        item = {"title": "title", "url": "https://example.com", "source": "source"}
        with self.assertRaises(ConfigurationError):
            _frozen_item([], 0)
        with self.assertRaises(ConfigurationError):
            _frozen_item({**item, "unexpected": 1}, 0)
        with self.assertRaises(ConfigurationError):
            _frozen_item({**item, "published_at": "2026-09-06T00:00:00"}, 0)
        with self.assertRaises(ConfigurationError):
            _frozen_item({**item, "score": True}, 0)
        with self.assertRaises(ConfigurationError):
            _frozen_item({**item, "matches": [1]}, 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            self.assertIsNone(_history_reader(path))
            self.assertEqual(_state_status(path)["state"], "missing")
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="owner")
                store.release_lease("delivery", "owner")
            reader = _history_reader(path)
            self.assertIsNotNone(reader)
            assert reader is not None
            reader.close()
            self.assertEqual(_state_status(path, _config())["schema_version"], 5)
            self.assertEqual(delivery.generation, 0)

    def test_rank_select_and_scheduler_helpers(self) -> None:
        config = _config()
        item = _item(published=datetime.now(UTC))
        collection = CollectionResult([item], [SourceResult("s", "S", "succeeded", items=[item])], datetime.now(UTC), 1)
        selected, exclusions, _, _ = _collect_rank_select(config, collection, None, top_candidates=1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(exclusions, {})
        with patch.dict("os.environ", {"STATE_DB": "C:\\does-not-exist\\state.db"}, clear=False):
            self.assertFalse(_has_recovery_work(config))
            self.assertIsNone(_next_durable_retry())
        self.assertIsInstance(_next_delivery(config), datetime)
        self.assertIsInstance(_is_due(config), bool)
        parser = build_parser()
        self.assertTrue(parser.parse_args(["--metrics"]).metrics)

    def test_main_report_modes_are_machine_parseable_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            alert_file = Path(directory) / "alerts.jsonl"
            with patch.dict("os.environ", {"STATE_DB": str(state), "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
                self.assertEqual(main(["--metrics", "--json"]), 0)
                self.assertEqual(main(["--healthcheck", "--alert-file", str(alert_file), "--json"]), 1)
                self.assertTrue(alert_file.exists())
                self.assertEqual(main(["--backup", str(Path(directory) / "backups"), "--json", "--config", "config/watchlist.json"]), 1)
                self.assertEqual(main(["--restore", str(Path(directory) / "missing.db"), "--json", "--config", "config/watchlist.json"]), 1)
                self.assertEqual(main(["--migrate", "--to-version", "4", "--json", "--config", "config/watchlist.json"]), 2)
                self.assertEqual(main(["--resolve-chunk", "1", "--resolution", "retry", "--reason", "r", "--operator", "o", "--json", "--config", "config/watchlist.json"]), 1)

    def test_main_telegram_modes_and_due_guard(self) -> None:
        class FakeTelegram:
            def __init__(self, *_args, **_kwargs):
                pass

            def get_me(self):
                return {"username": "meco_bot"}

            def send_html(self, _text):
                return "7"

            def discover_chats(self):
                return [{"id": 1, "type": "private"}]

        env = {"TELEGRAM_BOT_TOKEN": "123456:usable", "TELEGRAM_CHAT_ID": "1"}
        with patch.dict("os.environ", env, clear=False), patch("meco_news.app.TelegramClient", FakeTelegram):
            self.assertEqual(main(["--test-telegram", "--json"]), 0)
            self.assertEqual(main(["--discover-chat", "--json"]), 0)
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "replace_with_token", "TELEGRAM_CHAT_ID": "replace_with_chat"}, clear=False):
            self.assertEqual(main(["--test-telegram", "--json"]), 3)
        with patch("meco_news.app._is_due", return_value=False), patch("meco_news.app._has_recovery_work", return_value=False):
            self.assertEqual(main(["--run-if-due", "--config", "config/watchlist.json"]), 0)


class AppRunOnceCorpusTests(unittest.TestCase):
    def _run(self, collection: CollectionResult, telegram, *, extra_env: dict[str, str] | None = None, config=None, stop_event=None, force=False, operator="", reason=""):
        config = config or _config()
        with tempfile.TemporaryDirectory() as directory:
            values = {"STATE_DB": str(Path(directory) / "state.db"), "TELEGRAM_BOT_TOKEN": "123456:usable", "TELEGRAM_CHAT_ID": "1"}
            values.update(extra_env or {})
            with patch.dict("os.environ", values, clear=False), patch("meco_news.app.collect_all", return_value=collection), patch("meco_news.app.TelegramClient", telegram):
                result = run_once(config, force=force, stop_event=stop_event, force_operator=operator, force_reason=reason)
            return result, Path(directory) / "state.db"

    def test_live_send_outcomes_cover_retry_terminal_ambiguous_and_exception(self) -> None:
        item = _item()
        collection = CollectionResult([item], [SourceResult("s", "S", "succeeded", items=[item])], datetime.now(UTC), 1)

        class Client:
            error = None

            def __init__(self, *_args, **_kwargs):
                pass

            def send_html(self, _payload):
                if Client.error:
                    raise Client.error
                return "9"

        for error, expected in (
            (TelegramSendError("telegram_ambiguous", "unknown"), "needs_attention"),
            (TelegramSendError("telegram_rate_limited", "slow", retry_after=1), "retry_wait"),
            (TelegramSendError("telegram_terminal", "bad"), "failed_terminal"),
            (RuntimeError("client failed"), "needs_attention"),
        ):
            Client.error = error
            result, _ = self._run(collection, Client)
            self.assertEqual(result.outcome, expected)

        Client.error = TelegramSendError("telegram_rate_limited", "slow")
        result, _ = self._run(collection, Client, extra_env={"MECO_DISABLE_RETRIES": "1"})
        self.assertEqual(result.outcome, "failed_terminal")

    def test_live_send_stop_after_ack_and_collection_failures(self) -> None:
        item = _item()
        collection = CollectionResult([item], [SourceResult("s", "S", "succeeded", items=[item])], datetime.now(UTC), 1)
        stop = threading.Event()

        class StoppingClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def send_html(self, _payload):
                stop.set()
                return "2"

        result, _ = self._run(collection, StoppingClient, stop_event=stop)
        self.assertEqual(result.outcome, "stopped")
        failed = CollectionResult([], [SourceResult("s", "S", "failed", reason_code="network_error")], datetime.now(UTC), 1)
        retry_config = _config()
        result, _ = self._run(failed, StoppingClient, config=retry_config)
        self.assertEqual(result.outcome, "retry_wait")
        exhausted_config = __import__("dataclasses").replace(retry_config, retry_policy=RetryPolicy(max_attempts=1))
        result, _ = self._run(failed, StoppingClient, config=exhausted_config)
        self.assertEqual(result.outcome, "failed_terminal")

    def test_active_delivery_recovery_and_force_guards(self) -> None:
        config = _config()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "fixture", 180)
                attention = store.create_delivery("2026-09-06", state="needs_attention", owner_id="fixture")
                store.release_lease("delivery", "fixture")
            with patch.dict("os.environ", {"STATE_DB": str(path), "TELEGRAM_BOT_TOKEN": "123456:usable", "TELEGRAM_CHAT_ID": "1"}, clear=False):
                self.assertEqual(run_once(config).outcome, "needs_attention")
            self.assertEqual(attention.generation, 0)
        empty = CollectionResult([], [SourceResult("s", "S", "succeeded")], datetime.now(UTC), 1)
        result, _ = self._run(empty, lambda *_args, **_kwargs: None, force=True)
        self.assertEqual(result.outcome, "invalid_options")


class URLAndNetworkCorpusTests(unittest.TestCase):
    def test_url_policy_and_redirect_matrix(self) -> None:
        self.assertEqual(canonical_url("HTTPS://Example.com/a/?b=2&a=1&utm_source=x#x"), "https://example.com/a?a=1&b=2")
        self.assertEqual(canonical_url("https://bad host/\ud800"), "https://bad host/�")
        self.assertEqual(canonical_url(None), "")
        for value, reason in (
            (None, "url_not_string"),
            ("", "empty_url"),
            (" https://example.com", "url_whitespace"),
            ("ftp://example.com", "scheme_disallowed"),
            ("https://example.com:0", "invalid_port"),
            ("https://[bad]/", "invalid_url"),
            ("https://example.com/\ud800", "url_unicode_scalar"),
        ):
            with self.subTest(value=value), self.assertRaises(URLPolicyError) as caught:
                validate_url(value)
            self.assertEqual(caught.exception.reason_code, reason)
        current = validate_url("https://example.com/a")
        other = validate_url("https://other.example/a")
        http = validate_url("http://example.com/a", allow_http=True)
        self.assertTrue(same_or_allowed_redirect(current, current))
        self.assertTrue(same_or_allowed_redirect(current, other, allowed_hosts={"other.example"}))
        self.assertFalse(same_or_allowed_redirect(current, other))
        self.assertFalse(same_or_allowed_redirect(current, http))
        with patch("meco_news.urls.socket.getaddrinfo", side_effect=OSError("dns")), self.assertRaises(URLPolicyError):
            validate_resolved_addresses("example.com", 443)
        with patch("meco_news.urls.socket.getaddrinfo", return_value=[]), self.assertRaises(URLPolicyError):
            validate_resolved_addresses("example.com", 443)
        with patch("meco_news.urls.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 443))]), self.assertRaises(URLPolicyError):
            validate_resolved_addresses("example.com", 443)
        with patch("meco_news.urls.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]):
            self.assertEqual(validate_resolved_addresses("example.com", 443), ["93.184.216.34"])

    def test_network_fetch_statuses_redirects_and_bounded_body(self) -> None:
        class Response:
            def __init__(self, status=200, body=b"ok", headers=None, chunks=None):
                self.status = status
                self.headers = headers or {}
                self._body = list(chunks) if chunks is not None else [body]
                self.closed = False

            def getheader(self, name, default=""):
                return self.headers.get(name, default)

            def read(self, _size):
                return self._body.pop(0) if self._body else b""

            def close(self):
                self.closed = True

        client = BoundedHTTPClient(network_policy=__import__("meco_news.config", fromlist=["NetworkPolicy"]).NetworkPolicy(require_https=True))
        target = validate_url("https://example.com/story")
        with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", return_value=Response(body=b"hello", headers={"Content-Type": "text/xml"})):
            response = client.fetch(target.normalized_url)
        self.assertIsInstance(response, FetchResponse)
        self.assertEqual(response.payload, b"hello")

        responses = iter([Response(302, headers={"Location": "/next"}), Response(200, body=b"done")])
        with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", side_effect=lambda *_args: next(responses)):
            self.assertEqual(client.fetch(target.normalized_url).payload, b"done")
        for status, reason in ((429, "http_429"), (500, "http_5xx"), (404, "http_error")):
            with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", return_value=Response(status)):
                with self.assertRaises(NetworkError) as caught:
                    client.fetch(target.normalized_url)
                self.assertEqual(caught.exception.reason_code, reason)
        with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", return_value=Response(302)):
            with self.assertRaises(NetworkError) as caught:
                client.fetch(target.normalized_url)
            self.assertEqual(caught.exception.reason_code, "redirect_disallowed")
        tiny = BoundedHTTPClient(__import__("meco_news.config", fromlist=["CollectionLimits"]).CollectionLimits(response_bytes=3), network_policy=__import__("meco_news.config", fromlist=["NetworkPolicy"]).NetworkPolicy())
        response = Response(200, headers={"Content-Length": "4"})
        with self.assertRaises(ResponseTooLarge):
            tiny._read_bounded(response, float("inf"))
        response = Response(200, chunks=[b"aa", b"bb"])
        with self.assertRaises(ResponseTooLarge):
            tiny._read_bounded(response, float("inf"))
        with self.assertRaises(NetworkError):
            tiny._read_bounded(Response(200, headers={"Content-Length": "bad"}), float("inf"))
        with self.assertRaises(NetworkError):
            tiny._read_bounded(Response(200), 0)

    def test_network_error_translation_and_pinned_connection_construction(self) -> None:
        from urllib.error import HTTPError, URLError
        client = BoundedHTTPClient()
        target = validate_url("https://example.com/path?q=1")
        with patch.object(client, "_validate_origin", side_effect=URLPolicyError("ssrf_address_class")), self.assertRaises(NetworkError) as caught:
            client.fetch(target.normalized_url)
        self.assertEqual(caught.exception.reason_code, "ssrf_address_class")
        error = HTTPError(target.normalized_url, 429, "slow", {"Retry-After": "1"}, None)
        error.read = lambda _size: b'{"parameters":{"retry_after":3}}'
        with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", side_effect=error), self.assertRaises(NetworkError) as caught:
            client.fetch(target.normalized_url)
        self.assertEqual(caught.exception.reason_code, "http_429")
        for exc in (URLError("down"), TimeoutError("timeout"), OSError("os")):
            with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(client, "_open_pinned", side_effect=exc), self.assertRaises(NetworkError):
                client.fetch(target.normalized_url)
        with patch("meco_news.network.BoundedHTTPClient.fetch", return_value=FetchResponse(target, b"ok", 200, "")):
            self.assertEqual(fetch_bytes(target.normalized_url), b"ok")
        self.assertEqual(_PinnedHTTPConnection("example.com", 80, "93.184.216.34", 1)._pinned_address, "93.184.216.34")
        self.assertEqual(_PinnedHTTPSConnection("example.com", 443, "93.184.216.34", 1)._tls_server_hostname, "example.com")


class TelegramCorpusTests(unittest.TestCase):
    class Response:
        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return self.body

    def _client(self) -> TelegramClient:
        return TelegramClient("123456:usable", "1")

    def test_telegram_envelopes_and_transport_errors(self) -> None:
        from urllib.error import HTTPError, URLError

        client = self._client()
        client._opener = MagicMock()
        client._opener.open.return_value = self.Response(b'{"ok":true,"result":{"id":1}}')
        self.assertEqual(client._call("getMe")["ok"], True)
        for body in (b"not-json", b"[]", b'{"ok":"yes"}'):
            client._opener.open.return_value = self.Response(body)
            with self.assertRaises(TelegramSendError) as caught:
                client._call("getMe")
            self.assertEqual(caught.exception.outcome, "ambiguous")
        for body, outcome in ((b'{"ok":false,"error_code":429,"parameters":{"retry_after":4}}', "rejected_retryable"), (b'{"ok":false,"error_code":400}', "rejected_terminal"), (b'{"ok":false,"error_code":500}', "ambiguous"), (b'{"ok":false,"error_code":true}', "ambiguous")):
            client._opener.open.return_value = self.Response(body)
            with self.assertRaises(TelegramSendError) as caught:
                client._call("sendMessage")
            self.assertEqual(caught.exception.outcome, outcome)
        for exc in (TimeoutError("timeout"), URLError("down"), OSError("socket")):
            client._opener.open.side_effect = exc
            with self.assertRaises(TelegramSendError) as caught:
                client._call("sendMessage")
            self.assertEqual(caught.exception.outcome, "ambiguous")
        error = HTTPError("https://telegram", 500, "error", {}, None)
        error.read = lambda _size: b"{}"
        client._opener.open.side_effect = error
        with self.assertRaises(TelegramSendError) as caught:
            client._call("sendMessage")
        self.assertEqual(caught.exception.outcome, "ambiguous")
        with self.assertRaises(ValueError):
            TelegramClient("replace_with_token", "1")
        with self.assertRaises(ValueError):
            TelegramClient("123456:usable", "").send_html("ok")

    def test_telegram_result_validation_and_chat_discovery(self) -> None:
        client = self._client()
        client._call = MagicMock(return_value={"ok": True, "result": {"id": 1, "username": "bot"}})
        self.assertEqual(client.get_me()["id"], 1)
        client._call.return_value = {"ok": True, "result": [1]}
        with self.assertRaises(TelegramSendError):
            client.get_me()
        client._call.return_value = {"ok": True, "result": [{"message": {"chat": {"id": 1, "type": "private", "title": "x"}}}, {"channel_post": {"chat": {"id": 1, "type": "private"}}}, 3]}
        self.assertEqual(len(client.discover_chats()), 1)
        client._call.return_value = {"ok": True, "result": {}}
        with self.assertRaises(TelegramSendError):
            client.discover_chats()
        for result in (
            {"ok": True, "result": {}},
            {"ok": True, "result": {"message_id": 1}},
            {"ok": True, "result": {"message_id": True, "chat": {"id": 1}}},
            {"ok": True, "result": {"message_id": 1, "chat": {"id": 2}}},
        ):
            client._call.return_value = result
            with self.assertRaises(TelegramSendError):
                client.send_html("<b>hello</b>")
        client._call.return_value = {"ok": True, "result": {"message_id": 3, "chat": {"id": 1}}}
        self.assertEqual(client.send_html("<b>hello</b>"), "3")

    def test_html_parser_and_rendering_fallbacks(self) -> None:
        for text in ("<b>x</b>", "<a href=\"https://example.com\">x</a>", "plain &amp; text"):
            parser = _TelegramHTMLParser()
            parser.feed(text)
            parser.close()
            self.assertFalse(parser.error)
        for text in ("<u>x</u>", "<b id=\"x\">x</b>", "<a href=\"javascript:x\">x</a>", "<b>x", "</b>", "<!--x-->", "<!DOCTYPE html>", "<br/>"):
            parser = _TelegramHTMLParser()
            parser.feed(text)
            parser.close()
            self.assertTrue(parser.error or parser.stack)
        with self.assertRaises(ValueError):
            validate_message("<b>x")
        with self.assertRaises(ValueError):
            validate_message("<u>x</u>")
        with self.assertRaises(ValueError):
            validate_message("x", max_units=0)
        self.assertTrue(_looks_placeholder("your_token_here"))
        self.assertFalse(_looks_placeholder("usable"))
        self.assertEqual(utf16_units("😀"), 2)
        self.assertIn("...", _clean_display("x" * 20, 8))
        self.assertIn("...", _truncate("x" * 20, 8))
        tz = get_timezone("UTC")
        item = _item(title="A & B", url="https://example.com/a")
        self.assertIn("A &amp; B", _article_block(1, item, tz))
        self.assertIn("continued", _continuation_header(4, 1, 3900, 15600))
        self.assertIn("UTC", _timezone_label("UTC", tz))
        with self.assertRaises(ValueError):
            _continuation_header(1, 1, 2, 2)
        with self.assertRaises(ValueError):
            _build_header("x", "date", 1, None, 1, "note", 2, 2)
        oversized = _item(title="x" * 5000, url="https://example.com/large")
        block, reason = _fit_block(1, oversized, tz, 3900, 15600)
        self.assertIsNotNone(block)
        self.assertNotEqual(reason, "message_block_too_large")
        result = build_digest([oversized], "MECO", "UTC", max_length=900, issues=["bad"], coverage_notice="coverage", delivery_id=2)
        self.assertTrue(result.messages)
        self.assertTrue(all(utf16_units(message) <= 900 for message in result.messages))


if __name__ == "__main__":
    unittest.main()
