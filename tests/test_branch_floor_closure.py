"""Behavioral tests closing the branch-coverage release-gate gap.

Each test traverses previously uncovered behavior measured with branch
coverage (see scripts/coverage_gate.py): digest edge cases, frozen-input
caps, retry-resume paths, scheduler recovery probes, daemon/CLI
boundaries, and scheduler heartbeat. Nothing here exists for coverage
alone; every case asserts an operator-visible outcome.
"""

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
import unittest
from unittest.mock import patch

import meco_news.app as app
from meco_news.collectors import CollectionResult, SourceResult
from meco_news.config import ConfigurationError, load_config
from meco_news.models import NewsItem
from meco_news.storage import StateError, StateStore


ROOT = Path(__file__).resolve().parents[1]


def _config():
    return load_config(ROOT / "config" / "watchlist.json")


def _today() -> str:
    return app._delivery_date(_config())


def _live_env(path: Path) -> dict[str, str]:
    return {
        "STATE_DB": str(path),
        "TELEGRAM_BOT_TOKEN": "synthetic-valid-token-for-closure-tests",
        "TELEGRAM_CHAT_ID": "123456789",
    }


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
                "score": 95,
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
        ],
        "issues": ["Degraded source: timeout"],
        "duration_ms": 13,
        "history": [{"url_key": "old-url", "title_key": "old-title"}],
    }
    payload.update(changes)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _seed_prepared(path: Path, *, retry_policy: dict[str, object] | None = None) -> int:
    with StateStore(path) as store:
        store.acquire_lease("delivery", "fixture-owner", 180)
        kwargs: dict[str, object] = {}
        if retry_policy is not None:
            kwargs["retry_policy"] = retry_policy
        delivery = store.create_delivery(
            _today(),
            config_hash="fixture-config",
            owner_id="fixture-owner",
            state="collecting",
            target_snapshot="",
            **kwargs,
        )
        store.prepare_delivery(
            delivery.delivery_id,
            [_item(7)],
            ["<b>Fixture delivery</b>"],
            owner_id="fixture-owner",
            target_snapshot="",
        )
        store.release_lease("delivery", "fixture-owner")
        return delivery.delivery_id


def _past() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=5)


