"""Group 8 (release-gate spoofs) regression tests.

Locks in the production-readiness fixes verified by
docs/reviews/2026-09-07-independent/reproduce.py:

- F09: scripts/coverage_gate.py accepted register entries for lines and
  arcs that never appear in the measured coverage, and an empty register
  passed. Entries must resolve to measured executable lines that
  originate measured arcs, claimed arcs must occur in the measured arc
  set, and an empty register fails.
- F10: scripts/release-provenance.py treated the self-asserted JSON
  string signature.state == "signed" as authentication. The signature
  gate now fails closed (this tool defines no attestation format and
  holds no trust root), requires actual artifacts, and verifies the
  build-context binding.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any


def _load(name: str, relative: str):  # type: ignore[no-untyped-def]
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(name, root / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load("group8_coverage_gate", "scripts/coverage_gate.py")
provenance = _load("group8_release_provenance", "scripts/release-provenance.py")
ROOT = Path(__file__).resolve().parents[1]


def _write(directory: Path, name: str, payload: dict[str, Any]) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _coverage(files: dict[str, Any]) -> dict[str, Any]:
    return {
        "totals": {"num_statements": 10, "covered_lines": 10, "num_branches": 4, "covered_branches": 4},
        "files": files,
    }


def _measured_app() -> dict[str, Any]:
    return {
        "executed_lines": [1, 7],
        "missing_lines": [],
        "executed_branches": [[1, 2], [1, 3]],
        "missing_branches": [],
    }


class CoverageGateSpoofTests(unittest.TestCase):
    def test_fictitious_line_and_arc_are_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage = _write(root, "coverage.json", _coverage({"meco_news/app.py": _measured_app()}))
            register = _write(
                root,
                "register.json",
                {"branches": [{"file": "meco_news/app.py", "line": 999999, "branch": [999999, 1000000]}]},
            )
            result = gate.evaluate(str(coverage), str(register))["critical_branches"]
        self.assertFalse(result["passed"])
        self.assertEqual(result["registered"], 1)
        self.assertEqual([entry["reason"] for entry in result["unknown"]], ["line_not_measured"])
        self.assertEqual(result["missing"], [])

    def test_empty_register_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage = _write(root, "coverage.json", _coverage({"meco_news/app.py": _measured_app()}))
            register = _write(root, "register.json", {"branches": []})
            result = gate.evaluate(str(coverage), str(register))["critical_branches"]
        self.assertFalse(result["passed"])
        self.assertTrue(result["empty_register"])

    def test_measured_nonbranch_line_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage = _write(root, "coverage.json", _coverage({"meco_news/app.py": _measured_app()}))
            register = _write(root, "register.json", {"branches": [{"file": "meco_news/app.py", "line": 7}]})
            result = gate.evaluate(str(coverage), str(register))["critical_branches"]
        self.assertFalse(result["passed"])
        self.assertEqual([entry["reason"] for entry in result["unknown"]], ["line_not_a_branch"])

    def test_unmeasured_claimed_arc_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage = _write(root, "coverage.json", _coverage({"meco_news/app.py": _measured_app()}))
            register = _write(
                root, "register.json", {"branches": [{"file": "meco_news/app.py", "line": 1, "branch": [1, 99]}]}
            )
            result = gate.evaluate(str(coverage), str(register))["critical_branches"]
        self.assertFalse(result["passed"])
        self.assertEqual([entry["reason"] for entry in result["unknown"]], ["branch_not_measured"])

    def test_missing_claimed_arc_is_missing(self) -> None:
        measured = _measured_app()
        measured["missing_branches"] = [[1, 3]]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage = _write(root, "coverage.json", _coverage({"meco_news/app.py": measured}))
            register = _write(
                root, "register.json", {"branches": [{"file": "meco_news/app.py", "line": 1, "branch": [1, 3]}]}
            )
            result = gate.evaluate(str(coverage), str(register))["critical_branches"]
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(result["unknown"], [])

    def test_malformed_branch_claim_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage = _write(root, "coverage.json", _coverage({"meco_news/app.py": _measured_app()}))
            register = _write(
                root, "register.json", {"branches": [{"file": "meco_news/app.py", "line": 1, "branch": [1]}]}
            )
            result = gate.evaluate(str(coverage), str(register))["critical_branches"]
        self.assertFalse(result["passed"])
        self.assertEqual([entry["reason"] for entry in result["unknown"]], ["branch_malformed"])

    def test_fully_covered_branch_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage = _write(root, "coverage.json", _coverage({"meco_news/app.py": _measured_app()}))
            register = _write(
                root,
                "register.json",
                {"branches": [{"file": "meco_news/app.py", "line": 1, "branch": [1, 2], "tests": ["example"]}]},
            )
            result = gate.evaluate(str(coverage), str(register))["critical_branches"]
        self.assertTrue(result["passed"])
        self.assertFalse(result["empty_register"])

    def test_shipped_register_names_measured_branch_statements(self) -> None:
        register = json.loads((ROOT / "scripts" / "critical-branches.json").read_text(encoding="utf-8"))
        self.assertGreater(len(register["branches"]), 0)
        for entry in register["branches"]:
            self.assertTrue(entry["tests"], entry)
            source = ROOT / entry["file"]
            self.assertTrue(source.is_file(), entry)
            lines = source.read_text(encoding="utf-8").splitlines()
            self.assertLess(entry["line"], len(lines), entry)
            statement = lines[entry["line"] - 1].lstrip()
            self.assertTrue(
                statement.startswith(("if ", "if(", "elif ", "except", "while ", "for ")),
                {"entry": entry, "statement": statement},
            )


class ProvenanceSignatureTests(unittest.TestCase):
    def test_self_asserted_signature_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write(
                Path(directory),
                "fake-provenance.json",
                {"schema_version": 1, "artifacts": [], "signature": {"state": "signed"}},
            )
            result = provenance.verify_provenance(path, require_signature=True)
        self.assertFalse(result["passed"])
        self.assertIn("artifacts:empty", result["failures"])
        self.assertIn("signature:unverifiable", result["failures"])

    def test_created_document_verifies_but_is_not_signed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            document = provenance.create_provenance(root, root / "provenance.json", [artifact])
            self.assertTrue(provenance.verify_provenance(document)["passed"])
            gated = provenance.verify_provenance(document, require_signature=True)
        self.assertFalse(gated["passed"])
        self.assertIn("signature:not_signed", gated["failures"])

    def test_tampered_artifact_fails_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            document = provenance.create_provenance(root, root / "provenance.json", [artifact])
            artifact.write_bytes(b"tampered-bytes")
            result = provenance.verify_provenance(document, require_signature=True)
        self.assertFalse(result["passed"])
        self.assertIn(str(artifact.resolve()), result["failures"])

    def test_changed_build_context_fails_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            context = root / "context.json"
            context.write_text("{}", encoding="utf-8")
            document = provenance.create_provenance(root, root / "provenance.json", [artifact], context)
            context.write_text('{"changed": true}', encoding="utf-8")
            result = provenance.verify_provenance(document, require_signature=True)
        self.assertFalse(result["passed"])
        self.assertIn("context:mismatch", result["failures"])

    def test_attached_sbom_verifies_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            sbom = root / "sbom.json"
            sbom.write_text("{}", encoding="utf-8")
            document = provenance.create_provenance(root, root / "provenance.json", [artifact], sbom_report=sbom)
            result = provenance.verify_provenance(document, require_sbom=True)
        self.assertTrue(result["passed"], result["failures"])
        self.assertEqual(result["failures"], [])

    def test_missing_sbom_fails_only_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            document = provenance.create_provenance(root, root / "provenance.json", [artifact])
            gated = provenance.verify_provenance(document, require_sbom=True)
            plain = provenance.verify_provenance(document)
        self.assertFalse(gated["passed"])
        self.assertIn("sbom:not_attached", gated["failures"])
        self.assertTrue(plain["passed"], plain["failures"])

    def test_tampered_sbom_fails_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            sbom = root / "sbom.json"
            sbom.write_text("{}", encoding="utf-8")
            document = provenance.create_provenance(root, root / "provenance.json", [artifact], sbom_report=sbom)
            sbom.write_text("{\"changed\": true}", encoding="utf-8")
            result = provenance.verify_provenance(document, require_sbom=True)
        self.assertFalse(result["passed"])
        self.assertIn("sbom:mismatch", result["failures"])

    def test_removed_sbom_file_is_reported_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            sbom = root / "sbom.json"
            sbom.write_text("{}", encoding="utf-8")
            document = provenance.create_provenance(root, root / "provenance.json", [artifact], sbom_report=sbom)
            sbom.unlink()
            result = provenance.verify_provenance(document, require_sbom=True)
        self.assertFalse(result["passed"])
        self.assertIn("sbom:missing", result["failures"])

    def test_non_list_artifacts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write(
                Path(directory),
                "provenance.json",
                {"schema_version": 1, "artifacts": {}, "signature": {"state": "not_signed"}},
            )
            with self.assertRaises(ValueError):
                provenance.verify_provenance(path, require_signature=True)


if __name__ == "__main__":
    unittest.main()
