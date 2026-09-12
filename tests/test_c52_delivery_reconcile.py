"""C3.x delivery reconciliation (F03 recovery): audited resume after destination revert."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import patch

from meco_news import app
from meco_news.app import main
from meco_news.config import load_config
from meco_news.maintenance import MaintenanceContext
from meco_news.models import NewsItem
from meco_news.storage import InvalidTransition, StateError, StateStore

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config/watchlist.json")
ENV = {"TELEGRAM_BOT_TOKEN": "synthetic-review-token", "TELEGRAM_CHAT_ID": "12345"}


def _prepare(path: Path) -> int:
    with patch.dict(os.environ, ENV):
        snapshot = app._target_snapshot(CONFIG)
    with StateStore(path) as store:
        store.acquire_lease("delivery", "setup", 180)
        delivery = store.create_delivery(app._delivery_date(CONFIG), owner_id="setup")
        item = NewsItem(
            title="Reconcile probe",
            url="https://example.com/reconcile",
            source="Fixture",
            published_at=datetime.now(UTC),
        )
        store.prepare_delivery(
            delivery.delivery_id,
            [item],
            ["<b>Original frozen payload</b>"],
            owner_id="setup",
            target_snapshot=snapshot,
        )
        store.release_lease("delivery", "setup")
    return delivery.delivery_id


def _mismatch(delivery_id: int, path: Path) -> None:
    env = {**ENV, "STATE_DB": str(path), "TELEGRAM_CHAT_ID": "99999"}
    with (
        patch.dict(os.environ, env),
        patch.object(app, "TelegramClient") as client,
        patch.object(app, "collect_all", side_effect=AssertionError("unexpected collection")),
    ):
        client.return_value.send_html.return_value = "10"
        result = app.run_once(CONFIG)
    assert result.outcome == "needs_attention", result.outcome
    assert client.return_value.send_html.call_count == 0


def _reconcile(path: Path, delivery_id: int, current: str, reason: str = "r", operator: str = "o"):
    # One maintenance lifetime per attempt: a rolled-back attempt must not
    # poison the fence epoch cached by its context.
    with (
        MaintenanceContext.acquire(path, owner="reviewer") as context,
        StateStore(path, maintenance_context=context) as store,
    ):
        return store.reconcile_delivery(
                delivery_id, current_snapshot=current, reason=reason, operator=operator,
                maintenance_context=context,
            )


class ReconcileStorageTests(unittest.TestCase):
    def test_requires_maintenance_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            delivery_id = _prepare(path)
            _mismatch(delivery_id, path)
            with StateStore(path) as ordinary, self.assertRaises(StateError):
                ordinary.reconcile_delivery(
                        delivery_id, current_snapshot="x", reason="r", operator="o"
                    )

    def test_missing_and_non_mismatch_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            delivery_id = _prepare(path)
            with self.assertRaises(InvalidTransition):
                _reconcile(path, 9999, "x")
            with self.assertRaises(InvalidTransition):
                _reconcile(path, delivery_id, "x")

    def test_changed_destination_and_empty_audit_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            delivery_id = _prepare(path)
            _mismatch(delivery_id, path)
            with self.assertRaises(InvalidTransition):
                _reconcile(path, delivery_id, "still-changed")
            with self.assertRaises(StateError):
                _reconcile(path, delivery_id, "x", reason="")

    def test_partly_sent_delivery_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            delivery_id = _prepare(path)
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                chunk = store.due_chunks(delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="r", owner_id="owner")
                store.release_lease("delivery", "owner")
            _mismatch(delivery_id, path)
            with StateStore(path, readonly=True) as store:
                frozen = store.delivery(delivery_id).target_snapshot
            with self.assertRaises(InvalidTransition):
                _reconcile(path, delivery_id, frozen)

    def test_unsent_mismatch_resumes_with_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            delivery_id = _prepare(path)
            _mismatch(delivery_id, path)
            with StateStore(path, readonly=True) as store:
                frozen = store.delivery(delivery_id).target_snapshot
            info = _reconcile(path, delivery_id, frozen, reason="destination reverted", operator="op")
            self.assertEqual(info.state, "prepared")
            with StateStore(path, readonly=True) as store:
                trail = store.connection.execute(
                    "SELECT from_state,to_state,actor_type,actor_id,reason FROM state_transitions "
                    "WHERE entity_type='delivery' AND entity_id=? ORDER BY rowid DESC LIMIT 1",
                    (delivery_id,),
                ).fetchone()
                self.assertEqual(tuple(trail), ("needs_attention", "prepared", "operator", "op", "destination reverted"))


class ReconcileCommandTests(unittest.TestCase):
    def _cli(self, argv: list[str], path: Path, extra_env: dict | None = None) -> tuple[int, str]:
        env = {**ENV, "STATE_DB": str(path), **(extra_env or {})}
        buffer = io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_reverted_destination_recovers_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            delivery_id = _prepare(path)
            _mismatch(delivery_id, path)
            code, stdout = self._cli(
                ["--reconcile-delivery", str(delivery_id), "--reason", "chat reverted",
                 "--operator", "op", "--json"], path,
            )
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["delivery_id"], delivery_id)
            self.assertEqual(payload["state"], "prepared")
            with (
                patch.dict(os.environ, {**ENV, "STATE_DB": str(path)}),
                patch.object(app, "TelegramClient") as client,
                patch.object(app, "collect_all", side_effect=AssertionError("unexpected collection")),
            ):
                client.return_value.send_html.return_value = "11"
                result = app.run_once(CONFIG)
            self.assertEqual(result.outcome, "completed")
            self.assertEqual(client.return_value.send_html.call_count, 1)
            self.assertEqual(
                client.return_value.send_html.call_args[0][0], "<b>Original frozen payload</b>"
            )

    def test_reconcile_refuses_unknown_and_changed_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            delivery_id = _prepare(path)
            code, stdout = self._cli(
                ["--reconcile-delivery", "999999", "--reason", "r", "--operator", "o", "--json"], path,
            )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout).get("outcome"), "reconciliation_failed")
            _mismatch(delivery_id, path)
            code, stdout = self._cli(
                ["--reconcile-delivery", str(delivery_id), "--reason", "r", "--operator", "o", "--json"],
                path, {"TELEGRAM_CHAT_ID": "99999"},
            )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout).get("outcome"), "reconciliation_failed")

    def test_reconcile_grammar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            env = {**ENV, "STATE_DB": str(path)}
            with patch.dict(os.environ, env):
                with self.assertRaises(SystemExit) as cm:
                    main(["--reconcile-delivery", "3"])
                self.assertEqual(cm.exception.code, 2)
                with self.assertRaises(SystemExit) as cm:
                    main(["--reconcile-delivery", "0", "--reason", "r", "--operator", "o"])
                self.assertEqual(cm.exception.code, 2)
                with self.assertRaises(SystemExit) as cm:
                    main(["--reconcile-delivery", "3", "--reason", "r", "--operator", "o",
                          "--resolution", "sent"])
                self.assertEqual(cm.exception.code, 2)
                with self.assertRaises(SystemExit) as cm:
                    main(["--resolve-chunk", "1", "--resolution", "sent", "--reason", "r",
                          "--operator", "o", "--reconcile-delivery", "2"])
                self.assertEqual(cm.exception.code, 2)
                with self.assertRaises(SystemExit) as cm:
                    main(["--reason", "orphan"])
                self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()