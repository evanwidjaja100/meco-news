"""Independent review probes. Only disposable databases and mocked transports.

Run from repository root: .venv/Scripts/python.exe docs/reviews/2026-09-07-independent/reproduce.py
Outputs observations, not passing acceptance tests. Never loads .env.
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC
import importlib.util
import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from meco_news import app
from meco_news import collectors, operations, maintenance
from meco_news.collectors import CollectionResult, SourceResult
from meco_news.backup import create_backup, restore_backup
from meco_news.config import load_config
from meco_news.maintenance import MaintenanceContext
from meco_news.models import NewsItem
from meco_news.preflight import healthcheck
from meco_news.storage import StateStore

CONFIG = load_config(ROOT / "config/watchlist.json")
ENV = {"TELEGRAM_BOT_TOKEN": "synthetic-review-token", "TELEGRAM_CHAT_ID": "12345"}


def prepare(path, *, snapshot="", policy=None, empty=False):
    if not snapshot:
        with patch.dict(os.environ, ENV):
            snapshot = app._target_snapshot(CONFIG)
    with StateStore(path) as store:
        store.acquire_lease("delivery", "setup", 180)
        delivery = store.create_delivery(app._delivery_date(CONFIG), owner_id="setup", retry_policy=policy)
        item = NewsItem(title="Industrial project", url="https://example.com/industrial", source="Fixture", published_at=datetime.now(UTC))
        store.prepare_delivery(delivery.delivery_id, [] if empty else [item], ["<b>Original frozen digest</b>"], owner_id="setup", target_snapshot=snapshot)
        chunk = store.due_chunks(delivery.delivery_id)[0]
        store.release_lease("delivery", "setup")
    return delivery.delivery_id, chunk.chunk_id


def restore_replays_acknowledged(root):
    path = root / "restore.db"
    delivery, chunk = prepare(path)
    backup = create_backup(path, root / "backups")
    with StateStore(path) as store:
        store.acquire_lease("delivery", "sender", 180)
        store.begin_chunk_attempt(chunk, run_id="first", owner_id="sender")
        store.finish_chunk(chunk, "accepted", run_id="first", owner_id="sender", telegram_message_id="10")
        store.release_lease("delivery", "sender")
    restore_backup(backup.database, path)
    with StateStore(path, readonly=True) as store:
        before = {"restored_delivery": store.delivery(delivery).state, "history_rows": store.connection.execute("SELECT COUNT(*) FROM article_history").fetchone()[0]}
    with patch.dict(os.environ, {**ENV, "STATE_DB": str(path)}), patch.object(app, "TelegramClient") as client, patch.object(app, "collect_all", side_effect=AssertionError("unexpected collection")):
        client.return_value.send_html.return_value = "11"
        result = app.run_once(CONFIG)
        return {**before, "outcome": result.outcome, "duplicate_send_calls": client.return_value.send_html.call_count}


def expired_retry_still_sends(root):
    path = root / "retry.db"
    policy = {"enabled": True, "max_attempts": 4, "max_elapsed_seconds": 60}
    delivery, chunk = prepare(path, policy=policy)
    with StateStore(path) as store:
        store.acquire_lease("delivery", "sender", 180)
        store.begin_chunk_attempt(chunk, run_id="first", owner_id="sender")
        store.finish_chunk(chunk, "rejected_retryable", run_id="first", owner_id="sender", next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        # Simulate restart following two minutes of downtime.
        past = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        store.connection.execute("UPDATE delivery_attempts SET started_at=?", (past,))
        store.connection.commit()
        exhausted = store.retry_budget_exhausted(delivery, chunk_id=chunk, max_attempts=4, max_elapsed_seconds=60)
        store.release_lease("delivery", "sender")
    with patch.dict(os.environ, {**ENV, "STATE_DB": str(path)}), patch.object(app, "TelegramClient") as client:
        client.return_value.send_html.return_value = "12"
        result = app.run_once(CONFIG)
        return {"budget_exhausted_before_run": exhausted, "outcome": result.outcome, "send_calls": client.return_value.send_html.call_count}


def destination_revert_still_blocked(root):
    path = root / "destination.db"
    with patch.dict(os.environ, ENV):
        snapshot = app._target_snapshot(CONFIG)
    delivery, chunk = prepare(path, snapshot=snapshot)
    with patch.dict(os.environ, {**ENV, "STATE_DB": str(path), "TELEGRAM_CHAT_ID": "99999"}), patch.object(app, "TelegramClient") as client:
        changed = app.run_once(CONFIG).outcome
    with patch.dict(os.environ, {**ENV, "STATE_DB": str(path)}), patch.object(app, "TelegramClient") as client:
        reverted = app.run_once(CONFIG).outcome
        sends = client.return_value.send_html.call_count
    try:
        with MaintenanceContext.acquire(path, owner="review-operator") as context, StateStore(path, maintenance_context=context) as store:
            store.resolve_chunk(chunk, "retry", reason="restore original destination", operator="review", maintenance_context=context)
    except Exception as exc:
        resolution = f"{type(exc).__name__}: {exc}"
    else:
        resolution = "resolved"
    return {"changed": changed, "reverted": reverted, "send_calls": sends, "documented_resolution": resolution}


def abandoned_first_delivery_health(root):
    path = root / "health.db"
    with StateStore(path) as store:
        store.acquire_lease("delivery", "setup", 180)
        store.create_delivery("2026-01-01", owner_id="setup")
        store.connection.execute("UPDATE deliveries SET started_at='2026-01-01T00:00:00+00:00'")
        store.connection.commit()
        store.release_lease("delivery", "setup")
    # Hold disk facts constant; this tests delivery health, not this machine's free space.
    with patch("meco_news.preflight._disk_sufficient", return_value=True):
        healthy, report = healthcheck(CONFIG, state_path=path)
    return {"healthy": healthy, "reasons": report["reasons"], "state": report["status"]["active_delivery"]["state"]}


def lease_budget_validation(root):
    raw = CONFIG.as_dict()
    raw["lease_ttl_seconds"] = 65
    raw["limits"]["cycle_deadline_seconds"] = 120
    config_path = root / "config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    accepted = load_config(config_path)
    path = root / "lease.db"
    class Clock(datetime):
        current = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    def collection(_config):
        Clock.current += timedelta(seconds=70)
        return CollectionResult([], [SourceResult("fixture", "Fixture", "succeeded")], Clock.current, 70000)

    with patch.dict(os.environ, {**ENV, "STATE_DB": str(path)}), patch("meco_news.storage.datetime", Clock), patch.object(app, "collect_all", side_effect=collection), patch.object(app, "TelegramClient") as client:
        result = app.run_once(accepted)
        sends = client.return_value.send_html.call_count
    with StateStore(path, readonly=True) as store:
        state = store.active_delivery(None).state
    return {"accepted_lease_ttl": accepted.lease_ttl_seconds, "collection_cycle_budget": accepted.limits.cycle_deadline_seconds, "simulated_collection_seconds": 70, "outcome": result.outcome, "persisted_state": state, "send_calls": sends}


def fictional_critical_branch_passes(root):
    spec = importlib.util.spec_from_file_location("review_coverage_gate", ROOT / "scripts/coverage_gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    coverage = root / "coverage.json"
    coverage.write_text(json.dumps({"totals": {"num_statements": 1, "covered_lines": 1, "num_branches": 2, "covered_branches": 2}, "files": {"meco_news/app.py": {"executed_lines": [1], "missing_lines": [], "executed_branches": [[1, 2], [1, 3]], "missing_branches": []}}}), encoding="utf-8")
    register = root / "register.json"
    register.write_text(json.dumps({"branches": [{"file": "meco_news/app.py", "line": 999999, "branch": [999999, 1000000]}]}), encoding="utf-8")
    return gate.evaluate(coverage, register)["critical_branches"]


def fictitious_signature_passes(root):
    spec = importlib.util.spec_from_file_location("review_provenance", ROOT / "scripts/release-provenance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = root / "fake-provenance.json"
    path.write_text(json.dumps({"schema_version": 1, "artifacts": [], "signature": {"state": "signed"}}), encoding="utf-8")
    return module.verify_provenance(path, require_signature=True)


def concurrent_backup_lock_owners(root):
    path = root / "backup.lock"
    path.write_text(json.dumps({"pid": 0, "owner": "dead"}), encoding="utf-8")
    first = operations.BackupJobLock(path, owner="first")
    second = operations.BackupJobLock(path, owner="second")
    real_alive = operations._pid_alive

    def race_after_stale_read(*_args):
        # The second contender replaces the stale marker after the first
        # contender reads it, before the first unlinks it.
        with patch.object(operations, "_pid_alive", real_alive):
            second.__enter__()
        return False

    try:
        with patch.object(operations, "_pid_alive", side_effect=race_after_stale_read):
            first.__enter__()
        return {"first_held": first._held, "second_held": second._held, "marker_owner": json.loads(path.read_text(encoding="utf-8"))["owner"]}
    finally:
        first.__exit__(None, None, None)
        second.__exit__(None, None, None)


def _logging_child(connection, output_path):
    with open(output_path, "w", encoding="utf-8") as stderr, patch.object(sys, "stderr", stderr), patch.object(collectors, "_fetch", side_effect=RuntimeError("password=review-canary-92817")):
        collectors._source_process_entry(connection, collectors._collect_rss, ({"id": "fake", "name": "Fake", "url": "https://example.com"}, 1), {})


def spawned_worker_log_redaction(root):
    output_path = root / "worker-stderr.txt"
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_logging_child, args=(child, str(output_path)))
    process.start()
    child.close()
    try:
        if parent.poll(15):
            parent.recv_bytes()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
        text = output_path.read_text(encoding="utf-8")
        return {"process_exit": process.exitcode, "synthetic_password_visible_in_stderr": "password=review-canary-92817" in text, "stderr_has_traceback": "Traceback" in text}
    finally:
        parent.close()


def _restore_crash_child(backup, target):
    real_replace = os.replace

    def die_after_target_rename(source, destination):
        result = real_replace(source, destination)
        if Path(source).resolve() == Path(target).resolve():
            os._exit(73)
        return result

    with patch("meco_news.backup.os.replace", side_effect=die_after_target_rename):
        restore_backup(backup, target)


def restore_process_death_loses_authority(root):
    path = root / "crash-restore.db"
    delivery, chunk = prepare(path)
    with StateStore(path) as store:
        store.acquire_lease("delivery", "sender", 180)
        store.begin_chunk_attempt(chunk, run_id="first", owner_id="sender")
        store.finish_chunk(chunk, "accepted", run_id="first", owner_id="sender", telegram_message_id="20")
        store.release_lease("delivery", "sender")
    artifact = create_backup(path, root / "backups")
    context = mp.get_context("spawn")
    process = context.Process(target=_restore_crash_child, args=(str(artifact.database), str(path)))
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
    missing = not path.exists()
    # Fast-forward the maintenance marker's one-hour grace period. Only the
    # marker clock is patched; process liveness and DB opening are real.
    with patch.object(maintenance, "_utc_now", return_value=datetime.now(UTC) + timedelta(hours=2)), StateStore(path) as store:
        remaining_deliveries = store.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    return {"child_exit": process.exitcode, "target_missing_after_crash": missing, "rollback_file_retained": path.with_suffix(".db.pre-restore.bak").exists(), "deliveries_after_normal_reopen": remaining_deliveries}


def malformed_frozen_input_cli(root):
    frozen = root / "invalid.json"
    frozen.write_text('{"schema_version": 1}', encoding="utf-8")
    log = root / "invalid-input.log"
    process = subprocess.run([sys.executable, "-m", "meco_news", "--config", str(ROOT / "config/watchlist.json"), "--dry-run", "--frozen-input", str(frozen), "--json", "--log-file", str(log)], cwd=root, env={**os.environ, **ENV, "PYTHONPATH": str(ROOT), "STATE_DB": str(root / "unused.db"), "LOG_FILE": ""}, capture_output=True, text=True, timeout=20)
    return {"exit_code": process.returncode, "stdout": process.stdout, "traceback": "Traceback" in process.stderr, "log_created": log.exists()}


def corrupt_database_status_cli(root):
    path = root / "corrupt.db"
    path.write_bytes(b"not a sqlite database")
    process = subprocess.run([sys.executable, "-m", "meco_news", "--config", str(ROOT / "config/watchlist.json"), "--status", "--json"], cwd=root, env={**os.environ, **ENV, "PYTHONPATH": str(ROOT), "STATE_DB": str(path), "LOG_FILE": ""}, capture_output=True, text=True, timeout=20)
    return {"exit_code": process.returncode, "reported_state": json.loads(process.stdout).get("state"), "actual_file_exists": path.exists()}


if __name__ == "__main__":
    observations = {}
    for probe in (restore_replays_acknowledged, expired_retry_still_sends, destination_revert_still_blocked, abandoned_first_delivery_health, lease_budget_validation, fictional_critical_branch_passes, fictitious_signature_passes, concurrent_backup_lock_owners, spawned_worker_log_redaction, restore_process_death_loses_authority, malformed_frozen_input_cli, corrupt_database_status_cli):
        with tempfile.TemporaryDirectory(prefix="meco-independent-review-") as directory:
            try:
                observations[probe.__name__] = probe(Path(directory))
            except Exception as exc:
                observations[probe.__name__] = {"probe_error": f"{type(exc).__name__}: {exc}"}
    output = json.dumps(observations, indent=2, sort_keys=True)
    Path(__file__).with_name("reproductions.json").write_text(output + "\n", encoding="utf-8")
    print(output)
