"""C5.1 command terminal-event coverage.

Every operator/query command path owns one command attempt lifecycle and
emits exactly one attempt_terminal record. Machine mode keeps stdout as
exactly one JSON document while diagnostics and the terminal record go to
stderr.
"""

from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, UTC
from io import StringIO
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import meco_news.app as app
from meco_news.models import NewsItem
from meco_news.storage import StateStore


ROOT = Path(__file__).resolve().parents[1]


def _run_main(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    with patch.dict(os.environ, env, clear=False), redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            return app.main(argv), stdout.getvalue(), stderr.getvalue()
        finally:
            for handler in list(logging.getLogger().handlers):
                handler.close()
                logging.getLogger().removeHandler(handler)


def _terminals(stderr: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in stderr.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == "attempt_terminal":
            records.append(payload)
    return records


def _live_env(path: Path) -> dict[str, str]:
    return {
        "STATE_DB": str(path),
        "TELEGRAM_BOT_TOKEN": "synthetic-valid-token-for-c51-tests",
        "TELEGRAM_CHAT_ID": "123456789",
    }


def _seed_state(path: Path) -> None:
    with StateStore(path) as store:
        store.acquire_lease("delivery", "owner", 180)
        store.release_lease("delivery", "owner")


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
        matches=["lpg", "terminal"],
        source_id="fixture",
        source_host="example.com",
    )


def _seed_ambiguous(path: Path) -> int:
    config = app.load_config(ROOT / "config" / "watchlist.json")
    today = app._delivery_date(config)
    with StateStore(path) as store:
        store.acquire_lease("delivery", "owner", 180)
        delivery = store.create_delivery(
            today, config_hash="fixture-config", owner_id="owner", state="collecting", target_snapshot=""
        )
        store.prepare_delivery(
            delivery.delivery_id, [_item(1)], ["<b>Fixture</b>"], owner_id="owner", target_snapshot=""
        )
        chunks = store.due_chunks(delivery.delivery_id)
        store.begin_chunk_attempt(chunks[0].chunk_id, run_id="run", owner_id="owner")
        store.finish_chunk(chunks[0].chunk_id, "ambiguous", run_id="run", owner_id="owner", error_text="unknown")
        store.release_lease("delivery", "owner")
        return chunks[0].chunk_id


class CommandTerminalTests(unittest.TestCase):
    def test_config_show(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            code, stdout, stderr = _run_main(["--config-show", "--json"], _live_env(state))
            self.assertEqual(code, 0)
            json.loads(stdout)
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            record = terminals[0]
            self.assertEqual(record.get("kind"), "command")
            self.assertEqual(record.get("result"), "success")
            self.assertEqual(record.get("outcome"), "config_shown")
            run_id = str(record.get("run_id"))
            self.assertEqual(record.get("attempt_id"), f"{run_id}:command:config_show")

    def test_preflight_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            code, stdout, stderr = _run_main(["--preflight", "--json"], _live_env(state))
            self.assertEqual(code, 0)
            json.loads(stdout)
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("result"), "success")
            self.assertEqual(terminals[0].get("outcome"), "preflight_passed")

    def test_preflight_online_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            with patch.object(app, "run_preflight", return_value=(0, {"ready": True})):
                code, stdout, stderr = _run_main(["--preflight", "--online", "--json"], _live_env(state))
            self.assertEqual(code, 0)
            json.loads(stdout)
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            record = terminals[0]
            self.assertEqual(record.get("result"), "success")
            self.assertEqual(record.get("outcome"), "preflight_passed")
            self.assertTrue(str(record.get("attempt_id")).endswith(":command:preflight_online"))

    def test_status_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "missing.db"
            code, stdout, stderr = _run_main(["--status", "--json"], _live_env(state))
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload.get("state"), "missing")
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("result"), "success")
            self.assertEqual(terminals[0].get("outcome"), "status_reported")

    def test_metrics_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "missing.db"
            code, stdout, stderr = _run_main(["--metrics", "--json"], _live_env(state))
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload.get("state"), "missing")
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("result"), "success")
            self.assertEqual(terminals[0].get("outcome"), "metrics_exported")

    def test_healthcheck_missing_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "missing.db"
            code, stdout, stderr = _run_main(["--healthcheck", "--json"], _live_env(state))
            self.assertEqual(code, 1)
            json.loads(stdout)
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("result"), "terminal")
            self.assertEqual(terminals[0].get("outcome"), "unhealthy")

    def test_backup_and_restore_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            _seed_state(state)
            env = _live_env(state)
            code, stdout, stderr = _run_main(["--backup", str(root / "bk"), "--json"], env)
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertIn("sha256", payload)
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("outcome"), "backup_created")
            self.assertEqual(terminals[0].get("result"), "success")
            restored = root / "restored.db"
            code, stdout, stderr = _run_main(["--restore", str(payload["database"]), "--json"], _live_env(restored))
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload.get("outcome"), "restored")
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("outcome"), "restored")
            self.assertEqual(terminals[0].get("result"), "success")

    def test_backup_and_restore_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            _seed_state(state)
            env = _live_env(state)
            with patch.object(app, "create_backup", side_effect=OSError("backup failed")):
                code, stdout, stderr = _run_main(["--backup", str(root / "bk"), "--json"], env)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout).get("outcome"), "backup_failed")
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("outcome"), "backup_failed")
            self.assertEqual(terminals[0].get("result"), "terminal")
            self.assertEqual(terminals[0].get("error_class"), "OSError")
            with patch.object(app, "restore_backup", side_effect=OSError("restore failed")):
                code, stdout, stderr = _run_main(["--restore", str(root / "missing.db"), "--json"], env)
            self.assertEqual(code, 1)
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("outcome"), "restore_failed")
            self.assertEqual(terminals[0].get("result"), "terminal")

    def test_resolve_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            chunk_id = _seed_ambiguous(state)
            env = _live_env(state)
            code, stdout, stderr = _run_main(
                ["--resolve-chunk", str(chunk_id), "--resolution", "sent",
                 "--reason", "confirmed externally", "--operator", "op", "--json"],
                env,
            )
            self.assertEqual(code, 0)
            json.loads(stdout)
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("outcome"), "chunk_resolved")
            self.assertEqual(terminals[0].get("result"), "success")
            code, stdout, stderr = _run_main(
                ["--resolve-chunk", "999999", "--resolution", "sent",
                 "--reason", "confirmed externally", "--operator", "op", "--json"],
                env,
            )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout).get("outcome"), "resolution_failed")
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("outcome"), "resolution_failed")
            self.assertEqual(terminals[0].get("result"), "terminal")

    def test_migrate_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            _seed_state(state)
            env = _live_env(state)
            code, stdout, stderr = _run_main(["--migrate", "--to-version", "999", "--json"], env)
            self.assertEqual(code, 2)
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("outcome"), "unsupported_migration_target")
            self.assertEqual(terminals[0].get("result"), "terminal")
            with patch.object(app, "run_guarded_migrations", return_value=[1]):
                code, stdout, stderr = _run_main(
                    ["--migrate", "--to-version", str(app.CURRENT_SCHEMA_VERSION), "--json"], env
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout).get("outcome"), "migration_applied")
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("outcome"), "migration_applied")
            with patch.object(app, "run_guarded_migrations", side_effect=OSError("migration failed")):
                code, stdout, stderr = _run_main(
                    ["--migrate", "--to-version", str(app.CURRENT_SCHEMA_VERSION), "--json"], env
                )
            self.assertEqual(code, 1)
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("outcome"), "migration_failed")
            self.assertEqual(terminals[0].get("result"), "terminal")

    def test_telegram_placeholder_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            env = dict(_live_env(state))
            env["TELEGRAM_BOT_TOKEN"] = "replace_with_token"
            code, stdout, stderr = _run_main(["--test-telegram", "--json"], env)
            self.assertEqual(code, 3)
            terminals = _terminals(stderr)
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0].get("outcome"), "telegram_test_failed")
            self.assertEqual(terminals[0].get("result"), "terminal")
            self.assertEqual(terminals[0].get("error_class"), "TelegramSecretConfiguration")

    def test_telegram_success_paths(self) -> None:
        class FilledTelegram:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def discover_chats(self) -> list[dict[str, str]]:
                return [{"id": "1"}]

            def get_me(self) -> dict[str, str]:
                return {"username": "fixture_bot"}

            def send_html(self, _payload: str) -> str:
                return "303"

        class EmptyTelegram(FilledTelegram):
            def discover_chats(self) -> list[dict[str, str]]:
                return []

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            env = _live_env(state)
            with patch.object(app, "TelegramClient", FilledTelegram):
                code, stdout, stderr = _run_main(["--discover-chat", "--json"], env)
                self.assertEqual(code, 0)
                terminals = _terminals(stderr)
                self.assertEqual(len(terminals), 1)
                self.assertEqual(terminals[0].get("outcome"), "chats_discovered")
                code, stdout, stderr = _run_main(["--test-telegram", "--json"], env)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout).get("outcome"), "telegram_test_succeeded")
                terminals = _terminals(stderr)
                self.assertEqual(len(terminals), 1)
                self.assertEqual(terminals[0].get("outcome"), "telegram_test_succeeded")
            with patch.object(app, "TelegramClient", EmptyTelegram):
                code, stdout, stderr = _run_main(["--discover-chat", "--json"], env)
                self.assertEqual(code, 1)
                terminals = _terminals(stderr)
                self.assertEqual(len(terminals), 1)
                self.assertEqual(terminals[0].get("outcome"), "no_chats")
                self.assertEqual(terminals[0].get("result"), "terminal")
            with patch.object(app, "TelegramClient", side_effect=OSError("network down")):
                code, stdout, stderr = _run_main(["--test-telegram", "--json"], env)
                self.assertEqual(code, 1)
                terminals = _terminals(stderr)
                self.assertEqual(len(terminals), 1)
                self.assertEqual(terminals[0].get("outcome"), "telegram_test_failed")
                self.assertEqual(terminals[0].get("result"), "terminal")

    def test_machine_stdout_stays_pure_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "missing.db"
            code, stdout, stderr = _run_main(["--status", "--json"], _live_env(state))
            self.assertEqual(code, 0)
            self.assertEqual(len(_terminals(stderr)), 1)
            payload = json.loads(stdout)
            self.assertIn("state", payload)
            self.assertNotIn("attempt_terminal", stdout)


if __name__ == "__main__":
    unittest.main()
