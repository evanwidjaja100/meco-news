"""C1.4 RED suite - safe structured logging and lifecycle identity.

Covers closure-plan C1.4: nested token/cookie/authorization/.env/URL-query
canaries across config, exception, stack, DB, source, and Telegram paths;
stdout/stderr stream capture; early return, success, retryable, ambiguous,
terminal, exception, and recovery paths grouped by attempt ID with exactly
one terminal record; valid Unicode retained while prohibited controls/bidi
are absent.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path


RAW_TOKEN = "123456789:AAH-canary-abcdefghijklmnopqrstuvwxyz012"
BEARER = "Bearer bearer-canary-xyz123"
QUOTED_JSON = '{"token": "quoted-canary-xyz"}'
EMBEDDED_URL = "see https://user:userinfo-canary@example.com/path?token=query-canary&x=1 now"
ENV_STYLE = "TELEGRAM_APIKEY=env-canary-123"


def _reset_logging() -> None:
    for handler in list(logging.getLogger().root.handlers):
        with contextlib.suppress(Exception):
            handler.close()
        logging.getLogger().root.removeHandler(handler)


class TestC14NestedCanaries(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_logging()

    def test_raw_telegram_token_redacted(self) -> None:
        from meco_news.observability import redact

        self.assertNotIn("canary", redact(RAW_TOKEN))

    def test_bearer_and_quoted_json_redacted(self) -> None:
        from meco_news.observability import redact

        self.assertNotIn("canary", redact(BEARER))
        self.assertNotIn("canary", redact(QUOTED_JSON))

    def test_embedded_url_userinfo_query_stripped(self) -> None:
        from meco_news.observability import redact

        out = redact(EMBEDDED_URL)
        self.assertNotIn("userinfo-canary", out)
        self.assertNotIn("query-canary", out)

    def test_sensitive_key_variants_redacted(self) -> None:
        from meco_news.observability import redact

        payload = {
            "api-key": "hyphen-canary",
            "private_key": "private-canary",
            "auth": "auth-canary",
            "nested": {"headers": {"x-auth-token": "nested-canary"}},
            "items": [{"session": "session-canary"}],
        }
        out = json.dumps(redact(payload))
        for canary in ("hyphen-canary", "private-canary", "auth-canary", "nested-canary", "session-canary"):
            self.assertNotIn(canary, out)

    def test_env_style_secret_redacted(self) -> None:
        from meco_news.observability import redact

        self.assertNotIn("env-canary", redact(ENV_STYLE))

    def test_exception_and_stack_redacted(self) -> None:
        from meco_news.observability import JsonEventFormatter

        try:
            raise ValueError("token=stack-canary-123 " + RAW_TOKEN)
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord("t", logging.ERROR, "p", 1, "msg", None, exc_info)
        record.event_name = "ev"
        record.event_fields = {"detail": QUOTED_JSON, "url": EMBEDDED_URL}
        out = JsonEventFormatter().format(record)
        for canary in ("stack-canary", "quoted-canary", "query-canary", "userinfo-canary"):
            self.assertNotIn(canary, out)
        self.assertIn("ValueError", out)

    def test_bytes_sanitized_to_string(self) -> None:
        from meco_news.observability import redact

        out = redact(b"token=bytes-canary-123")
        self.assertIsInstance(out, str)
        self.assertNotIn("bytes-canary", out)

    def test_unicode_retained_controls_stripped(self) -> None:
        from meco_news.observability import redact

        text = "Pabrik kelapa sawit uraian ringkas " + "\u200b\ufeff\u202e" + "selesai"
        out = redact(text)
        self.assertIn("Pabrik kelapa sawit", out)
        for bad in ("\u200b", "\ufeff", "\u202e", "\u202a", "\u2066", "\x00"):
            self.assertNotIn(bad, out)


class TestC14Streams(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_logging()

    def test_stdout_json_no_stderr_no_canary(self) -> None:
        import sys as sys_mod

        from meco_news.observability import configure_logging, emit_event

        stdout = io.StringIO()
        stderr = io.StringIO()
        old_out, old_err = sys_mod.stdout, sys_mod.stderr
        sys_mod.stdout, sys_mod.stderr = stdout, stderr
        try:
            configure_logging(level="INFO")
            emit_event("run_terminal", outcome="completed", detail=RAW_TOKEN, extra=QUOTED_JSON)
            out = stdout.getvalue()
            err = stderr.getvalue()
        finally:
            sys_mod.stdout, sys_mod.stderr = old_out, old_err
            _reset_logging()
        self.assertEqual(err, "")
        self.assertTrue(out.strip(), "expected JSON log on stdout")
        for line in out.strip().splitlines():
            json.loads(line)
        self.assertNotIn("canary", out)


class TestC14Lifecycle(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_logging()

    def test_finalizer_exactly_once(self) -> None:
        from meco_news.observability import AttemptLifecycle

        attempt = AttemptLifecycle(kind="chunk", run_id="run-1", attempt_id="att-1", delivery_id=1, chunk_id=2)
        first = attempt.finalize("success", outcome="completed")
        self.assertTrue(attempt.finalized)
        with self.assertRaises(RuntimeError):
            attempt.finalize("success", outcome="completed")
        self.assertEqual(first["attempt_id"], "att-1")
        self.assertEqual(first["kind"], "chunk")

    def test_all_attempt_kinds_exactly_one_terminal(self) -> None:
        import sys as sys_mod

        from meco_news.observability import AttemptLifecycle, configure_logging

        kinds = ("command", "collection", "delivery", "chunk")
        outcomes = ("completed", "retry_wait", "needs_attention", "failed_terminal")
        stdout = io.StringIO()
        old_out = sys_mod.stdout
        sys_mod.stdout = stdout
        try:
            configure_logging(level="INFO")
            for kind, outcome in zip(kinds, outcomes, strict=True):
                attempt = AttemptLifecycle(kind=kind, run_id="run-kinds", attempt_id=f"att-{kind}")
                attempt.finalize("success" if outcome == "completed" else "terminal", outcome=outcome)
        finally:
            sys_mod.stdout = old_out
            _reset_logging()
        lines = [line for line in stdout.getvalue().strip().splitlines() if line.strip()]
        by_attempt: dict[str, int] = {}
        for line in lines:
            payload = json.loads(line)
            if payload.get("event") == "attempt_terminal":
                by_attempt[payload["attempt_id"]] = by_attempt.get(payload["attempt_id"], 0) + 1
        for kind in kinds:
            self.assertEqual(by_attempt.get(f"att-{kind}"), 1, kind)

    def test_recovery_grouped_by_attempt_id(self) -> None:
        from meco_news.observability import AttemptLifecycle

        first = AttemptLifecycle(kind="delivery", run_id="run-r", attempt_id="att-r1")
        first.finalize("retryable", outcome="retry_wait")
        second = AttemptLifecycle(kind="delivery", run_id="run-r", attempt_id="att-r2", recovery_of="att-r1")
        record = second.finalize("success", outcome="completed")
        self.assertEqual(record["recovery_of"], "att-r1")
        self.assertTrue(first.finalized)
        self.assertTrue(second.finalized)


class TestC14Persistence(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_logging()

    def test_chunk_error_text_sanitized_in_state_and_status(self) -> None:
        from datetime import UTC, datetime

        from meco_news.config import load_config
        from meco_news.models import NewsItem
        from meco_news.storage import StateStore

        load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                lease = store.acquire_lease("delivery", "owner-1", 180)
                self.assertTrue(lease.acquired)
                delivery = store.create_delivery("2026-09-06", config_hash="h")
                item = NewsItem(
                    title="LPG terminal project",
                    url="https://example.com/lpg-canary",
                    source="S",
                    published_at=datetime.now(UTC),
                    score=10,
                    topic="lpg_energy",
                )
                store.prepare_delivery(delivery.delivery_id, [item], ["<b>hello</b>"], owner_id="owner-1")
                chunks = store.due_chunks(delivery.delivery_id)
                self.assertTrue(chunks)
                chunk = chunks[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="run-1", owner_id="owner-1")
                evil = "telegram_ambiguous " + RAW_TOKEN + " " + EMBEDDED_URL
                store.finish_chunk(
                    chunk.chunk_id,
                    "ambiguous",
                    run_id="run-1",
                    owner_id="owner-1",
                    error_class="telegram_ambiguous",
                    error_text=evil,
                )
                snapshot = store.status_snapshot()
        blob = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("canary", blob)
        self.assertNotIn("userinfo-canary", blob)
        self.assertIn("telegram_ambiguous", blob)

    def test_config_redacted_recursive(self) -> None:
        from meco_news.observability import redact

        from meco_news.config import load_config

        config = load_config("config/watchlist.json")
        raw = config.as_dict()
        raw["telegram"] = {"bot_token": "nested-config-canary"}
        out = json.dumps(redact(raw))
        self.assertNotIn("nested-config-canary", out)


if __name__ == "__main__":
    unittest.main()
class TestC14Hardening(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_logging()

    def test_invalid_kind_and_empty_outcome_rejected(self) -> None:
        from meco_news.observability import AttemptLifecycle

        with self.assertRaises(ValueError):
            AttemptLifecycle(kind="nope", run_id="r", attempt_id="a").finalize("terminal", outcome="failed_terminal")
        with self.assertRaises(ValueError):
            AttemptLifecycle(kind="chunk", run_id="r", attempt_id="a").finalize("terminal", outcome="")

    def test_url_edge_cases(self) -> None:
        from meco_news.observability import redact

        trailed = redact("see https://example.com/a?token=edge-canary-1.")
        self.assertNotIn("edge-canary-1", trailed)
        self.assertIn("https://example.com/a", trailed)
        ported = redact("https://example.com:8443/a?token=edge-canary-2")
        self.assertNotIn("edge-canary-2", ported)
        self.assertIn("https://example.com:8443/a", ported)
        bad_port = redact("https://example.com:badport/a?token=edge-canary-3")
        self.assertNotIn("edge-canary-3", bad_port)
        self.assertNotIn("badport", bad_port)
        self.assertIn("https://example.com/a", bad_port)
        bad_ipv6 = redact("fetch https://[::1/path now")
        self.assertNotIn("::1", bad_ipv6)
        no_host = redact("https:///path?token=edge-canary-4")
        self.assertNotIn("edge-canary-4", no_host)

    def test_hostile_caps(self) -> None:
        from meco_news.observability import redact

        nested: object = "depth-canary"
        for _ in range(20):
            nested = {"layer": nested}
        self.assertNotIn("depth-canary", json.dumps(redact(nested)))
        payload: dict[str, object] = {"api_key": "cap-canary"}
        payload.update({f"field_{index}": f"value-{index}" for index in range(105)})
        cleaned = redact(payload)
        self.assertNotIn("cap-canary", json.dumps(cleaned))
        self.assertIn("<truncated>", cleaned)
        self.assertLessEqual(len(cleaned), 101)
        long_text = redact("token=long-canary " + "x" * 3000)
        self.assertNotIn("long-canary", long_text)
        self.assertLessEqual(len(long_text), 2000)
        many = redact([f"item-{index}" for index in range(150)])
        self.assertEqual(len(many), 100)

    def test_accepted_run_exactly_one_terminal_per_attempt(self) -> None:
        import os
        from datetime import UTC, datetime
        from unittest.mock import patch

        from meco_news.collectors import CollectionResult, SourceResult
        from meco_news.config import load_config
        from meco_news.observability import configure_logging

        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory) / "state.db")
            env = {"STATE_DB": state, "TELEGRAM_BOT_TOKEN": "123456:real-token-value", "TELEGRAM_CHAT_ID": "99"}
            ok = SourceResult("s", "S", "succeeded", items=[], accepted_count=0)
            collection = CollectionResult([], [ok], datetime.now(UTC), 1)

            class FakeTelegram:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    self.sent: list[str] = []

                def send_html(self, text: str) -> str:
                    self.sent.append(text)
                    return str(len(self.sent))

            stdout, stderr = io.StringIO(), io.StringIO()
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = stdout, stderr
            try:
                configure_logging(level="INFO")
                with (
                    patch.dict(os.environ, env, clear=False),
                    patch("meco_news.app.collect_all", return_value=collection),
                    patch("meco_news.app.TelegramClient", FakeTelegram),
                ):
                    from meco_news.app import run_once

                    outcome = run_once(load_config("config/watchlist.json"))
                out, err = stdout.getvalue(), stderr.getvalue()
            finally:
                sys.stdout, sys.stderr = old_out, old_err
                _reset_logging()
            self.assertEqual(int(outcome), 0)
            self.assertEqual(err, "")
            terminals: dict[str, int] = {}
            kinds: dict[str, str] = {}
            for line in out.strip().splitlines():
                payload_line = json.loads(line)
                if payload_line.get("event") == "attempt_terminal":
                    attempt_id = payload_line["attempt_id"]
                    terminals[attempt_id] = terminals.get(attempt_id, 0) + 1
                    kinds[attempt_id] = payload_line["kind"]
            self.assertGreaterEqual(len(terminals), 1)
            for attempt_id, count in terminals.items():
                self.assertEqual(count, 1, attempt_id)
            self.assertIn("collection", set(kinds.values()))
            self.assertNotIn("canary", out)

    def test_collection_exception_canary_never_persists(self) -> None:
        import os
        from unittest.mock import patch

        from meco_news.config import load_config
        from meco_news.observability import configure_logging
        from meco_news.storage import StateStore

        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory) / "state.db")
            env = {"STATE_DB": state, "TELEGRAM_BOT_TOKEN": "123456:real-token-value", "TELEGRAM_CHAT_ID": "99"}
            evil = "collector blew up token=flow-canary-9 " + RAW_TOKEN
            stdout, stderr = io.StringIO(), io.StringIO()
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = stdout, stderr
            try:
                configure_logging(level="INFO")
                with (
                    patch.dict(os.environ, env, clear=False),
                    patch("meco_news.app.collect_all", side_effect=ValueError(evil)),
                ):
                    from meco_news.app import run_once

                    outcome = run_once(load_config("config/watchlist.json"))
                out = stdout.getvalue()
            finally:
                sys.stdout, sys.stderr = old_out, old_err
                _reset_logging()
            self.assertEqual(outcome.outcome, "failed_terminal")
            self.assertNotIn("flow-canary", out)
            self.assertNotIn("canary", out)
            with StateStore(state, readonly=True) as store:
                blob = json.dumps(store.status_snapshot(), ensure_ascii=False)
            self.assertNotIn("canary", blob)
            self.assertIn("ValueError", blob)
    def test_non_mapping_event_fields_ignored(self) -> None:
        from meco_news.observability import JsonEventFormatter

        record = logging.LogRecord("t", logging.INFO, "p", 1, "msg", None, None)
        record.event_name = "ev-nonmapping"
        record.event_fields = ["k", "v"]
        out = JsonEventFormatter().format(record)
        self.assertIn("ev-nonmapping", out)
    def test_sanitize_error_non_string_and_config_guard(self) -> None:
        from unittest.mock import patch

        from meco_news.config import load_config
        from meco_news.storage import _sanitize_error

        self.assertEqual(_sanitize_error(None), "None")
        self.assertEqual(_sanitize_error(123), "123")
        config = load_config("config/watchlist.json")
        with patch("meco_news.config._redact_log_value", return_value=["not-a-dict"]):
            self.assertEqual(config.redacted(), {})