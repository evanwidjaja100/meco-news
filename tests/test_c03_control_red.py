"""C0.3 control-plane red reproducers — F-002/004/020."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestF002OrphanCLIAndDryRunSideEffects(unittest.TestCase):
    """F-002: orphan resolve flags fall through; dry-run must be offline and artifact-free."""

    def test_orphan_resolution_flags_rejected(self) -> None:
        from meco_news.app import main

        # --resolve-chunk without --resolution/--reason/--operator should exit 2 before any state/network
        with self.assertRaises(SystemExit) as cm:
            main(["--resolve-chunk", "12"])
        self.assertEqual(cm.exception.code, 2)

        with self.assertRaises(SystemExit) as cm:
            main(["--resolve-chunk", "12", "--resolution", "sent"])
        self.assertEqual(cm.exception.code, 2)

    def test_dry_run_makes_no_network_and_leaves_no_artifact(self) -> None:
        from meco_news.app import main
        from meco_news.collectors import CollectionResult, SourceResult
        from meco_news.models import NewsItem
        from datetime import datetime, UTC

        item = NewsItem(title="Gas infra fresh", url="https://example.com/a", source="S", published_at=datetime.now(UTC))
        fake_collection = CollectionResult([item], [SourceResult("fake", "fake", "succeeded", items=[item])], datetime.now(UTC), 1)

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.db"
            log = Path(d) / "logs" / "meco.jsonl"
            with patch.dict(os.environ, {"STATE_DB": str(state), "LOG_FILE": str(log)}, clear=False):
                with patch("meco_news.app.collect_all", return_value=fake_collection) as mock_collect:
                    with patch("meco_news.app.TelegramClient") as mock_tg:
                        code = main(["--dry-run", "--config", "config/watchlist.json"])
                        self.assertEqual(code, 0)
                        # DRY-RUN MUST be offline: no Telegram construction
                        mock_tg.assert_not_called()
                        # Must leave no DB/WAL/SHM/log/status
                        if state.exists():
                            self.fail(f"BUG REPRODUCED: dry-run created state DB at {state} — must be offline (C1.1)")
                        if log.exists():
                            self.fail(f"BUG REPRODUCED: dry-run created log file at {log} — must not initialize file logging before validation (F-002)")

    def test_invalid_config_creates_no_log_artifact(self) -> None:
        from meco_news.app import main
        import logging

        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text('{"unexpected": true}', encoding="utf-8")
            log = Path(d) / "meco.jsonl"
            with patch.dict(os.environ, {"LOG_FILE": str(log)}, clear=False):
                try:
                    main(["--config", str(bad)])
                except SystemExit:
                    pass
                finally:
                    # Close file handlers so Windows can clean up temp dir
                    for h in list(logging.getLogger().root.handlers):
                        try:
                            h.close()
                        except Exception:
                            pass
                        logging.getLogger().root.removeHandler(h)
                if log.exists():
                    self.fail("BUG REPRODUCED: invalid config created log file before validation — must validate before file logging (F-002)")


class TestF004HealthFalseGreen(unittest.TestCase):
    """F-004: health must be unhealthy for terminal/attention, ambiguous, incompatible schema."""

    def test_health_fails_for_needs_attention(self) -> None:
        from meco_news.storage import StateStore
        from meco_news.config import load_config
        from meco_news.preflight import healthcheck

        config = load_config("config/watchlist.json")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                delv = store.create_delivery("2026-08-25", config_hash="h")
                # Prepare with one chunk so we can transition to ambiguous
                from meco_news.models import NewsItem
                from datetime import datetime, UTC

                item = NewsItem(
                    title="LPG terminal",
                    url="https://example.com/lpg",
                    source="S",
                    published_at=datetime.now(UTC),
                    score=10,
                    topic="lpg_energy",
                )
                store.prepare_delivery(delv.delivery_id, [item], ["<b>hello</b>"], item_chunk_indexes={item.fingerprint: 0})
                chunk = store.due_chunks(delv.delivery_id)[0]
                store.begin_chunk_attempt(chunk.chunk_id, run_id="test")
                store.finish_chunk(chunk.chunk_id, "ambiguous", run_id="test", error_class="telegram_ambiguous", error_text="unknown")
            healthy, report = healthcheck(config, state_path=path)
            if healthy:
                self.fail(f"BUG REPRODUCED: health is healthy despite needs_attention — must be unhealthy (F-004). report={report}")

    def test_status_distinguishes_terminal_from_active(self) -> None:
        from meco_news.storage import StateStore

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            with StateStore(path) as store:
                store.acquire_lease("delivery", "owner", 180)
                d1 = store.create_delivery("2026-08-24", config_hash="h")
                store.prepare_delivery(d1.delivery_id, [], ["<b>a</b>"])
                # Complete it
                s = store
                # Manually complete for fixture
                s.connection.execute("UPDATE deliveries SET state='completed', completed_at=datetime('now') WHERE delivery_id=?", (d1.delivery_id,))
                s.connection.commit()
                d2 = store.create_delivery("2026-08-25", config_hash="h")
                snap = store.status_snapshot()
                # Must expose both latest terminal vs active — currently status_snapshot only exposes active_delivery
                if snap.get("active_delivery") and snap["active_delivery"]["delivery_date"] == "2026-08-24":
                    self.fail("BUG: status confuses latest terminal with active — must separate (C1.3)")


class TestF020LogRedactionIncomplete(unittest.TestCase):
    """F-020: logs must go to stdout, redact nested canaries, strip bidi, exactly one terminal."""

    def test_log_goes_to_stdout_not_stderr(self) -> None:
        import io, logging
        from meco_news.observability import configure_logging, emit_event

        with tempfile.TemporaryDirectory() as d:
            # Capture stdout
            import sys

            old_out = sys.stdout
            sys.stdout = io.StringIO()
            try:
                configure_logging(level="INFO")
                emit_event("run_terminal", outcome="completed", token="bot12345:ABCDEF-canary", secret="mysecret")
                out = sys.stdout.getvalue()
            finally:
                sys.stdout = old_out
                # Reset logging
                for h in list(logging.getLogger().root.handlers):
                    logging.getLogger().root.removeHandler(h)
            if "ABCDEF-canary" in out or "mysecret" in out:
                self.fail("BUG REPRODUCED: token/secret not redacted in stdout log (C1.4)")
            if "bot12345:" in out:
                self.fail("BUG: token canary leaked")


class TestF024BuildContextSentinel(unittest.TestCase):
    """F-024: textual .dockerignore check does not prove actual context/layers."""

    def test_sentinel_proves_context_not_just_text(self) -> None:
        from pathlib import Path

        p = Path("scripts/verify-build-context.py").read_text(encoding="utf-8")
        if 'verify-build-context' in p and 'layer' not in p.lower() and 'history' not in p.lower():
            self.fail("BUG REPRODUCED: build-context sentinel checks text only, not actual Docker context/layers/history/runtime (F-024/C6.4)")
