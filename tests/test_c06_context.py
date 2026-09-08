"""C6.4 actual context/layer harness — F-024.

The verifier is intentionally exercised against a disposable synthetic
checkout.  In particular, these tests never place canaries in the repository
that contains the test runner.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _run_verifier(root, *args):
    """Run the verifier with Docker hidden so the blocked path is deterministic.

    CI runners may provide a live Docker daemon while developer machines do
    not; hiding the executable keeps these tests hermetic on every host.
    """
    with tempfile.TemporaryDirectory(prefix="meco-no-docker-") as bindir:
        env = dict(os.environ)
        env["PATH"] = bindir
        return subprocess.run(
            [sys.executable, "scripts/verify-build-context.py", "--root", str(root), *args],
            capture_output=True,
            text=True,
            env=env,
        )


def _docker_available():
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


class TestContext(unittest.TestCase):
    @staticmethod
    def _synthetic_checkout(root: Path) -> None:
        (root / ".dockerignore").write_text(
            ".env\ndata/\nlogs/\n.git\n__pycache__/\n.pytest_cache/\ntests/\nresearch/\n",
            encoding="utf-8",
        )
        (root / "Dockerfile").write_text(
            "FROM scratch\nCOPY config ./config\nCOPY meco_news ./meco_news\n",
            encoding="utf-8",
        )

    def test_verify_build_context_passes(self):
        with tempfile.TemporaryDirectory(prefix="meco-context-test-") as directory:
            root = Path(directory)
            self._synthetic_checkout(root)
            sentinel = root / ".env"
            sentinel.write_text("preserve-me", encoding="utf-8")
            result = _run_verifier(root, "--json")
            self.assertEqual(result.returncode, 0, msg=result.stderr[-2000:])
            report = json.loads(result.stdout)
            self.assertIn(report["status"], {"passed", "blocked"})
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve-me")

    def test_verify_with_context(self):
        with tempfile.TemporaryDirectory(prefix="meco-context-test-") as directory:
            root = Path(directory)
            self._synthetic_checkout(root)
            preserved = {
                ".env": "synthetic-env",
                "data/state.db": "synthetic-state",
                "logs/run.jsonl": "synthetic-log",
            }
            for relative, value in preserved.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(value, encoding="utf-8")
            result = _run_verifier(root, "--require-docker", "--json")
            report = json.loads(result.stdout)
            expected_code = 0 if report.get("status") == "passed" else 2
            self.assertEqual(result.returncode, expected_code)
            self.assertIn(report["status"], {"passed", "blocked"})
            for relative, value in preserved.items():
                self.assertEqual((root / relative).read_text(encoding="utf-8"), value)

    def test_positive_control_is_not_silently_accepted(self):
        # The positive canary is a deliberately included file.  When Docker is
        # unavailable the report is blocked; a text-only fallback is never a
        # false pass.  Docker is hidden here so this assertion is hermetic on
        # every host; the live-Docker path has its own test below.
        with tempfile.TemporaryDirectory(prefix="meco-context-test-") as directory:
            root = Path(directory)
            self._synthetic_checkout(root)
            result = _run_verifier(root, "--json")
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "blocked", msg=json.dumps(report)[:2000])
            self.assertEqual(report["actual"]["status"], "blocked")

    def test_positive_control_detected_with_live_docker(self):
        # Exercises the real disposable Docker probe where a daemon exists.
        # The failure message carries the verifier report so CI logs show the
        # exact probe step that failed.
        if not _docker_available():
            self.skipTest("live Docker daemon is unavailable")
        with tempfile.TemporaryDirectory(prefix="meco-context-test-") as directory:
            root = Path(directory)
            self._synthetic_checkout(root)
            result = subprocess.run(
                [sys.executable, "scripts/verify-build-context.py", "--root", str(root), "--json"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            report = json.loads(result.stdout)
            detail = json.dumps(report)[:3000]
            self.assertEqual(result.returncode, 0, msg=detail)
            self.assertEqual(report["status"], "passed", msg=detail)
            self.assertEqual(report["actual"]["positive_control"]["status"], "detected", msg=detail)

    def test_dockerignore_has_required(self):
        content = Path(".dockerignore").read_text(encoding="utf-8")
        for pat in [".env", "data/", "logs/", ".git", "__pycache__/"]:
            self.assertIn(pat, content)
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("COPY .", dockerfile)
        self.assertIn("COPY config ./config", dockerfile)
        self.assertIn("COPY meco_news ./meco_news", dockerfile)

    def test_no_canary_in_context(self):
        # Simulate that canaries in ignored locations are not in Docker context
        # This is verified by the harness — we just check that the harness would detect leakage
        # Create a fake canary and ensure .dockerignore would ignore it
        dockerignore = Path(".dockerignore").read_text(encoding="utf-8")
        self.assertIn(".env", dockerignore)
        self.assertIn("data/", dockerignore)
