"""Focused behavioral corpus for the remaining release-gate decision arcs."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, UTC
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from io import StringIO
from unittest.mock import MagicMock, patch

import meco_news.alerts as alerts
import meco_news.app as app
import meco_news.backup as backup
import meco_news.config as config_module
from meco_news.collectors import CollectionResult, SourceResult
import meco_news.inspection as inspection
import meco_news.migrate as migrate
import meco_news.metrics as metrics
import meco_news.network as network
import meco_news.operations as operations
import meco_news.preflight as preflight
import meco_news.ranking as ranking
import meco_news.telegram as telegram
import meco_news.urls as urls
from meco_news.config import CollectionLimits, NetworkPolicy, load_config
from meco_news.models import NewsItem
from meco_news.storage import StateError, StateStore


ROOT = Path(__file__).resolve().parents[1]


def _raw_config() -> dict[str, object]:
    return json.loads((ROOT / "config" / "watchlist.json").read_text(encoding="utf-8"))


def _item(title: str = "LPG terminal expansion project") -> NewsItem:
    return NewsItem(
        title=title,
        url="https://example.com/story",
        source="Example Publisher",
        published_at=datetime(2026, 9, 6, tzinfo=UTC),
        summary="A new storage terminal project is under construction.",
        topic="lpg_energy",
        topic_label="LPG storage",
        relevance_reason="Potential tank demand.",
        score=20,
        source_host="example.com",
    )


def _collection(*items: NewsItem, failed: bool = False, with_issue: bool = False) -> CollectionResult:
    results = [
        SourceResult("good", "Good source", "succeeded", items=list(items), accepted_count=len(items)),
    ]
    if failed or with_issue:
        results.append(SourceResult("bad", "Bad source", "failed", reason_code="timeout", error_class="TimeoutError"))
    return CollectionResult(list(items), results, datetime.now(UTC), 1)


def _live_env(path: Path) -> dict[str, str]:
    return {
        "STATE_DB": str(path),
        "TELEGRAM_BOT_TOKEN": "synthetic-branch-closure-token",
        "TELEGRAM_CHAT_ID": "42",
    }


def _seed_delivery(path: Path, *, state: str = "prepared", due: bool = False) -> int:
    with StateStore(path) as store:
        store.acquire_lease("delivery", "fixture", 180)
        delivery = store.create_delivery("2026-09-07", owner_id="fixture", state="collecting")
        if state == "collecting":
            store.release_lease("delivery", "fixture")
            return delivery.delivery_id
        if state == "collection_retry":
            when = datetime.now(UTC) - timedelta(hours=1) if due else datetime.now(UTC) + timedelta(hours=1)
            retry = store.ensure_collection_retry(
                "2026-09-07", run_id="fixture-run", config_hash="fixture", next_attempt_at=when, error="outage", owner_id="fixture"
            )
            store.release_lease("delivery", "fixture")
            return retry.delivery_id
        store.prepare_delivery(delivery.delivery_id, [], ["<b>fixture</b>"], owner_id="fixture")
        store.release_lease("delivery", "fixture")
        return delivery.delivery_id


class ConfigAndPreflightBranchTests(unittest.TestCase):
    def test_config_parser_rejects_limits_sources_queries_and_duplicates(self) -> None:
        with self.assertRaises(config_module.ConfigurationError):
            config_module._list_of_strings(["x"] * 501, "values")
        limits = CollectionLimits(max_sources=1)
        feeds = [
            {"id": "one", "name": "One", "url": "https://one.example/feed"},
            {"id": "two", "name": "Two", "url": "https://two.example/feed"},
        ]
        with self.assertRaises(config_module.ConfigurationError):
            config_module._parse_feeds(feeds, limits)
        with self.assertRaises(config_module.ConfigurationError):
            config_module._parse_feeds([feeds[0], {**feeds[0]}], CollectionLimits(max_sources=3))
        with self.assertRaises(config_module.ConfigurationError):
            config_module._parse_queries(None, "google_news", CollectionLimits())
        queries = [{"id": "q", "name": "Q", "query": "tank"}, {"id": "q", "name": "Q2", "query": "vessel"}]
        with self.assertRaises(config_module.ConfigurationError):
            config_module._parse_queries(queries, "google_news", CollectionLimits())

        raw = _raw_config()
        raw["limits"] = {**raw["limits"], "max_sources": 5}  # type: ignore[index]
        raw["google_news"] = {**raw["google_news"], "enabled": True}  # type: ignore[index]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(config_module.ConfigurationError):
                load_config(path)

    def test_config_scalar_and_environment_boundaries(self) -> None:
        for value in (None, 1, ""):
            with self.assertRaises(config_module.ConfigurationError):
                config_module._string(value, "value")
        with self.assertRaises(config_module.ConfigurationError):
            config_module._integer(True, "number", 0, 10)
        with self.assertRaises(config_module.ConfigurationError):
            config_module._boolean("true", "flag")
        with self.assertRaises(config_module.ConfigurationError):
            config_module._list_of_strings("not-list", "values")
        with self.assertRaises(config_module.ConfigurationError):
            config_module._validate_hhmm("25:99")
        self.assertEqual(config_module._slug("  New Source!! "), "new-source")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("\n# comment\nMECO_TEST='quoted'\nNO_EQUALS\nOTHER=value=with=equals\n", encoding="utf-8")
            with patch.dict(config_module.os.environ, {}, clear=False):
                config_module.os.environ.pop("MECO_TEST", None)
                config_module.load_dotenv(path)
                self.assertEqual(config_module.os.environ["MECO_TEST"], "quoted")
                self.assertEqual(config_module.os.environ["OTHER"], "value=with=equals")

    def test_preflight_secret_and_disk_edges(self) -> None:
        self.assertTrue(preflight._disk_sufficient(preflight.MIN_FREE_BYTES, preflight.MIN_FREE_BYTES * 10))
        self.assertFalse(preflight._disk_sufficient(preflight.MIN_FREE_BYTES - 1, preflight.MIN_FREE_BYTES))
        self.assertTrue(preflight.looks_placeholder(""))
        self.assertTrue(preflight.looks_placeholder("your_token_here"))
        self.assertFalse(preflight.looks_placeholder("123:real"))
        with patch.dict(preflight.os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "123"}, clear=False):
            self.assertEqual(preflight._secret_status(), (False, "telegram_token_missing_or_placeholder"))
        with patch.dict(preflight.os.environ, {"TELEGRAM_BOT_TOKEN": "123:real", "TELEGRAM_CHAT_ID": ""}, clear=False):
            self.assertEqual(preflight._secret_status(), (False, "telegram_chat_id_missing_or_placeholder"))
        with patch.dict(preflight.os.environ, {"TELEGRAM_BOT_TOKEN": "123:real", "TELEGRAM_CHAT_ID": "123"}, clear=False):
            self.assertEqual(preflight._secret_status(), (True, "ok"))
        with patch.object(preflight.shutil, "disk_usage", side_effect=OSError("no disk")):
            self.assertFalse(preflight._disk_sufficient(0, 0))

    def test_preflight_online_precedence_and_health_truth_table_edges(self) -> None:
        config = load_config(ROOT / "config" / "watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.db"
            with patch.dict(preflight.os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
                code, report = preflight.run_preflight(config, online=True, state_path=missing)
            self.assertEqual(report["checks"]["telegram"]["reason"], "secrets_not_ready")
            self.assertNotEqual(code, preflight.PREFLIGHT_OK)

            ready_path = root / "ready.db"
            from meco_news.storage import StateStore

            with StateStore(ready_path):
                pass
            with patch.dict(preflight.os.environ, {"TELEGRAM_BOT_TOKEN": "123:real", "TELEGRAM_CHAT_ID": "42"}, clear=False), patch.object(
                preflight.TelegramClient, "get_me", return_value={"username": "bot"}
            ), patch.object(preflight.BoundedHTTPClient, "fetch", side_effect=OSError("source down")):
                code, report = preflight.run_preflight(config, online=True, state_path=ready_path)
            self.assertEqual(code, preflight.PREFLIGHT_ONLINE)
            self.assertFalse(any(check["ok"] for check in report["checks"]["sources"]))

            unreadable = root / "unreadable.db"
            unreadable.write_bytes(b"not sqlite")
            healthy, health_report = preflight.healthcheck(config, state_path=unreadable)
            self.assertFalse(healthy)
            self.assertIn("state_unreadable", health_report["reasons"])

            class FakeStore:
                def __init__(self, status: dict[str, object], exhausted: bool = False) -> None:
                    self.status = status
                    self.exhausted = exhausted

                def __enter__(self):
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def status_snapshot(self):
                    return self.status

                def retry_budget_exhausted(self, *_args: object, **_kwargs: object) -> bool:
                    return self.exhausted

            base_status: dict[str, object] = {
                "schema_version": preflight.CURRENT_SCHEMA_VERSION - 1,
                "integrity": "ok",
                "lease": None,
                "scheduler_lease": None,
                "active_delivery": None,
                "latest_delivery": None,
                "unresolved_ambiguity_count": 0,
                "last_success_at": None,
                "active_chunk": None,
            }
            fake_path = root / "fake.db"
            fake_path.write_bytes(b"placeholder")
            with patch.object(preflight, "StateStore", return_value=FakeStore(base_status)):
                healthy, report = preflight.healthcheck(config, state_path=fake_path)
            self.assertFalse(healthy)
            self.assertIn("incompatible_schema", report["reasons"])

            exhausted_status = {
                **base_status,
                "schema_version": preflight.CURRENT_SCHEMA_VERSION,
                "active_delivery": {"state": "retry_wait", "terminal_error": "all_sources_failed", "delivery_id": 1},
                "latest_delivery": {},
            }
            with patch.object(preflight, "StateStore", return_value=FakeStore(exhausted_status, exhausted=True)):
                healthy, report = preflight.healthcheck(config, state_path=fake_path)
            self.assertFalse(healthy)
            self.assertIn("all_sources_failed_retry_exhausted", report["reasons"])

            completed_empty_status = {
                **base_status,
                "schema_version": preflight.CURRENT_SCHEMA_VERSION,
                "active_delivery": {"state": "completed_empty"},
            }
            with patch.object(preflight, "StateStore", return_value=FakeStore(completed_empty_status)):
                healthy, report = preflight.healthcheck(config, state_path=fake_path)
            self.assertTrue(healthy)
            self.assertNotIn("unresolved_delivery_failure", report["reasons"])


class BoundaryArcTests(unittest.TestCase):
    def test_model_url_and_inspection_scalar_boundaries(self) -> None:
        item = NewsItem(
            title=object(),  # type: ignore[arg-type]
            url="https://example.com/story",
            source="Example",
            matches=["\ud800"],
        )
        self.assertEqual(item.title, "")
        self.assertTrue(item.is_quarantined)
        self.assertEqual(urls.canonical_url("://"), ":")
        self.assertEqual(urls.canonical_url("https://[::1]/story"), "https://[::1]/story")
        self.assertEqual(urls.canonical_url("https://example.com:8443/story"), "https://example.com:8443/story")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with StateStore(path) as store:
                store.connection.execute("ALTER TABLE deliveries DROP COLUMN force_reason")
                store.connection.commit()
            result = inspection.inspect_state(path)
            self.assertEqual(result.classification, "malformed")
            self.assertIn("force_reason", result.detail)

    def test_preflight_reports_retry_budget_still_available(self) -> None:
        config = load_config(ROOT / "config" / "watchlist.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            status = {
                "schema_version": preflight.CURRENT_SCHEMA_VERSION,
                "integrity": "ok",
                "lease": None,
                "scheduler_lease": None,
                "active_delivery": {
                    "state": "retry_wait",
                    "terminal_error": "all_sources_failed",
                    "delivery_id": 1,
                },
                "latest_delivery": {},
                "unresolved_ambiguity_count": 0,
                "last_success_at": None,
                "active_chunk": None,
            }

            class FakeStore:
                def __enter__(self):
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def status_snapshot(self):
                    return status

                def retry_budget_exhausted(self, *_args: object, **_kwargs: object) -> bool:
                    return False

            path.write_bytes(b"fixture")
            with patch.object(preflight, "StateStore", return_value=FakeStore()):
                healthy, report = preflight.healthcheck(config, state_path=path)
            self.assertFalse(healthy)
            self.assertFalse(report["retry"]["collection_exhausted"])
            self.assertNotIn("all_sources_failed_retry_exhausted", report["reasons"])


class MigrationBoundaryArcTests(unittest.TestCase):
    def test_current_schema_completeness_and_manifest_directory_sync_edges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current.db"
            with StateStore(current):
                pass
            self.assertTrue(migrate._is_current_and_complete(current))

            missing_target = root / "missing-target.db"
            with StateStore(missing_target) as store:
                store.connection.execute("ALTER TABLE deliveries DROP COLUMN target_snapshot")
                store.connection.commit()
            self.assertFalse(migrate._is_current_and_complete(missing_target))

            missing_retry = root / "missing-retry.db"
            with StateStore(missing_retry) as store:
                store.connection.execute("ALTER TABLE deliveries DROP COLUMN force_reason")
                store.connection.commit()
            self.assertFalse(migrate._is_current_and_complete(missing_retry))

            missing_chunk_retry = root / "missing-chunk-retry.db"
            with StateStore(missing_chunk_retry) as store:
                store.connection.execute("ALTER TABLE outbox_chunks DROP COLUMN retry_deadline_at")
                store.connection.commit()
            self.assertFalse(migrate._is_current_and_complete(missing_chunk_retry))

            with patch.object(migrate.os, "name", "posix"):
                migrate._fsync_directory(root)


class BackupBoundaryArcTests(unittest.TestCase):
    def test_backup_helper_failure_and_reconciliation_edges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(backup.os, "name", "posix"):
                backup._fsync_directory(root)
            with patch.object(backup, "_reserve_file", return_value=False), self.assertRaises(FileExistsError):
                backup._new_backup_path(root / "reserved.db")
            with self.assertRaises(ValueError):
                backup._reject_duplicate_json_keys([("key", 1), ("key", 2)])
            invalid_manifest = root / "invalid-manifest.json"
            invalid_manifest.write_text("[]", encoding="utf-8")
            with self.assertRaises(StateError):
                backup._load_manifest(invalid_manifest, root / "missing.db")

            empty = root / "empty.db"
            empty.touch()
            with patch.object(backup.sqlite3, "connect", side_effect=backup.sqlite3.DatabaseError("cannot open")):
                self.assertEqual(backup._target_recovery_state(empty), ([], []))

            no_history = root / "no-history.db"
            connection = backup.sqlite3.connect(no_history)
            connection.close()
            restored = root / "restored.db"
            self.assertEqual(backup._merge_post_backup_history(no_history, restored), 0)

            history = root / "history.db"
            with StateStore(history) as store:
                store.acquire_lease("delivery", "fixture", 180)
                delivery = store.create_delivery("2026-09-06", owner_id="fixture")
                store.prepare_delivery(
                    delivery.delivery_id,
                    [_item("History item")],
                    ["<b>history</b>"],
                    owner_id="fixture",
                )
                chunk = store.due_chunks(delivery.delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="fixture-run", owner_id="fixture")
                store.finish_chunk(
                    chunk.chunk_id,
                    "accepted",
                    run_id="fixture-run",
                    owner_id="fixture",
                    telegram_message_id="1",
                )
                store.release_lease("delivery", "fixture")
            self.assertEqual(backup._merge_post_backup_history(history, history), 0)

            source = root / "source.db"
            with StateStore(source):
                pass
            artifact = backup.create_backup(source, root / "backups")
            target = root / "target.db"
            with StateStore(target) as store:
                store.acquire_lease("delivery", "fixture", 180)
                store.create_delivery("2026-09-07", owner_id="fixture", state="collecting")
                store.release_lease("delivery", "fixture")
            with self.assertRaises(StateError):
                backup.restore_backup(artifact.database, target)


class MetricsAlertsOperationsBranchTests(unittest.TestCase):
    def test_metrics_scalar_seconds_and_disk_failures_are_bounded(self) -> None:
        class BadConnection:
            def execute(self, *_args: object):
                return self

            def fetchone(self):
                return ("not-a-number",)

        bad = BadConnection()
        self.assertEqual(metrics._scalar(bad, "SELECT 1"), 0)
        self.assertEqual(metrics._seconds(bad, "SELECT 1"), 0.0)

        class EmptyConnection:
            def execute(self, *_args: object):
                return self

            def fetchone(self):
                return None

        empty = EmptyConnection()
        self.assertEqual(metrics._scalar(empty, "SELECT 1"), 0)
        self.assertEqual(metrics._seconds(empty, "SELECT 1"), 0.0)
        with patch.object(metrics.shutil, "disk_usage", side_effect=OSError("unavailable")):
            self.assertEqual(metrics._disk_snapshot(Path("missing"))["available"], False)

    def test_alert_sink_invalid_input_scopes_and_fallbacks(self) -> None:
        sink = alerts.MemoryAlertSink()
        record = alerts.AlertRecord(
            alert_id="meco.health.x",
            severity="critical",
            threshold="x",
            first_seen_at="2026-09-06T00:00:00+00:00",
            state="firing",
            dedup_key="health:x",
            timestamp="2026-09-06T00:00:00+00:00",
            receipt_id="r1",
            details={"token": "secret"},
        )
        sink.publish(record)
        self.assertEqual(sink.reconcile_scoped(set(), scope="other:"), [])
        self.assertEqual(sink.reconcile_scoped(set(), only_keys={"other:key"}), [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.jsonl"
            path.write_text("not-json\n{}\n{\"dedup_key\": 1}\n", encoding="utf-8")
            self.assertEqual(alerts.JsonlAlertSink(path)._last, {})
            jsonl = alerts.JsonlAlertSink(path)
            jsonl.publish(record)
            self.assertEqual(jsonl.reconcile_scoped(set(), scope="other:"), [])
        self.assertEqual(alerts._stable_id("!!!", "health"), "meco.health.unknown")
        self.assertEqual(alerts._safe_details(["secret"]), {"detail": ["secret"]})

        class BasicSink:
            def __init__(self) -> None:
                self.published: list[alerts.AlertRecord] = []

            def publish(self, value: alerts.AlertRecord) -> alerts.AlertRecord:
                self.published.append(value)
                return value

            def reconcile(self, _active: set[str], *, now: datetime | None = None) -> list[alerts.AlertRecord]:
                return []

        basic = BasicSink()
        self.assertEqual(alerts.evaluate_health({}, basic), [])
        self.assertEqual(alerts.evaluate_backup(success=True, sink=basic), [])
        self.assertTrue(alerts.evaluate_health({"reasons": ["new_reason"]}, basic))

    def test_operations_nonregular_paths_and_pending_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(operations.os, "name", "nt"):
                operations._atomic_json(root / "atomic.json", {"ok": True})
            backups = root / "backups"
            backups.mkdir()
            target = backups / "linked.db"
            target.write_bytes(b"not-a-backup")
            manifest = target.with_suffix(".db.manifest.json")
            manifest.write_text("{}", encoding="utf-8")
            symlink = backups / "symlink.db"
            symlink_manifest = symlink.with_suffix(".db.manifest.json")
            try:
                symlink.symlink_to(target)
                symlink_manifest.symlink_to(manifest)
            except (OSError, NotImplementedError):
                pass
            else:
                valid, invalid = operations.inventory_backups(backups)
                self.assertEqual(valid, [])
                self.assertIn(symlink.resolve(), invalid)
            fake_link = root / "fake-link.lock"
            fake_link.write_text("{}", encoding="utf-8")
            with patch.object(operations.os, "open", side_effect=FileExistsError("exists")), patch.object(
                Path, "is_symlink", return_value=True
            ), self.assertRaises(operations.BackupBusy):
                operations.BackupJobLock(fake_link).__enter__()
            permission_lock = root / "permission.lock"
            permission_lock.write_text("{}", encoding="utf-8")
            with patch.object(operations.os, "open", side_effect=PermissionError("denied")), self.assertRaises(PermissionError):
                operations.BackupJobLock(permission_lock).__enter__()
            operations.BackupJobLock(root / "unused.lock").__exit__(None, None, None)
            with patch.object(operations.os, "name", "posix"):
                operations._atomic_json(root / "posix-atomic.json", {"ok": True})
            candidate = backups / "candidate.db"
            with patch.object(
                operations.Path,
                "resolve",
                side_effect=[root, root, root / "outside" / "candidate"],
            ), self.assertRaises(StateError):
                operations.apply_retention_plan(
                    operations.RetentionPlan((), (candidate,), (), None, ()), directory=backups
                )
        with self.assertRaises(ValueError):
            operations.write_replication_receipt(
                MagicMock(database=Path("x"), manifest=Path("m"), sha256=""),
                "receipt.json",
                destination="",
            )


class URLNetworkTelegramBranchTests(unittest.TestCase):
    def test_url_canonical_and_resolution_fail_closed(self) -> None:
        self.assertEqual(urls.canonical_url("https://example.com:bad/path"), "https://example.com/path")
        self.assertEqual(urls.sanitize_url_for_log("not a url"), "<invalid-url>")
        with patch.object(urls.socket, "getaddrinfo", return_value=[]), self.assertRaises(urls.URLPolicyError) as error:
            urls.validate_resolved_addresses("example.com", 443)
        self.assertEqual(error.exception.reason_code, "dns_no_addresses")
        with patch.object(urls.socket, "getaddrinfo", side_effect=socket.gaierror("no dns")), self.assertRaises(urls.URLPolicyError) as error:
            urls.validate_resolved_addresses("example.com", 443)
        self.assertEqual(error.exception.reason_code, "dns_resolution_failed")
        current = urls.validate_url("https://example.com/a")
        allowed = urls.validate_url("https://other.example/a")
        self.assertFalse(urls.same_or_allowed_redirect(current, allowed))
        self.assertTrue(urls.same_or_allowed_redirect(current, allowed, allowed_hosts={"other.example"}))
        self.assertFalse(urls.same_or_allowed_redirect(current, urls.validate_url("http://example.com/a", allow_http=True)))

    def test_network_error_classification_and_redirect_inputs(self) -> None:
        client = network.BoundedHTTPClient(CollectionLimits(source_deadline_seconds=2), NetworkPolicy())
        current = client._validated("https://example.com/a")
        with self.assertRaises(network.NetworkError):
            client._redirect_target(current, "")
        with self.assertRaises(network.NetworkError):
            client._redirect_target(current, "https://other.example/a")
        with patch.object(client, "_validate_origin", side_effect=urls.URLPolicyError("ssrf_address_class")), self.assertRaises(network.NetworkError):
            client.fetch("https://example.com/a")
        for error in (TimeoutError("late"), URLError("down"), OSError("broken")):
            with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(
                client, "_open_pinned", side_effect=error
            ), self.assertRaises(network.NetworkError):
                client.fetch("https://example.com/a")

        for status in (301, 429, 503, 404):
            response_error = HTTPError("https://example.com/a", status, "http", {}, None)
            with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(
                client, "_open_pinned", side_effect=response_error
            ), self.assertRaises(network.NetworkError):
                client.fetch("https://example.com/a")
        one_hop = network.BoundedHTTPClient(
            CollectionLimits(source_deadline_seconds=2, max_redirects=0), NetworkPolicy()
        )
        redirect = HTTPError("https://example.com/a", 301, "redirect", {"Location": "https://example.com/b"}, None)
        with patch.object(one_hop, "_validate_origin", return_value=["93.184.216.34"]), patch.object(
            one_hop, "_open_pinned", side_effect=redirect
        ), self.assertRaises(network.NetworkError):
            one_hop.fetch("https://example.com/a")

        class Response:
            status = 200
            headers = {"Content-Length": "bad"}

            def close(self) -> None:
                return None

            def getheader(self, _name: str, default: str = "") -> str:
                return default

        with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(
            client, "_open_pinned", return_value=Response()
        ), self.assertRaises(network.NetworkError):
            client.fetch("https://example.com/a")

        class GoodResponse:
            status = 200
            headers = {"Content-Length": "0"}

            def close(self) -> None:
                return None

            def getheader(self, _name: str, default: str = "") -> str:
                return default

            def read(self, _size: int) -> bytes:
                return b""

        with patch.object(client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(
            client, "_open_pinned", return_value=GoodResponse()
        ):
            self.assertEqual(client.fetch("https://example.com/a").payload, b"")

        redirect_client = network.BoundedHTTPClient(CollectionLimits(source_deadline_seconds=2, max_redirects=1), NetworkPolicy())

        class RedirectResponse:
            status = 301

            def close(self) -> None:
                return None

            def getheader(self, name: str, default: str = "") -> str:
                return "https://example.com/a" if name == "Location" else default

        with patch.object(redirect_client, "_validate_origin", return_value=["93.184.216.34"]), patch.object(
            redirect_client, "_open_pinned", return_value=RedirectResponse()
        ), patch.object(redirect_client, "_redirect_target", return_value=redirect_client._validated("https://example.com/a")), self.assertRaises(
            network.NetworkError
        ):
            redirect_client.fetch("https://example.com/a")
        with patch.object(network.time, "monotonic", side_effect=[0, 100]), self.assertRaises(network.NetworkError):
            redirect_client.fetch("https://example.com/a")
        with patch.object(network.time, "monotonic", side_effect=[0, 0, 100]), patch.object(
            redirect_client, "_validate_origin", return_value=["93.184.216.34"]
        ), patch.object(redirect_client, "_open_pinned", side_effect=OSError("late")), self.assertRaises(network.NetworkError):
            redirect_client.fetch("https://example.com/a")
        no_hops = network.BoundedHTTPClient(
            CollectionLimits(source_deadline_seconds=2, max_redirects=-1),
            NetworkPolicy(),
        )
        with self.assertRaises(network.NetworkError) as raised:
            no_hops.fetch("https://example.com/a")
        self.assertEqual(raised.exception.reason_code, "redirect_limit")

    def test_telegram_envelopes_and_html_edges(self) -> None:
        with self.assertRaises(ValueError):
            telegram.TelegramClient("replace_with_token")
        client = telegram.TelegramClient("123:valid", "42", timeout=0)

        class ContextResponse:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return self.body

        for body in (b"not-json", b"[]", b'{"ok": "yes"}', b'{"ok": false, "error_code": 0}', b'{"ok": false, "error_code": 500}'):
            with patch.object(client._opener, "open", return_value=ContextResponse(body)), self.assertRaises(telegram.TelegramSendError):
                client._call("test")
        error = HTTPError("https://example", 429, "rate", {}, None)
        error.read = lambda _limit=0: b'{"parameters":{"retry_after":3}}'  # type: ignore[method-assign]
        with patch.object(client._opener, "open", side_effect=error), self.assertRaises(telegram.TelegramSendError) as raised:
            client._call("test")
        self.assertEqual(raised.exception.outcome, "rejected_retryable")
        for error in (TimeoutError("timeout"), ConnectionResetError("reset"), URLError(TimeoutError("timeout")), OSError("os")):
            with patch.object(client._opener, "open", side_effect=error), self.assertRaises(telegram.TelegramSendError):
                client._call("test")
        updates = [{"message": {"chat": {"id": 42, "type": "private", "username": "u"}}}, None, {"channel_post": {}}]
        with patch.object(client, "_call", return_value={"result": updates}):
            self.assertEqual(client.discover_chats()[0]["id"], 42)
        with patch.object(client, "_call", return_value={"result": {}}), self.assertRaises(telegram.TelegramSendError):
            client.discover_chats()
        with patch.object(client, "_call", return_value={"result": "not-a-user"}), self.assertRaises(telegram.TelegramSendError):
            client.get_me()
        with self.assertRaises(ValueError):
            telegram.validate_message("<b>x")
        with self.assertRaises(ValueError):
            telegram.validate_message("<a href='javascript:x'>x</a>")
        with self.assertRaises(ValueError):
            telegram.validate_message("<a href='https://example.com/\x00'>x</a>")
        with self.assertRaises(ValueError):
            telegram.validate_message("<!-- x -->")
        self.assertEqual(telegram._clean_display(None, 10), "")
        self.assertFalse(telegram._fits_message("\ud800", 100, 100))

        oversized = _item("x")
        oversized.url = "https://example.com/" + ("u" * 2040)
        first = _item("ordinary")
        built = telegram.build_digest([first, oversized], "MECO", "UTC", max_length=3900, max_bytes=1200)
        self.assertTrue(built.omitted_items)
        self.assertTrue(built.messages)


class AppBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "config" / "watchlist.json")

    def test_frozen_empty_and_loader_boundary_arcs(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            app._print_dry_run([], 0, [], collection=None)
        self.assertIn("Collected 0 raw items", stderr.getvalue())
        base = {"title": "x", "url": "https://example.com/x", "source": "s"}
        with self.assertRaises(config_module.ConfigurationError):
            app._frozen_item({**base, "matches": ["x"] * 129}, 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frozen.json"
            payload = json.loads((ROOT / "tests" / "fixtures" / "frozen-empty-v1.json").read_text(encoding="utf-8"))
            payload["issues"] = "not-a-list"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(config_module.ConfigurationError):
                app._load_frozen_input(path)
            with patch.object(app, "StateStore", side_effect=OSError("missing")):
                self.assertIsNone(app._history_reader(path))
        fixture = ROOT / "tests" / "fixtures" / "frozen-empty-v1.json"
        with patch.dict(os.environ, {"STATE_DB": str(ROOT / "does-not-exist.db")}, clear=False):
            result = app.run_once(self.config, dry_run=True, frozen_input=fixture)
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(app._next_durable_retry(), None)
        self.assertFalse(app._has_recovery_work(self.config))

    def test_run_once_recovery_collection_issue_and_outer_error_branches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / "active.db"
            _seed_delivery(active, state="collecting")
            with patch.dict(os.environ, _live_env(active), clear=False):
                result = app.run_once(self.config, force=True, force_operator="operator", force_reason="replay")
            self.assertEqual(result.outcome, "needs_attention")

            retry = root / "retry.db"
            _seed_delivery(retry, state="collection_retry", due=True)
            with patch.dict(os.environ, _live_env(retry), clear=False), patch.object(
                app, "collect_all", return_value=_collection()
            ), patch.object(app.TelegramClient, "send_html", return_value="404"):
                result = app.run_once(self.config)
            self.assertEqual(result.outcome, "completed_empty")

            partial = root / "partial.db"
            _seed_delivery(partial, state="collecting")
            with patch.dict(os.environ, _live_env(partial), clear=False), patch.object(
                app, "collect_all", return_value=_collection(_item("LPG terminal expansion with partial source coverage"), with_issue=True)
            ), patch.object(app.TelegramClient, "send_html", return_value="405"):
                result = app.run_once(self.config)
            self.assertIn(result.outcome, {"completed", "completed_empty"})

            stopped = root / "stopped.db"
            _seed_delivery(stopped, state="collecting")
            stop = app.threading.Event()

            def collect_then_stop(_config: object) -> CollectionResult:
                stop.set()
                return _collection()

            with patch.dict(os.environ, _live_env(stopped), clear=False), patch.object(app, "collect_all", side_effect=collect_then_stop), patch.object(
                app.TelegramClient, "send_html", side_effect=AssertionError("stop must prevent send")
            ):
                result = app.run_once(self.config, stop_event=stop)
            self.assertEqual(result.outcome, "stopped")

            prepared = root / "constructor-failure.db"
            _seed_delivery(prepared)
            with patch.dict(os.environ, _live_env(prepared), clear=False), patch.object(
                app, "TelegramClient", side_effect=RuntimeError("client construction failed")
            ):
                result = app.run_once(self.config)
            self.assertEqual(result.outcome, "failed_terminal")
            prepared_stop = root / "constructor-failure-stop.db"
            _seed_delivery(prepared_stop)
            stop_after_error = app.threading.Event()
            stop_after_error.set()
            with patch.dict(os.environ, _live_env(prepared_stop), clear=False), patch.object(
                app, "TelegramClient", side_effect=RuntimeError("client construction failed")
            ):
                result = app.run_once(self.config, stop_event=stop_after_error)
            self.assertEqual(result.outcome, "stopped")

    def test_parser_validation_and_cli_mode_boundaries(self) -> None:
        parser = app.build_parser()

        def invalid(argv: list[str]) -> None:
            with self.assertRaises(SystemExit):
                app._validate_options(parser, parser.parse_args(argv))

        for argv in (
            ["--top-candidates", "-1"],
            ["--max-heartbeat-age", "0"],
            ["--resolution", "sent"],
            ["--resolve-chunk", "0", "--resolution", "sent", "--reason", "r", "--operator", "o"],
            ["--to-version", "1"],
            ["--migrate"],
            ["--to-version", "0", "--migrate"],
            ["--status", "--healthcheck"],
            ["--status", "--dry-run", "--frozen-input", "input.json"],
            ["--daemon", "--dry-run", "--frozen-input", "input.json"],
            ["--daemon", "--force", "--reason", "r", "--operator", "o"],
            ["--run-now"],
            ["--run-if-due", "--daemon"],
            ["--top-candidates", "1"],
            ["--ignore-history"],
            ["--frozen-input", "input.json"],
            ["--dry-run"],
            ["--force"],
            ["--force", "--reason", "r", "--operator", "o", "--resolution", "sent"],
            ["--online"],
            ["--json", "--daemon"],
            ["--alert-file", "alerts.jsonl"],
            ["--resolve-chunk", "1"],
        ):
            invalid(argv)
        valid = parser.parse_args(["--dry-run", "--frozen-input", "input.json", "--top-candidates", "1", "--ignore-history"])
        app._validate_options(parser, valid)

    def test_cli_success_failure_and_legacy_outcome_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.db"
            env = _live_env(state)
            with patch.dict(os.environ, {**env, "TELEGRAM_BOT_TOKEN": "replace_with_token"}, clear=False):
                self.assertEqual(app.main(["--daemon"]), 3)
                self.assertEqual(app.main(["--discover-chat", "--json"]), 3)
                self.assertEqual(app.main(["--test-telegram", "--json"]), 3)
            with patch.dict(os.environ, env, clear=False), patch.object(app, "restore_backup", return_value=root / "restored.db"):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(app.main(["--restore", "backup.db", "--json"]), 0)
                self.assertEqual(json.loads(stdout.getvalue())["outcome"], "restored")
            with patch.dict(os.environ, env, clear=False), patch.object(
                app, "run_guarded_migrations", side_effect=OSError("migration failure")
            ):
                self.assertEqual(app.main(["--migrate", "--to-version", str(app.CURRENT_SCHEMA_VERSION), "--json"]), 1)
            with patch.dict(os.environ, env, clear=False), patch.object(app, "_is_due", return_value=False), patch.object(
                app, "_has_recovery_work", return_value=False
            ):
                self.assertEqual(app.main(["--run-if-due"]), 0)
            with patch.dict(os.environ, env, clear=False), patch.object(app, "run_once", return_value=0):
                self.assertEqual(app.main([]), 0)


class RankingBranchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "config" / "watchlist.json")

    def test_rank_quality_merge_and_selection_limits(self) -> None:
        old = _item("Generic industrial tank")
        old.published_at = None
        old.source_host = ""
        old.summary = "short"
        old.topic = ""
        old.score = 30
        newer = _item("Generic industrial tank")
        newer.summary = "A much longer summary that enriches the selected publisher record."
        newer.source_host = "example.com"
        newer.published_at = datetime(2026, 9, 6, tzinfo=UTC)
        merged = ranking._merge_group([old, newer])
        self.assertEqual(merged.summary, newer.summary)
        self.assertEqual(merged.source_host, "example.com")
        self.assertEqual(merged.topic, "lpg_energy")
        trusted = _item("LPG terminal expansion project")
        trusted.source_host = "meco.co.id"
        ranking.rank_item(trusted, self.config, datetime(2026, 9, 6, tzinfo=UTC))
        for hours in (36, 72):
            dated = _item(f"LPG terminal expansion {hours}")
            dated.published_at = datetime(2026, 9, 6, tzinfo=UTC)
            ranking.rank_item(dated, self.config, datetime(2026, 9, 6, tzinfo=UTC) + timedelta(hours=hours))
        aged = _item("LPG terminal expansion 120")
        aged.published_at = datetime(2026, 9, 6, tzinfo=UTC)
        ranking.rank_item(aged, self.config, datetime(2026, 9, 11, tzinfo=UTC))
        many = [ranking.rank_item(_item(f"LPG terminal expansion project {index}"), self.config, datetime.now(UTC)) for index in range(4)]
        selected = ranking.select_digest(many, self.config, now=datetime.now(UTC))
        self.assertLessEqual(len(selected), self.config.daily_max)
        self.assertEqual(ranking.deduplicate_with_stats([], self.config)[1].input_count, 0)

        budget_config = {"limits": {"fuzzy_comparisons": 0}}
        first = _item("First unique title")
        second = _item("Second unique title")
        second.url = "https://example.com/second"
        clustered, stats = ranking.deduplicate_with_stats([first, second], budget_config)
        self.assertTrue(stats.budget_exhausted)
        self.assertGreaterEqual(len(clustered), 2)
        left = _item("First unique alpha")
        right = _item("Second unique beta")
        right.url = "https://example.com/right"
        ranking.deduplicate_with_stats([left, right], {"limits": {"fuzzy_comparisons": 100}})
        free_left = _item("Alpha headline")
        free_right = _item("Beta headline")
        free_right.url = "https://example.com/free-right"
        result, free_stats = ranking.deduplicate_with_stats([free_left, free_right], {"limits": {"fuzzy_comparisons": 100}})
        self.assertEqual(len(result), 2)
        self.assertFalse(free_stats.budget_exhausted)
        sent = ranking.select_digest([first], self.config, sent_fingerprints={first.fingerprint}, now=datetime.now(UTC))
        self.assertEqual(sent, [])
        one_max = self.config.as_dict()
        one_max.update({"daily_min": 1, "daily_max": 1})
        self.assertEqual(len(ranking.select_digest([_item()], one_max, now=datetime.now(UTC))), 1)
        one_min = self.config.as_dict()
        one_min.update({"daily_min": 1, "daily_max": 7, "minimum_score": 9, "fallback_score": 5})
        fallback = _item("fallback")
        fallback.score = 6
        self.assertEqual(len(ranking.select_digest([fallback], one_min, now=datetime.now(UTC))), 1)
        enough = _item("enough")
        enough.score = 20
        self.assertEqual(len(ranking.select_digest([enough], one_min, now=datetime.now(UTC))), 1)


if __name__ == "__main__":
    unittest.main()
