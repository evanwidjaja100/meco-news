"""Phase 1 reviewer counterexample probes (owner self-review, 2026-09-11).

Reviewer-written, disposable state only, mocked transports, never .env.
Each probe attacks a Sep-07 finding fix from an angle the checked-in
regressions do not cover. A probe FAILURE is a reopened finding.
Run: .venv/Scripts/python.exe docs/evidence/production-readiness/local-2026-09-11-phase1/reviewer_probes.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from meco_news import app
from meco_news.backup import create_backup, restore_backup
from meco_news.collectors import parse_feed_result
from meco_news.config import load_config
from meco_news.models import NewsItem
from meco_news.storage import StateStore

CONFIG = load_config(ROOT / "config/watchlist.json")
ENV = {"TELEGRAM_BOT_TOKEN": "synthetic-review-token", "TELEGRAM_CHAT_ID": "12345"}
RESULTS: dict = {}


def _prepare(path: Path, title: str = "Reviewer probe item"):
    with patch.dict(os.environ, ENV):
        snapshot = app._target_snapshot(CONFIG)
    with StateStore(path) as store:
        store.acquire_lease("delivery", "setup", 180)
        delivery = store.create_delivery(app._delivery_date(CONFIG), owner_id="setup")
        item = NewsItem(title=title, url="https://example.com/reviewer-probe",
                        source="Reviewer", published_at=datetime.now(UTC))
        store.prepare_delivery(delivery.delivery_id, [item], ["<b>Frozen reviewer payload</b>"],
                               owner_id="setup", target_snapshot=snapshot)
        chunk = store.due_chunks(delivery.delivery_id)[0]
        store.release_lease("delivery", "setup")
    return delivery.delivery_id, chunk.chunk_id


def _run(path: Path, extra_env: dict | None = None):
    env = {**ENV, "STATE_DB": str(path), **(extra_env or {})}
    with (patch.dict(os.environ, env),
          patch.object(app, "TelegramClient") as client,
          patch.object(app, "collect_all", side_effect=AssertionError("unexpected collection"))):
        client.return_value.send_html.return_value = "77"
        outcome = app.run_once(CONFIG)
        return outcome, client.return_value.send_html


def rp1_restore_to_new_path_no_replay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        live = root / "live.db"
        delivery_id, chunk_id = _prepare(live)
        backup = create_backup(live, root / "backups")
        with StateStore(live) as store:
            store.acquire_lease("delivery", "sender", 180)
            store.begin_chunk_attempt(chunk_id, run_id="first", owner_id="sender")
            store.finish_chunk(chunk_id, "accepted", run_id="first", owner_id="sender",
                               telegram_message_id="10")
            store.release_lease("delivery", "sender")
        fresh = root / "restored.db"
        restore_backup(backup.database, fresh)
        outcome, sender = _run(fresh)
        assert sender.call_count == 0, f"RP1 FAIL: restored DB resent {sender.call_count}x"
        assert outcome.outcome == "needs_attention", f"RP1 FAIL: wrong outcome {outcome.outcome}"
        with StateStore(fresh, readonly=True) as store:
            chunk = store.connection.execute(
                "SELECT state,error_class FROM outbox_chunks WHERE delivery_id=?", (delivery_id,)).fetchall()
        assert all(r[0] == "ambiguous" for r in chunk), f"RP1 FAIL: chunks not held: {chunk}"
        RESULTS["RP1"] = {"sends": 0, "outcome": outcome.outcome,
                          "chunks": [[r[0], r[1]] for r in chunk]}
        print(f"RP1 PASS: restore-to-new-path sent 0x, outcome={outcome.outcome}, chunks held ambiguous", flush=True)


def rp2_ambiguous_backup_restore_no_autosend() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        live = root / "live.db"
        _prepare(live)
        with StateStore(live) as store:
            store.acquire_lease("delivery", "sender", 180)
            did = store.active_delivery(app._delivery_date(CONFIG)).delivery_id
            chunk = store.due_chunks(did)[0]
            store.begin_chunk_attempt(chunk.chunk_id, run_id="first", owner_id="sender")
            store.finish_chunk(chunk.chunk_id, "ambiguous", run_id="first", owner_id="sender",
                               error_text="unknown stage")
            store.release_lease("delivery", "sender")
        backup = create_backup(live, root / "backups")
        fresh = root / "restored.db"
        try:
            restore_backup(backup.database, fresh)
        except Exception as exc:
            RESULTS["RP2"] = {"refused": type(exc).__name__, "detail": str(exc)[:160]}
            print(f"RP2 PASS: ambiguous backup restore refused ({type(exc).__name__})", flush=True)
            return
        raise AssertionError("RP2 FAIL: ambiguous backup restore was not refused")


def rp3_mismatch_then_revert() -> None:
    from meco_news.app import main
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "state.db"
        _prepare(path)
        outcome, sender = _run(path, {"TELEGRAM_CHAT_ID": "99999"})
        assert outcome.outcome == "needs_attention", outcome.outcome
        assert sender.call_count == 0, "RP3 FAIL: mismatch run sent"
        with StateStore(path, readonly=True) as store:
            did = store.active_delivery(app._delivery_date(CONFIG)).delivery_id
        env = {**ENV, "STATE_DB": str(path)}
        with patch.dict(os.environ, env):
            code = main(["--reconcile-delivery", str(did), "--reason", "chat reverted",
                         "--operator", "reviewer", "--json"])
        assert code == 0, f"RP3 FAIL: reconcile rc={code}"
        outcome2, sender2 = _run(path)
        assert outcome2.outcome == "completed", outcome2.outcome
        assert sender2.call_count == 1, f"RP3 FAIL: sends={sender2.call_count}"
        assert sender2.call_args[0][0] == "<b>Frozen reviewer payload</b>", "RP3 FAIL: payload changed"
        RESULTS["RP3"] = {"mismatch_outcome": "needs_attention", "mismatch_sends": 0,
                          "reconcile_rc": code, "final_outcome": outcome2.outcome,
                          "final_sends": 1, "payload_preserved": True}
        print("RP3 PASS: mismatch blocked 0x; audited reconcile resumed original payload 1x",
              flush=True)


def rp4_fresh_hostile_combo() -> None:
    hostile_title = "St\u202eart C0 control end \u0001 lone \ud800 bidi \u2066x\u2069"
    hostile_link = "http://user:pass@127.0.0.1:8080/path?q=secret#frag"
    hostile_summary = "A" * 6000 + "&amp;entity-like-text;"
    good = "<item><title>Healthy sibling</title><link>https://example.com/good</link>" \
           "<description>All well</description></item>"
    bad = ("<item><title>" + hostile_title + "</title><link>" + hostile_link + "</link>" +
           "<description>" + hostile_summary + "</description></item>")
    payload = ("<?xml version=\"1.0\"?><rss><channel>" + bad + good + "</channel></rss>").encode(
        "utf-8", errors="surrogatepass")
    items, quarantine = parse_feed_result(payload, "Test", "rss", source_id="test")
    titles = [i.title for i in items]
    assert titles == ["Healthy sibling"], f"RP4 FAIL: titles={titles!r} quarantine={quarantine!r}"
    assert quarantine, "RP4 FAIL: hostile item not quarantined"
    RESULTS["RP4"] = {"healthy": titles, "quarantine": quarantine}
    print(f"RP4 PASS: healthy sibling intact, quarantine={quarantine}", flush=True)


def rp5_signature_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        dummy = root / "dummy.txt"
        dummy.write_text("reviewer dummy artifact", encoding="utf-8")
        prov = root / "prov.json"
        gen = subprocess.run(
            [sys.executable, "scripts/release-provenance.py", "--root", ".",
             "--output", str(prov), "--artifact", str(dummy)],
            capture_output=True, text=True, cwd=str(ROOT))
        assert gen.returncode == 0, f"RP5 FAIL: generate rc={gen.returncode}: {gen.stderr}"
        ver = subprocess.run(
            [sys.executable, "scripts/release-provenance.py",
             "--output", str(prov), "--verify", "--require-signature"],
            capture_output=True, text=True, cwd=str(ROOT))
        combined = (ver.stdout + ver.stderr).lower()
        assert ver.returncode != 0, "RP5 FAIL: unsigned provenance verified"
        assert "signature" in combined, f"RP5 FAIL: no signature status: {combined[:300]}"
        RESULTS["RP5"] = {"generate_rc": gen.returncode, "verify_rc": ver.returncode,
                          "verify_output": (ver.stdout + ver.stderr)[:600]}
        print(f"RP5 PASS: unsigned verify rc={ver.returncode}, signature reported", flush=True)


if __name__ == "__main__":
    rp1_restore_to_new_path_no_replay()
    rp2_ambiguous_backup_restore_no_autosend()
    rp3_mismatch_then_revert()
    rp4_fresh_hostile_combo()
    rp5_signature_fail_closed()
    out = Path(__file__).with_name("reviewer-probes.json")
    out.write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    print("ALL REVIEWER PROBES DONE", flush=True)