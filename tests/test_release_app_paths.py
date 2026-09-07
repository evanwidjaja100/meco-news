"""Behavioral coverage for the application control plane and CLI boundaries."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, UTC
from io import StringIO
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import meco_news.app as app
from meco_news.collectors import CollectionResult, SourceResult
from meco_news.config import ConfigurationError, load_config
from meco_news.models import NewsItem
from meco_news.storage import StateError, StateStore
from meco_news.telegram import TelegramSendError


ROOT = Path(__file__).resolve().parents[1]


def _config():
    return load_config(ROOT / "config" / "watchlist.json")


def _item(number: int = 0) -> NewsItem:
    return NewsItem(
        title=f"LPG terminal expansion project {number}",
        url=f"https://example.com/market/{number}",
        source="Example Publisher",
        source_url="https://example.com/feed",
        published_at=datetime(2026, 9, 6, tzinfo=UTC),
        summary="A new LPG storage terminal project is under construction.",
        collector="fixture",
        query_name="LPG projects",
        score=25,
        topic="lpg_energy",
        topic_label="LPG storage & downstream energy",
        relevance_reason="Potential tank demand.",
        matches=["lpg", "terminal", "expansion"],
        source_id="fixture",
        source_host="example.com",
    )


def _collection(*, failed: bool = False, items: list[NewsItem] | None = None) -> CollectionResult:
    result = SourceResult(
        "fixture",
        "Fixture source",
        "failed" if failed else "succeeded",
        items=[] if failed else list(items or []),
        accepted_count=0 if failed else len(items or []),
        reason_code="fixture_failed" if failed else "",
        error_class="FixtureError" if failed else "",
    )
    return CollectionResult(list(items or []) if not failed else [], [result], datetime.now(UTC), 4)


def _write_frozen(path: Path, **changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "collected_at": "2026-09-06T00:00:00+00:00",
        "items": [
            {
                "title": "LPG terminal expansion",
                "url": "https://example.com/story",
                "source": "Example Publisher",
                "source_url": "https://example.com/feed",
                "published_at": "2026-09-05T23:00:00+00:00",
                "summary": "A storage project is under construction.",
                "collector": "rss",
                "query_name": "LPG",
                "score": 20,
                "topic": "lpg_energy",
                "topic_label": "LPG storage",
                "relevance_reason": "Potential equipment demand.",
                "matches": ["lpg", "terminal"],
                "source_id": "fixture",
                "source_host": "example.com",
                "quarantine_reason": "",
            }
        ],
        "source_results": [
            {
                "source_id": "fixture",
                "source_name": "Fixture source",
                "outcome": "succeeded",
                "duration_ms": 4,
                "bytes_read": 128,
                "accepted_count": 1,
                "quarantined_count": 0,
                "reason_code": "",
                "error_class": "",
                "error": "",
            },
            {
                "source_id": "degraded",
                "source_name": "Degraded source",
                "outcome": "failed",
                "duration_ms": 9,
                "bytes_read": 64,
                "accepted_count": 0,
                "quarantined_count": 1,
                "reason_code": "timeout",
                "error_class": "TimeoutError",
                "error": "request timed out",
            },
        ],
        "issues": ["Degraded source: timeout"],
        "duration_ms": 13,
        "history": [{"url_key": "old-url", "title_key": "old-title"}],
    }
    payload.update(changes)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _live_env(path: Path) -> dict[str, str]:
    return {
        "STATE_DB": str(path),
        "TELEGRAM_BOT_TOKEN": "123456:valid-token-for-release-tests",
        "TELEGRAM_CHAT_ID": "123456789",
    }


def _seed_delivery(
    path: Path,
    *,
    state: str = "prepared",
    delivery_date: str = "2026-09-07",
    target_snapshot: str = "",
    final_outcome: str | None = None,
) -> int:
    with StateStore(path) as store:
        store.acquire_lease("delivery", "fixture-owner", 180)
        delivery = store.create_delivery(
            delivery_date,
            config_hash="fixture-config",
            owner_id="fixture-owner",
            state="collecting",
            target_snapshot="",
        )
        if state == "retry_wait" and final_outcome is None:
            store.ensure_collection_retry(
                delivery_date,
                run_id="fixture-run",
                config_hash="fixture-config",
                next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
                error="fixture retry",
                owner_id="fixture-owner",
            )
            store.release_lease("delivery", "fixture-owner")
            return store.latest_delivery(delivery_date).delivery_id  # type: ignore[union-attr]
        store.prepare_delivery(
            delivery.delivery_id,
            [],
            ["<b>Fixture delivery</b>"],
            owner_id="fixture-owner",
            target_snapshot=target_snapshot,
        )
        if final_outcome is not None:
            chunk = store.due_chunks(delivery.delivery_id)[0]
            store.begin_chunk_attempt(chunk.chunk_id, run_id="fixture-run", owner_id="fixture-owner")
            if final_outcome == "accepted":
                store.finish_chunk(chunk.chunk_id, final_outcome, run_id="fixture-run", owner_id="fixture-owner", telegram_message_id="99")
            else:
                store.finish_chunk(chunk.chunk_id, final_outcome, run_id="fixture-run", owner_id="fixture-owner", error_text="fixture outcome")
        store.release_lease("delivery", "fixture-owner")
        return delivery.delivery_id


class FrozenInputBoundaryTests(unittest.TestCase):
    def test_full_frozen_payload_and_diagnostics_are_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frozen.json"
            _write_frozen(path)
            collection, history = app._load_frozen_input(path)
            self.assertEqual(len(collection.items), 1)
            self.assertEqual(collection.successful_sources, 1)
            self.assertEqual(collection.failed_sources, 1)
            self.assertEqual(history.identity_keys([]), ({"old-url"}, {"old-title"}))
            item = collection.items[0]
            stderr = StringIO()
            with redirect_stderr(stderr):
                app._print_dry_run(
                    [item],
                    1,
                    collection.issues,
                    exclusions={"future_date": 1},
                    collection=collection,
                )
            text = stderr.getvalue()
            self.assertIn("Sources succeeded: 1; failed: 1", text)
            self.assertIn("Freshness exclusions: future_date=1", text)
            self.assertIn("matches: lpg, terminal", text)
            self.assertIn("Source issues:", text)

    def test_frozen_helpers_and_loader_fail_closed(self) -> None:
        with self.assertRaises(ConfigurationError):
            app._frozen_text(None, "value", 10, required=True)
        with self.assertRaises(ConfigurationError):
            app._frozen_text("x" * 11, "value", 10)
        with self.assertRaises(ConfigurationError):
            app._frozen_text("\ud800", "value", 10)
        with self.assertRaises(ConfigurationError):
            app._frozen_text("bad\x00value", "value", 20)
        with self.assertRaises(ConfigurationError):
            app._frozen_integer(True, "number")
        with self.assertRaises(ConfigurationError):
            app._frozen_integer(-1, "number")
        with self.assertRaises(ConfigurationError):
            app._frozen_integer(101, "number", maximum=100)
        with self.assertRaises(ValueError):
            app._reject_duplicate_json_keys([("x", 1), ("x", 2)])

        bad_items: list[object] = [None, {"title": "x", "url": "u", "source": "s", "unknown": 1}]
        for raw in bad_items:
            with self.assertRaises(ConfigurationError):
                app._frozen_item(raw, 0)
        base_item = {"title": "x", "url": "u", "source": "s"}
        for raw in (
            {**base_item, "published_at": "not-a-date"},
            {**base_item, "published_at": "2026-09-06T00:00:00"},
            {**base_item, "score": True},
            {**base_item, "score": 1_000_000_001},
            {**base_item, "matches": [1]},
        ):
            with self.assertRaises(ConfigurationError):
                app._frozen_item(raw, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ConfigurationError):
                app._load_frozen_input(root / "missing.json")
            invalid = root / "invalid.json"
            invalid.write_text("not-json", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                app._load_frozen_input(invalid)
            cases: list[object] = [
                [],
                {"schema_version": 1},
                {"schema_version": 2},
            ]
            for value in cases:
                invalid.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    app._load_frozen_input(invalid)
            payload = _write_frozen(invalid)
            mutations: list[dict[str, object]] = [
                {**payload, "extra": 1},
                {**payload, "collected_at": "2026-09-06T00:00:00"},
                {**payload, "items": "not-list"},
                {**payload, "source_results": "not-list"},
                {**payload, "source_results": [None]},
                {**payload, "source_results": [{"source_id": "x", "source_name": "x", "outcome": "other"}]},
                {**payload, "duration_ms": -1},
                {**payload, "history": [None]},
                {**payload, "history": [{"url_key": "u", "title_key": "t", "extra": 1}]},
                {**payload, "history": [{"url_key": "", "title_key": "t"}]},
            ]
            for value in mutations:
                invalid.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    app._load_frozen_input(invalid)
            issues_only = dict(payload)
            issues_only["items"] = []
            issues_only["source_results"] = []
            invalid.write_text(json.dumps(issues_only), encoding="utf-8")
            collection, _ = app._load_frozen_input(invalid)
            self.assertTrue(collection.all_sources_failed)

    def test_rank_selection_uses_history_and_top_candidate_mode(self) -> None:
        config = _config()
        collection = _collection(items=[_item()])
        store = MagicMock()
        store.identity_keys.return_value = ({"already-sent"}, {"already-titled"})
        selected, exclusions, sent_urls, sent_titles = app._collect_rank_select(config, collection, store, top_candidates=1)
        self.assertIsInstance(selected, list)
        self.assertIsInstance(exclusions, dict)
        self.assertEqual(sent_urls, {"already-sent"})
        self.assertEqual(sent_titles, {"already-titled"})
        store.identity_keys.assert_called_once()
        selected_ignore, _, ignored_urls, ignored_titles = app._collect_rank_select(
            config, collection, store, ignore_history=True, top_candidates=1
        )
        self.assertTrue(selected_ignore)
        self.assertEqual(ignored_urls, set())
        self.assertEqual(ignored_titles, set())

    def test_history_reader_rejects_old_and_unreadable_connections(self) -> None:
        old = MagicMock(schema_version=1)
        with patch.object(app, "StateStore", return_value=old):
            self.assertIsNone(app._history_reader("old.db"))
        old.close.assert_called_once()
        broken = MagicMock(schema_version=2)
        broken.status_snapshot.side_effect = sqlite3.Error("broken status")
        with patch.object(app, "StateStore", return_value=broken):
            self.assertIsNone(app._history_reader("broken.db"))
        broken.close.assert_called_once()
        with patch.object(app, "StateStore", side_effect=StateError("unreadable")):
            self.assertIsNone(app._history_reader("bad.db"))


class LiveRunBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config()

    def _run(self, path: Path, *, collection: CollectionResult | None = None, **kwargs: object) -> app.RunOutcome:
        with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
            app, "collect_all", return_value=collection or _collection()
        ), patch.object(app.TelegramClient, "send_html", return_value="101"):
            return app.run_once(self.config, **kwargs)

    def test_live_guards_success_and_dry_run_requirement(self) -> None:
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "replace_with_token", "TELEGRAM_CHAT_ID": "1"}, clear=False):
            self.assertEqual(app.run_once(self.config).outcome, "preflight_failed")
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:valid-token", "TELEGRAM_CHAT_ID": "123"}, clear=False):
            self.assertEqual(app.run_once(self.config, force=True).outcome, "invalid_options")
            with self.assertRaises(ConfigurationError):
                app.run_once(self.config, dry_run=True)
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(Path(directory) / "state.db")
            self.assertIn(result.outcome, {"completed", "completed_empty"})

    def test_live_outcome_matrix_for_existing_prepared_delivery(self) -> None:
        cases = [
            (TelegramSendError("telegram_rate_limited", "rate limited", retry_after=1), "retry_wait"),
            (TelegramSendError("telegram_invalid_request", "bad request"), "failed_terminal"),
            (TelegramSendError("telegram_ambiguous", "unknown acceptance"), "needs_attention"),
            (RuntimeError("client unexpectedly failed"), "needs_attention"),
        ]
        for error, expected in cases:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.db"
                _seed_delivery(path)
                with patch.dict(os.environ, _live_env(path), clear=False), patch.object(app.TelegramClient, "send_html", side_effect=error):
                    result = app.run_once(self.config)
                self.assertEqual(result.outcome, expected, type(error).__name__)
                with StateStore(path, readonly=True) as store:
                    current = store.active_delivery(None) or store.latest_delivery("2026-09-07")
                    self.assertIsNotNone(current)
                    self.assertEqual(current.state, expected, type(error).__name__)

    def test_live_existing_delivery_gates_and_collection_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "lease.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "other-owner", 180)
            with patch.dict(os.environ, _live_env(path), clear=False):
                self.assertEqual(app.run_once(self.config).outcome, "already_running")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "complete.db"
            _seed_delivery(path, final_outcome="accepted")
            with patch.dict(os.environ, _live_env(path), clear=False):
                self.assertEqual(app.run_once(self.config).outcome, "already_completed")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attention.db"
            _seed_delivery(path, final_outcome="ambiguous")
            with patch.dict(os.environ, _live_env(path), clear=False):
                self.assertEqual(app.run_once(self.config).outcome, "needs_attention")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retry.db"
            _seed_delivery(path, state="retry_wait")
            with patch.dict(os.environ, _live_env(path), clear=False):
                self.assertEqual(app.run_once(self.config).outcome, "retry_wait")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mismatch.db"
            _seed_delivery(path, target_snapshot="old-target")
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app.TelegramClient, "send_html", side_effect=AssertionError("target mismatch must not send")
            ):
                result = app.run_once(self.config)
            self.assertEqual(result.outcome, "needs_attention")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stopped.db"
            _seed_delivery(path)
            stop = threading.Event()
            stop.set()
            with patch.dict(os.environ, _live_env(path), clear=False):
                self.assertEqual(app.run_once(self.config, stop_event=stop).outcome, "stopped")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "no-predecessor.db"
            with patch.dict(os.environ, _live_env(path), clear=False):
                result = app.run_once(self.config, force=True, force_operator="operator", force_reason="replay")
            self.assertEqual(result.outcome, "failed_terminal")

        no_retry = replace(self.config, retry_policy=replace(self.config.retry_policy, enabled=False))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collect-failed.db"
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app, "collect_all", return_value=_collection(failed=True)
            ):
                result = app.run_once(no_retry)
            self.assertEqual(result.outcome, "failed_terminal")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collect-exception.db"
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app, "collect_all", side_effect=RuntimeError("collector control failure")
            ):
                result = app.run_once(self.config)
            self.assertEqual(result.outcome, "failed_terminal")
            with StateStore(path, readonly=True) as store:
                self.assertEqual(store.latest_delivery("2026-09-07").state, "failed_terminal")  # type: ignore[union-attr]

    def test_force_replay_records_audit_and_sends_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "force.db"
            _seed_delivery(path, final_outcome="accepted")
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app, "collect_all", return_value=_collection()
            ), patch.object(app.TelegramClient, "send_html", return_value="202"):
                result = app.run_once(self.config, force=True, force_operator="operator", force_reason="verified replay")
            self.assertEqual(result.outcome, "completed_empty")
            with StateStore(path, readonly=True) as store:
                audits = store.connection.execute("SELECT COUNT(*) FROM force_audits").fetchone()[0]
                self.assertEqual(audits, 1)
                self.assertEqual(store.latest_generation("2026-09-07"), 1)


class DaemonAndCliBoundaryTests(unittest.TestCase):
    def test_daemon_consumes_typed_terminal_outcome_and_rejects_lost_heartbeat(self) -> None:
        config = _config()

        class FakeLease:
            acquired = True
            owner_id = "daemon-owner"

        class FakeStore:
            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def acquire_lease(self, *_args: object, **_kwargs: object) -> FakeLease:
                return FakeLease()

            def heartbeat_lease(self, *_args: object, **_kwargs: object) -> None:
                return None

            def release_lease(self, *_args: object, **_kwargs: object) -> bool:
                return True

        class DeadThread:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def start(self) -> None:
                return None

            def is_alive(self) -> bool:
                return False

            def join(self, *_args: object, **_kwargs: object) -> None:
                return None

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, _live_env(Path(directory) / "daemon.db"), clear=False),
            patch.object(app, "StateStore", return_value=FakeStore()),
            patch.object(app.threading, "Thread", DeadThread),
            patch.object(app, "_is_due", return_value=False),
            patch.object(app, "_has_recovery_work", return_value=False),
        ):
            self.assertEqual(app.run_daemon(config), 1)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, _live_env(Path(directory) / "daemon.db"), clear=False),
            patch.object(app, "StateStore", return_value=FakeStore()),
            patch.object(app.threading, "Thread", DeadThread),
            patch.object(app, "_is_due", return_value=True),
            patch.object(app, "run_once", return_value=app.RunOutcome(1, "failed_terminal")),
        ):
            self.assertEqual(app.run_daemon(config), 1)

    def test_daemon_preflight_heartbeat_and_reload_helpers(self) -> None:
        config = _config()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "replace_with_token", "TELEGRAM_CHAT_ID": "1"}, clear=False):
            self.assertEqual(app.run_daemon(config), 3)
        self.assertEqual(app.RunOutcome(0, "ok"), 0)
        self.assertNotEqual(app.RunOutcome(1, "bad"), object())
        with patch.object(app, "StateStore", side_effect=OSError("heartbeat state unavailable")), patch.object(app, "emit_event") as emit:
            app._scheduler_heartbeat("unused.db", "owner", 10, threading.Event())
            emit.assert_called()

    def test_cli_modes_fail_closed_and_keep_machine_output_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            alert_file = root / "alerts.jsonl"
            env = {**_live_env(state), "LOG_FILE": str(root / "log.jsonl")}
            try:
                with patch.dict(os.environ, env, clear=False):
                    stdout = StringIO()
                    with redirect_stdout(stdout):
                        self.assertEqual(app.main(["--metrics", "--json"]), 0)
                    self.assertEqual(json.loads(stdout.getvalue())["state"], "missing")
                    stdout = StringIO()
                    with redirect_stdout(stdout):
                        self.assertEqual(app.main(["--healthcheck", "--alert-file", str(alert_file), "--json"]), 1)
                    self.assertIn("alert_receipts", json.loads(stdout.getvalue()))

                    bad_config = root / "bad.json"
                    bad_config.write_text('{"unknown": true}', encoding="utf-8")
                    self.assertEqual(app.main(["--config", str(bad_config), "--json"]), 2)

                    with patch.object(app, "create_backup", side_effect=OSError("backup failed")):
                        self.assertEqual(app.main(["--backup", str(root / "backup"), "--json"]), 1)
                    with patch.object(app, "restore_backup", side_effect=OSError("restore failed")):
                        self.assertEqual(app.main(["--restore", str(root / "backup.db"), "--json"]), 1)
                    self.assertEqual(app.main(["--migrate", "--to-version", "999", "--json"]), 2)

                    with patch.object(app, "run_once", return_value=app.RunOutcome(0, "test", {"count": 1})):
                        stdout = StringIO()
                        with redirect_stdout(stdout):
                            self.assertEqual(app.main(["--json"]), 0)
                        self.assertEqual(json.loads(stdout.getvalue())["outcome"], "test")
                    with patch.object(app, "run_once", side_effect=OSError("run failed")):
                        self.assertEqual(app.main(["--json"]), 1)
            finally:
                for handler in list(logging.getLogger().handlers):
                    handler.close()
                    logging.getLogger().removeHandler(handler)

    def test_cli_telegram_discovery_and_migration_success(self) -> None:
        class FakeTelegram:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def discover_chats(self) -> list[dict[str, str]]:
                return []

            def get_me(self) -> dict[str, str]:
                return {"username": "fixture_bot"}

            def send_html(self, _payload: str) -> str:
                return "303"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            with patch.dict(os.environ, _live_env(state), clear=False), patch.object(app, "TelegramClient", FakeTelegram):
                self.assertEqual(app.main(["--discover-chat", "--json"]), 1)
                self.assertEqual(app.main(["--test-telegram", "--json"]), 0)
            with patch.dict(os.environ, _live_env(state), clear=False), patch.object(
                app, "run_guarded_migrations", return_value=[1, 2]
            ):
                self.assertEqual(app.main(["--migrate", "--to-version", str(app.CURRENT_SCHEMA_VERSION), "--json"]), 0)


if __name__ == "__main__":
    unittest.main()
