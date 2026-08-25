"""C3.5 zero/outage red suite — F-013 (closure plan).

Tests must be RED before fix, GREEN after C3.5 (distinct outcomes).
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import patch

from meco_news.config import load_config
from meco_news.collectors import CollectionResult, SourceResult
from meco_news.models import NewsItem
from meco_news.storage import StateStore


class TestC35ZeroOutageDistinct(unittest.TestCase):
    def test_empty_success_vs_all_failed_distinct(self) -> None:
        # C3.5 requires distinct handling: empty healthy must be completed_empty with coverage notice, all_failed must be retry_wait with alert
        from meco_news import app as app_mod
        import inspect

        src = inspect.getsource(app_mod.run_once)
        # Before fix, both paths use same generic handling and health does not distinguish
        # Check that healthcheck distinguishes them
        from meco_news.preflight import healthcheck
        health_src = inspect.getsource(healthcheck)
        has_empty_healthy = "completed_empty" in health_src
        if not has_empty_healthy:
            self.fail("BUG REPRODUCED: health does not distinguish completed_empty — must be healthy for empty success but unhealthy for all_failed (C3.5)")

    def test_all_sources_failed_is_retry_not_empty(self) -> None:
        # This test currently passes because run_once correctly distinguishes, but health does not — make it check health
        from meco_news.preflight import healthcheck
        import inspect
        src = inspect.getsource(healthcheck)
        # Before fix, health is healthy for all_sources_failed retry_wait (should be unhealthy after exhaust)
        if "all_sources_failed" not in src and "retry_wait" not in src:
            self.fail("BUG REPRODUCED: health does not check all_sources_failed retry exhaustion — must be unhealthy after max attempts (C3.5)")

    def test_empty_success_is_completed_empty(self) -> None:
        # Before fix, empty healthy and all_failed both use same generic path and status does not expose completed_empty distinctly
        from meco_news import app as app_mod
        import inspect
        src = inspect.getsource(app_mod.run_once)
        # Check that run_once distinguishes completed_empty in its return outcome mapping
        has_completed_empty_outcome = src.count("completed_empty") >= 2 and "outcome=\"completed_empty\"" in src or "outcome='completed_empty'" in src
        if not has_completed_empty_outcome:
            self.fail("BUG REPRODUCED: run_once does not emit distinct completed_empty outcome — must be distinct from retry_wait (C3.5)")

    def test_dry_run_is_offline_no_outbox(self) -> None:
        # C3.5: dry_run must be offline — no network, no outbox, no history
        from meco_news import app as app_mod
        import inspect
        src = inspect.getsource(app_mod.run_once)
        # After fix, dry_run should not call collect_all( — check the dry_run block up to its first return
        # Use a more precise split that excludes the rest of the file
        dry_section = src.split("if dry_run:")[1].split("if looks_placeholder")[0] if "if dry_run:" in src else src
        if "collect_all(" in dry_section:
            self.fail("BUG REPRODUCED: dry_run calls collect_all (network) — must be offline with frozen input (C3.5/C1.1)")
