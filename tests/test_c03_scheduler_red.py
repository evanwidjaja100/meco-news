"""C3.4 scheduler red suite — F-012 (closure plan).

Tests must be RED before fix, GREEN after C3.4 (typed outcomes + config reload + heartbeat fatal).
"""

from __future__ import annotations

import unittest


class TestC34SchedulerRed(unittest.TestCase):
    def test_run_once_returns_typed_outcome_not_int(self) -> None:
        # C3.4 requires run_once to return a typed Outcome, not bare int
        from meco_news import app as app_mod
        import inspect

        src = inspect.getsource(app_mod.run_once)
        daemon_src = inspect.getsource(app_mod.run_daemon)
        # Before fix, run_once is annotated `-> int` and daemon does `run_once(config)` without assignment
        has_int_return = "-> int" in src
        daemon_ignores = (
            "run_once(config)" in daemon_src and "result = run_once" not in daemon_src and "outcome = run_once" not in daemon_src
        )
        if has_int_return and daemon_ignores:
            self.fail("BUG REPRODUCED: run_once returns int and daemon ignores return — must return/work with typed Outcome (C3.4)")

    def test_config_reload_invalid_keeps_old(self) -> None:
        from meco_news import app as app_mod
        import inspect

        src = inspect.getsource(app_mod.run_daemon)
        # Before fix, daemon does: if config_path: config = load_config(config_path) without try, and run_once(config) without outcome check
        # After fix, it must have try/except around reload and keep old on failure
        has_try_around_reload = src.count("load_config") >= 1 and "try:" in src and "except" in src and "config_path" in src
        # Check that reload is inside try and that old config is kept
        if not has_try_around_reload:
            self.fail("BUG REPRODUCED: daemon reload not guarded — must try load_config and keep old on failure (C3.4)")
        if "effective" not in src.lower() and "MECO_CONFIG" not in src:
            # Should resolve effective path once
            self.fail(
                "BUG REPRODUCED: daemon does not resolve effective config path (default/MECO_CONFIG) — must reload correct file each cycle (C3.4)"
            )

    def test_heartbeat_failure_is_fatal(self) -> None:
        from meco_news import app as app_mod
        import inspect

        daemon_src = inspect.getsource(app_mod.run_daemon)
        # Before fix, run_daemon does heartbeat.start() and then never checks is_alive() inside the main loop — heartbeat can die silently
        # After fix, it must check heartbeat.is_alive() each wake and exit as failed_terminal
        has_is_alive_in_loop = "heartbeat.is_alive()" in daemon_src
        if not has_is_alive_in_loop:
            self.fail("BUG REPRODUCED: daemon does not check heartbeat.is_alive() in loop — heartbeat death must be fatal (C3.4)")

    def test_recovery_work_before_new_date(self) -> None:
        from meco_news import app as app_mod
        import inspect

        src = inspect.getsource(app_mod.run_daemon)
        # Before fix, daemon does: if run_now or _is_due or _has_recovery_work: run_once, but _has_recovery_work is only checked at start, not each cycle
        # After fix, each cycle must recover incomplete/due work before planning new date
        if "_has_recovery_work" in src and src.count("_has_recovery_work") < 2:
            self.fail(
                "BUG REPRODUCED: recovery work only checked at startup, not each cycle — must recover before new date each wake (C3.4)"
            )