def _run_main(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    with patch.dict(os.environ, env, clear=False), redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            return app.main(argv), stdout.getvalue(), stderr.getvalue()
        finally:
            for handler in list(logging.getLogger().handlers):
                handler.close()
                logging.getLogger().removeHandler(handler)




class TelegramDigestEdgeTests(unittest.TestCase):
    def _digest_item(self, title: str = "Judul proyek gas", url: str = "https://example.com/a") -> NewsItem:
        return NewsItem(
            title=title,
            url=url,
            source="S",
            published_at=datetime(2026, 8, 24, tzinfo=UTC),
            topic_label="T",
            relevance_reason="R",
            summary="Ringkasan singkat.",
        )

    def test_first_item_oversized_is_omitted_but_header_survives(self) -> None:
        from meco_news import telegram

        oversized = self._digest_item(url="https://example.com/" + "u" * 2040)
        result = telegram.build_digest([oversized], "MECO", "UTC", max_length=3900, max_bytes=1200)
        self.assertEqual(len(result.omitted_items), 1)
        self.assertTrue(result.messages)

    def test_separator_overflow_flushes_and_resumes_in_continuation(self) -> None:
        from meco_news import telegram

        items = [
            self._digest_item(title=f"Cerita berbeda {index} tentang konstruksi kilang minyak dan gas")
            for index in range(2)
        ]
        for item in items:
            item.summary = "Ringkasan " * 30
        result = telegram.build_digest(items, "MECO", "UTC", max_length=700)
        self.assertEqual(len(result.messages), 2)
        self.assertEqual(len(result.omitted_items), 0)

    def test_coverage_note_standalone_message_when_last_chunk_is_full(self) -> None:
        from meco_news import telegram

        items = [
            self._digest_item(title=f"Cerita berbeda {index} tentang konstruksi kilang minyak dan gas")
            for index in range(2)
        ]
        for item in items:
            item.summary = "Ringkasan " * 30
        result = telegram.build_digest(items, "MECO", "UTC", issues=["sumber satu", "sumber dua"], max_length=500)
        self.assertTrue(any(message.startswith("<i>Coverage note:") for message in result.messages))

    def test_oversized_coverage_note_is_dropped_not_forged(self) -> None:
        from meco_news import telegram

        oversized = self._digest_item(url="https://example.com/" + "u" * 2040)
        result = telegram.build_digest(
            [self._digest_item(), oversized],
            "MECO",
            "UTC",
            issues=["a", "b", "c"],
            max_length=100,
            max_bytes=1200,
        )
        self.assertEqual(len(result.omitted_items), 2)
        self.assertFalse(any("Coverage note" in message for message in result.messages))


class ProtocolAndFormatterTests(unittest.TestCase):
    def test_alert_sink_members_execute_their_default_body(self) -> None:
        from meco_news.alerts import AlertRecord, AlertSink

        class _Probe(AlertSink):
            pass

        record = AlertRecord("a", "s", "t", "f", "st", "d", "ts", "r")
        self.assertIsNone(_Probe().publish(record))
        self.assertIsNone(_Probe().reconcile(set()))

    def test_plain_log_record_keeps_redacted_message_field(self) -> None:
        import logging

        from meco_news.observability import JsonEventFormatter

        record = logging.LogRecord("t", logging.INFO, "p", 1, "hello %s", ("world",), None)
        payload = json.loads(JsonEventFormatter().format(record))
        self.assertEqual(payload["event"], "log_message")
        self.assertEqual(payload["message"], "hello world")

    def test_migration_artifact_reservation_failure_is_terminal(self) -> None:
        from meco_news import migrate

        with tempfile.TemporaryDirectory() as directory, patch.object(
            migrate, "_reserve_file", return_value=False
        ), self.assertRaises(StateError):
            migrate._reserve_migration_artifacts(Path(directory) / "source.db", "20260908T000000")

    def test_run_outcome_value_equality(self) -> None:
        self.assertEqual(app.RunOutcome(0, "completed"), app.RunOutcome(0, "completed"))
        self.assertNotEqual(app.RunOutcome(0, "completed"), app.RunOutcome(0, "completed_empty"))
        self.assertEqual(int(app.RunOutcome(0, "completed")), 0)

    def test_next_delivery_after_window_is_tomorrow(self) -> None:
        from meco_news.timezones import get_timezone

        config = {"timezone": "UTC", "delivery_time": "00:00"}
        target = app._next_delivery(config)
        now = datetime.now(get_timezone("UTC"))
        self.assertEqual(target.date(), (now + timedelta(days=1)).date())


class FrozenInputCapTests(unittest.TestCase):
    def test_oversized_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "_FROZEN_INPUT_MAX_BYTES", 16):
            path = Path(directory) / "frozen.json"
            path.write_bytes(b"x" * 32)
            with self.assertRaises(ConfigurationError):
                app._load_frozen_input(path)

    def test_unknown_source_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frozen.json"
            payload = _write_frozen(path)
            sources = list(payload["source_results"])  # type: ignore[union-attr]
            sources[0] = {**sources[0], "bogus_field": 1}  # type: ignore[misc]
            payload["source_results"] = sources
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                app._load_frozen_input(path)

    def test_history_cap_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "_FROZEN_INPUT_MAX_HISTORY", 1):
            path = Path(directory) / "frozen.json"
            _write_frozen(path, history=[{"url_key": "a", "title_key": "b"}, {"url_key": "c", "title_key": "d"}])
            with self.assertRaises(ConfigurationError):
                app._load_frozen_input(path)

    def test_frozen_dry_run_selects_quality_items_and_reports_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frozen.json"
            _write_frozen(path)
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = app.run_once(_config(), dry_run=True, frozen_input=path)
            self.assertEqual(result.outcome, "dry_run")
            self.assertIn("selected 1 unsent", stderr.getvalue())


