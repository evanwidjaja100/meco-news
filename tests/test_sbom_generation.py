"""SBOM generation from the hashed lock files."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _load(name: str, relative: str):  # type: ignore[no-untyped-def]
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(name, root / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sbom = _load("sbom_generator", "scripts/generate-sbom.py")
ROOT = Path(__file__).resolve().parents[1]


class SbomGenerationTests(unittest.TestCase):
    def test_locks_parse_to_pinned_components(self) -> None:
        entries = sbom.parse_lock(ROOT / "requirements-dev.lock", "optional")
        self.assertIn(("build", "1.2.2.post1"), entries)
        self.assertEqual(len(entries), 16)
        for (name, version), entry in entries.items():
            self.assertTrue(name and version)
            self.assertEqual(entry["scope"], "optional")
            self.assertTrue(entry["hashes"], (name, version))
            self.assertTrue(all(algorithm == "SHA-256" for algorithm, _ in entry["hashes"]))

    def test_build_lock_pins_merge_with_dev_scope(self) -> None:
        document = sbom.build_sbom(
            "meco-news",
            "0.0.0-test",
            [(ROOT / "requirements-build.lock", "excluded"), (ROOT / "requirements-dev.lock", "optional")],
            "2026-09-09T00:00:00Z",
        )
        self.assertEqual(document["bomFormat"], "CycloneDX")
        self.assertEqual(document["specVersion"], "1.5")
        self.assertEqual(len(document["components"]), 46)
        self.assertTrue(document["serialNumber"].startswith("urn:uuid:"))
        by_name = {component["name"]: component for component in document["components"]}
        self.assertEqual(by_name["setuptools"]["scope"], "optional")
        self.assertEqual(by_name["sigstore"]["scope"], "excluded")
        self.assertEqual(by_name["sigstore"]["version"], "4.5.0")
        self.assertEqual(by_name["typing-extensions"]["scope"], "optional")
        self.assertNotIn("typing_extensions", by_name)

    def test_generation_is_deterministic_for_fixed_timestamp(self) -> None:
        locks = [(ROOT / "requirements-build.lock", "excluded"), (ROOT / "requirements-dev.lock", "optional")]
        first = sbom.build_sbom("meco-news", "0.0.0-test", locks, "2026-09-09T00:00:00Z")
        second = sbom.build_sbom("meco-news", "0.0.0-test", locks, "2026-09-09T00:00:00Z")
        self.assertEqual(first, second)

    def test_main_defaults_to_current_utc_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sbom.json"
            code = sbom.main(
                [
                    "--root",
                    str(ROOT),
                    "--build-lock",
                    str(ROOT / "requirements-build.lock"),
                    "--dev-lock",
                    str(ROOT / "requirements-dev.lock"),
                    "--output",
                    str(target),
                ]
            )
            self.assertEqual(code, 0)
            document = json.loads(target.read_text(encoding="utf-8"))
            stamp = document["metadata"]["timestamp"]
            self.assertTrue(stamp.endswith("Z"), stamp)
            self.assertTrue(document["components"], "default run must record the locked components")

    def test_malformed_locks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare = root / "bare.txt"
            bare.write_text("not-a-requirement\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                sbom.parse_lock(bare, "optional")
            orphan = root / "orphan.txt"
            orphan.write_text("    --hash=sha256:abc123\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                sbom.parse_lock(orphan, "optional")
            comments = root / "comments.txt"
            comments.write_text("# nothing pinned here\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                sbom.parse_lock(comments, "optional")

    def test_main_generates_sbom_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "sbom.json"
            code = sbom.main(
                [
                    "--root",
                    str(ROOT),
                    "--build-lock",
                    str(ROOT / "requirements-build.lock"),
                    "--dev-lock",
                    str(ROOT / "requirements-dev.lock"),
                    "--output",
                    str(target),
                    "--timestamp",
                    "2026-09-09T00:00:00Z",
                ]
            )
            self.assertEqual(code, 0)
            document = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(document["bomFormat"], "CycloneDX")
            self.assertEqual(document["metadata"]["component"]["name"], "meco-news")
            missing = sbom.main(["--root", str(ROOT), "--dev-lock", str(root / "absent.lock"), "--output", str(target)])
            self.assertEqual(missing, 1)


if __name__ == "__main__":
    unittest.main()