class RecoveryProbeTests(unittest.TestCase):
    def test_empty_state_has_no_recovery_work_or_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.db"
            with StateStore(path):
                pass
            with patch.dict(os.environ, {"STATE_DB": str(path)}, clear=False):
                self.assertFalse(app._has_recovery_work(_config()))
                self.assertIsNone(app._next_durable_retry())

    def test_prepared_state_is_recovery_work_completed_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            waiting = Path(directory) / "waiting.db"
            _seed_prepared(waiting)
            with patch.dict(os.environ, {"STATE_DB": str(waiting)}, clear=False):
                self.assertTrue(app._has_recovery_work(_config()))
            done = Path(directory) / "done.db"
            with StateStore(done) as store:
                store.acquire_lease("delivery", "fixture-owner", 180)
                delivery = store.create_delivery(
                    _today(), config_hash="fixture-config", owner_id="fixture-owner", state="collecting"
                )
                store.prepare_delivery(
                    delivery.delivery_id, [], ["<b>Fixture delivery</b>"], owner_id="fixture-owner"
                )
                chunk = store.due_chunks(delivery.delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="fixture-run", owner_id="fixture-owner")
                store.finish_chunk(
                    chunk.chunk_id, "accepted", run_id="fixture-run", owner_id="fixture-owner", telegram_message_id="9"
                )
                store.release_lease("delivery", "fixture-owner")
            with patch.dict(os.environ, {"STATE_DB": str(done)}, clear=False):
                self.assertFalse(app._has_recovery_work(_config()))

    def test_abandoned_delivery_with_spent_budget_is_unhealthy(self) -> None:
        from meco_news.preflight import healthcheck

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stuck.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "fixture-owner", 180)
                created = store.create_delivery(
                    _today(),
                    config_hash="fixture-config",
                    state="collecting",
                    owner_id="fixture-owner",
                    retry_policy={"enabled": True, "max_attempts": 1, "max_elapsed_seconds": 604_800},
                )
                store.record_collection_attempt(
                    created.delivery_id, run_id="fixture-run", error="boom", outcome="collection_retry",
                    owner_id="fixture-owner",
                )
                store.release_lease("delivery", "fixture-owner")
            healthy, report = healthcheck(_config(), state_path=path)
            self.assertFalse(healthy)
            self.assertIn("abandoned_delivery", report["reasons"])


class RetryResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config()

    def _seed_collection_retry(self, path: Path, *, attempts: int = 0) -> int:
        with StateStore(path) as store:
            store.acquire_lease("delivery", "fixture-owner", 180)
            created = store.ensure_collection_retry(
                _today(),
                run_id="fixture-run",
                config_hash="fixture-config",
                next_attempt_at=_past(),
                error="fixture retry",
                owner_id="fixture-owner",
            )
            for _ in range(attempts):
                store.record_collection_attempt(
                    created.delivery_id, run_id="fixture-run", error="boom", outcome="collection_retry",
                    owner_id="fixture-owner",
                )
            store.release_lease("delivery", "fixture-owner")
            return created.delivery_id

    def test_collection_retry_reopens_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            self._seed_collection_retry(path)
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app, "collect_all", return_value=_collection(items=[_item(0), _item(1)])
            ) as collect, patch.object(app.TelegramClient, "send_html", return_value="101"):
                result = app.run_once(self.config)
            collect.assert_called_once()
            self.assertIn(result.outcome, {"completed", "completed_empty"})

    def test_exhausted_collection_budget_fails_terminal(self) -> None:
        limited = replace(self.config, retry_policy=replace(self.config.retry_policy, max_attempts=1))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            self._seed_collection_retry(path, attempts=1)
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app, "collect_all", side_effect=AssertionError("exhausted budget must not recollect")
            ):
                result = app.run_once(limited)
            self.assertEqual(result.outcome, "failed_terminal")
            with StateStore(path, readonly=True) as store:
                latest = store.latest_delivery(_today())
                assert latest is not None
                self.assertEqual(latest.state, "failed_terminal")

    def test_content_retry_resumes_frozen_chunks_without_recollecting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            delivery_id = _seed_prepared(path)
            connection = sqlite3.connect(str(path))
            try:
                connection.execute(
                    "UPDATE deliveries SET state='retry_wait', next_attempt_at=? WHERE delivery_id=?",
                    (_past().isoformat(), delivery_id),
                )
                connection.commit()
            finally:
                connection.close()
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app, "collect_all", side_effect=AssertionError("frozen chunks must not be recollected")
            ), patch.object(app.TelegramClient, "send_html", return_value="102"):
                result = app.run_once(self.config)
            self.assertIn(result.outcome, {"completed", "completed_empty"})

    def test_terminal_failure_blocks_a_new_generation(self) -> None:
        no_retry = replace(self.config, retry_policy=replace(self.config.retry_policy, enabled=False))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app, "collect_all", return_value=_collection(failed=True)
            ):
                first = app.run_once(no_retry)
            self.assertEqual(first.outcome, "failed_terminal")
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app, "collect_all", side_effect=AssertionError("terminal state must block collection")
            ), patch.object(app.TelegramClient, "send_html", return_value="103"):
                second = app.run_once(self.config)
            self.assertEqual(second.outcome, "failed_terminal")

    def test_chunk_budget_exhausted_in_send_loop_fails_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            delivery_id = _seed_prepared(path)
            connection = sqlite3.connect(str(path))
            try:
                connection.execute("UPDATE outbox_chunks SET attempt_count=99 WHERE delivery_id=?", (delivery_id,))
                connection.commit()
            finally:
                connection.close()
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app.TelegramClient, "send_html", side_effect=AssertionError("exhausted chunk must not send")
            ):
                result = app.run_once(self.config)
            self.assertEqual(result.outcome, "failed_terminal")

    def test_stored_disabled_policy_cannot_wedge_a_prepared_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _seed_prepared(
                path, retry_policy={"enabled": False, "max_attempts": 4, "max_elapsed_seconds": 604_800}
            )
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app.TelegramClient, "send_html", return_value="104"
            ):
                result = app.run_once(self.config)
            self.assertIn(result.outcome, {"completed", "completed_empty"})

    def test_policy_read_failure_falls_back_to_live_config(self) -> None:
        real_policy = app.StateStore.delivery_retry_policy

        def _flaky(self: StateStore, delivery_id: int, fallback: object = None) -> dict[str, object]:
            if isinstance(fallback, dict) and "enabled" in fallback:
                raise StateError("policy unreadable")
            return real_policy(self, delivery_id, fallback)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _seed_prepared(path)
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app.StateStore, "delivery_retry_policy", autospec=True, side_effect=_flaky
            ), patch.object(app.TelegramClient, "send_html", return_value="105"):
                result = app.run_once(self.config)
            self.assertIn(result.outcome, {"completed", "completed_empty"})

    def test_shutdown_with_inflight_chunk_needs_attention(self) -> None:
        import threading

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            delivery_id = _seed_prepared(path)
            with StateStore(path) as store:
                store.acquire_lease("delivery", "fixture-owner", 180)
                chunk = store.due_chunks(delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="fixture-run", owner_id="fixture-owner")
                store.release_lease("delivery", "fixture-owner")
            stop = threading.Event()
            stop.set()
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app.TelegramClient, "send_html", side_effect=AssertionError("shutdown must precede send")
            ):
                result = app.run_once(self.config, stop_event=stop)
            self.assertEqual(result.outcome, "needs_attention")

    def test_double_fault_after_inflight_mark_needs_attention(self) -> None:
        real_finish = app.StateStore.finish_chunk

        def _flaky_finish(self: StateStore, chunk_id: int, outcome: str, *args: object, **kwargs: object) -> object:
            if outcome == "ambiguous":
                raise StateError("commit failed after send")
            return real_finish(self, chunk_id, outcome, *args, **kwargs)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            _seed_prepared(path)
            with patch.dict(os.environ, _live_env(path), clear=False), patch.object(
                app.TelegramClient, "send_html", side_effect=RuntimeError("client exploded")
            ), patch.object(app.StateStore, "finish_chunk", autospec=True, side_effect=_flaky_finish):
                result = app.run_once(self.config)
            self.assertEqual(result.outcome, "needs_attention")


class SchedulerAndDaemonTests(unittest.TestCase):
    def test_scheduler_heartbeat_exits_when_already_stopped(self) -> None:
        import threading

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "beat.db"
            with StateStore(path):
                pass
            stop = threading.Event()
            stop.set()
            app._scheduler_heartbeat(str(path), "owner", 10, stop)

    def test_scheduler_heartbeat_beats_once_then_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "beat.db"
            with StateStore(path):
                pass

            class _Flip:
                def __init__(self) -> None:
                    self.calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 1

            app._scheduler_heartbeat(str(path), "owner", 10, _Flip())  # type: ignore[arg-type]

    def test_daemon_reports_an_existing_scheduler(self) -> None:
        config = _config()

        class _HeldLease:
            acquired = False
            owner_id = "other-owner"

        class _HeldStore:
            def __enter__(self) -> _HeldStore:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def acquire_lease(self, *_args: object, **_kwargs: object) -> _HeldLease:
                return _HeldLease()

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, _live_env(Path(directory) / "daemon.db"), clear=False
        ), patch.object(app, "StateStore", return_value=_HeldStore()):
            self.assertEqual(app.run_daemon(config), 0)


class CliValidationTests(unittest.TestCase):
    def test_force_with_dry_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = _live_env(Path(directory) / "state.db")
            with patch.dict(os.environ, env, clear=False), redirect_stderr(StringIO()), self.assertRaises(
                SystemExit
            ) as raised:
                app.main(["--dry-run", "--frozen-input", "x", "--force", "--reason", "r", "--operator", "o"])
        self.assertEqual(raised.exception.code, 2)

    def test_backup_failure_without_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app, "create_backup", side_effect=OSError("backup failed")):
                code, _out, _err = _run_main(["--backup", str(root / "backup")], _live_env(root / "state.db"))
        self.assertEqual(code, 1)

    def test_restore_failure_without_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app, "restore_backup", side_effect=OSError("restore failed")):
                code, _out, _err = _run_main(["--restore", str(root / "backup.db")], _live_env(root / "state.db"))
        self.assertEqual(code, 1)

    def test_resolve_failure_without_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, _out, _err = _run_main(
                ["--resolve-chunk", "1", "--resolution", "sent", "--reason", "r", "--operator", "o"],
                _live_env(root / "missing.db"),
            )
        self.assertEqual(code, 1)

    def test_migrate_failure_without_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app, "run_guarded_migrations", side_effect=OSError("migration failed")):
                code, _out, _err = _run_main(
                    ["--migrate", "--to-version", str(app.CURRENT_SCHEMA_VERSION)], _live_env(root / "state.db")
                )
        self.assertEqual(code, 1)

    def test_telegram_placeholder_reports_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**_live_env(root / "state.db"), "TELEGRAM_BOT_TOKEN": "replace_with_token"}
            code, out, _err = _run_main(["--test-telegram", "--json"], env)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["outcome"], "telegram_test_failed")

    def test_discover_chat_without_json(self) -> None:
        class _EmptyTelegram:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def discover_chats(self) -> list[dict[str, str]]:
                return []

        class _FullTelegram(_EmptyTelegram):
            def discover_chats(self) -> list[dict[str, str]]:
                return [{"id": "1", "title": "ops"}]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app, "TelegramClient", _EmptyTelegram):
                code, _out, _err = _run_main(["--discover-chat"], _live_env(root / "state.db"))
                self.assertEqual(code, 1)
            with patch.object(app, "TelegramClient", _FullTelegram):
                code, _out, _err = _run_main(["--discover-chat"], _live_env(root / "state.db"))
                self.assertEqual(code, 0)

    def test_telegram_success_without_json(self) -> None:
        class _OkTelegram:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def get_me(self) -> dict[str, str]:
                return {"username": "fixture_bot"}

            def send_html(self, _payload: str) -> str:
                return "303"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app, "TelegramClient", _OkTelegram):
                code, _out, err = _run_main(["--test-telegram"], _live_env(root / "state.db"))
        self.assertEqual(code, 0)
        self.assertIn("fixture_bot", err)

    def test_telegram_failure_json_and_plain(self) -> None:
        class _BrokenTelegram:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def get_me(self) -> dict[str, str]:
                raise RuntimeError("telegram down")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app, "TelegramClient", _BrokenTelegram):
                code, out, _err = _run_main(["--test-telegram", "--json"], _live_env(root / "state.db"))
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["outcome"], "telegram_test_failed")
            with patch.object(app, "TelegramClient", _BrokenTelegram):
                code, _out, _err = _run_main(["--test-telegram"], _live_env(root / "state.db"))
                self.assertEqual(code, 1)

    def test_invalid_frozen_input_json_and_plain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / "bad.json"
            bad.write_text("not-json", encoding="utf-8")
            code, out, _err = _run_main(
                ["--dry-run", "--frozen-input", str(bad), "--json"], _live_env(root / "state.db")
            )
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)["outcome"], "invalid_input")
            code, _out, err = _run_main(["--dry-run", "--frozen-input", str(bad)], _live_env(root / "state.db"))
            self.assertEqual(code, 2)
            self.assertTrue(err.strip())

    def test_status_unreadable_when_inspection_itself_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.db"
            with patch.object(app, "inspect_state", side_effect=OSError("inspection io failure")):
                report = app._state_status(missing)
        self.assertEqual(report["state"], "unreadable")
        self.assertEqual(report["detail"], "state inspection failed")

    def test_status_compatible_but_unopenable_is_not_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path):
                pass
            with patch.object(app, "_history_reader", return_value=None):
                report = app._state_status(path)
        self.assertEqual(report["state"], "unreadable")
        self.assertEqual(report["schema_version"], app.CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
