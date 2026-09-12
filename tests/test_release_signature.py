"""Sigstore bundle binding regression tests (Phase 2, CG6).

The unit suite runs without network access and without the third-party
signer installed, so every Rekor check below is stubbed by monkeypatching
the release-provenance module attribute ``_verify_bundle``. Only the CI
package job performs a real ``sigstore verify`` against the pinned GitHub
OIDC identity.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
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


provenance = _load("release_signature_provenance", "scripts/release-provenance.py")

EXPECTED_ISSUER = "https://token.actions.githubusercontent.com"
EXPECTED_IDENTITY = "https://github.com/evanwidjaja100/meco-news/.github/workflows/ci.yml@refs/heads/main"


def _stub_verifier(outcome: str):  # type: ignore[no-untyped-def]
    def _fake(bundle: Path, artifact: Path, expected_identity: str | None = None) -> str:
        assert bundle.is_file(), bundle
        assert artifact.is_file(), artifact
        return outcome

    return _fake


BRANCH_IDENTITY = (
    "https://github.com/evanwidjaja100/meco-news/.github/workflows/ci.yml"
    "@refs/heads/release/phase2-signing-sbom"
)


def _make_signed(root: Path, verifier_outcome: str = "verified") -> tuple[Path, Path, Path]:
    artifact = root / "candidate.whl"
    artifact.write_bytes(b"candidate-bytes")
    bundle = root / "candidate.whl.sigstore"
    bundle.write_bytes(b"bundle-bytes")
    document = provenance.create_provenance(
        root, root / "provenance.json", [artifact], signature_bundles={str(artifact): str(bundle)}
    )
    original = provenance._verify_bundle
    provenance._verify_bundle = _stub_verifier(verifier_outcome)  # type: ignore[method-assign]
    try:
        result = provenance.verify_provenance(document, require_signature=True)
    finally:
        provenance._verify_bundle = original  # type: ignore[method-assign]
    assert result["passed"], result["failures"]
    return document, artifact, bundle


class SignatureBindingTests(unittest.TestCase):
    def test_bound_bundle_passes_with_verified_stub(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_signed(root)

    def test_identity_policy_pins_option_a(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _, _ = _make_signed(root)
            raw = json.loads(document.read_text(encoding="utf-8"))
            policy = raw["signature"]["identity_policy"]
        self.assertEqual(policy["oidc_issuer"], EXPECTED_ISSUER)
        self.assertEqual(policy["identity"], EXPECTED_IDENTITY)

    def test_tampered_bundle_bytes_fail_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _, bundle = _make_signed(root)
            bundle.write_bytes(b"tampered-bundle")
            original = provenance._verify_bundle
            provenance._verify_bundle = _stub_verifier("verified")  # type: ignore[method-assign]
            try:
                result = provenance.verify_provenance(document, require_signature=True)
            finally:
                provenance._verify_bundle = original  # type: ignore[method-assign]
        self.assertFalse(result["passed"])
        self.assertIn("signature:bundle_mismatch", result["failures"])

    def test_deleted_bundle_file_fails_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _, bundle = _make_signed(root)
            bundle.unlink()
            original = provenance._verify_bundle
            provenance._verify_bundle = _stub_verifier("verified")  # type: ignore[method-assign]
            try:
                result = provenance.verify_provenance(document, require_signature=True)
            finally:
                provenance._verify_bundle = original  # type: ignore[method-assign]
        self.assertFalse(result["passed"])
        self.assertIn("signature:bundle_missing", result["failures"])

    def test_sbom_covered_without_bundle_fails_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _, _ = _make_signed(root)
            sbom = root / "sbom.json"
            sbom.write_text("{}", encoding="utf-8")
            raw = json.loads(document.read_text(encoding="utf-8"))
            from hashlib import sha256

            raw["sbom"] = {"state": "attached", "path": str(sbom.resolve()), "sha256": sha256(b"{}").hexdigest()}
            document.write_text(json.dumps(raw), encoding="utf-8")
            original = provenance._verify_bundle
            provenance._verify_bundle = _stub_verifier("verified")  # type: ignore[method-assign]
            try:
                result = provenance.verify_provenance(document, require_signature=True, require_sbom=True)
            finally:
                provenance._verify_bundle = original  # type: ignore[method-assign]
        self.assertFalse(result["passed"])
        self.assertIn("signature:bundle_missing", result["failures"])

    def test_create_rejects_bundle_for_unknown_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            bundle = root / "other.sigstore"
            bundle.write_bytes(b"bundle-bytes")
            with self.assertRaises(ValueError):
                provenance.create_provenance(
                    root, root / "provenance.json", [artifact], signature_bundles={str(root / "nope.whl"): str(bundle)}
                )

    def test_verify_rejects_unknown_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _, _ = _make_signed(root)
            raw = json.loads(document.read_text(encoding="utf-8"))
            raw["signature"]["bundles"].append(
                {"artifact": str(root / "unknown.whl"), "path": str(root / "unknown.sigstore"), "sha256": "0" * 64}
            )
            document.write_text(json.dumps(raw), encoding="utf-8")
            original = provenance._verify_bundle
            provenance._verify_bundle = _stub_verifier("verified")  # type: ignore[method-assign]
            try:
                result = provenance.verify_provenance(document, require_signature=True)
            finally:
                provenance._verify_bundle = original  # type: ignore[method-assign]
        self.assertFalse(result["passed"])
        self.assertIn("signature:bundle_unknown", result["failures"])

    def test_malformed_bundle_entries_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _, _ = _make_signed(root)
            raw: dict[str, Any] = json.loads(document.read_text(encoding="utf-8"))
            raw["signature"]["bundles"] = [{"artifact": "x"}]
            document.write_text(json.dumps(raw), encoding="utf-8")
            original = provenance._verify_bundle
            provenance._verify_bundle = _stub_verifier("verified")  # type: ignore[method-assign]
            try:
                result = provenance.verify_provenance(document, require_signature=True)
            finally:
                provenance._verify_bundle = original  # type: ignore[method-assign]
        self.assertFalse(result["passed"])
        self.assertIn("signature:bundles_malformed", result["failures"])

    def test_self_asserted_signed_without_bundles_stays_unverifiable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.json"
            path.write_text(json.dumps({"schema_version": 1, "artifacts": [], "signature": {"state": "signed"}}), encoding="utf-8")
            result = provenance.verify_provenance(path, require_signature=True)
        self.assertFalse(result["passed"])
        self.assertIn("signature:unverifiable", result["failures"])

    def test_verifier_unavailable_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _, _ = _make_signed(root)
            original = provenance._verify_bundle
            provenance._verify_bundle = _stub_verifier("unavailable")  # type: ignore[method-assign]
            try:
                result = provenance.verify_provenance(document, require_signature=True)
            finally:
                provenance._verify_bundle = original  # type: ignore[method-assign]
        self.assertFalse(result["passed"])
        self.assertIn("signature:verifier_unavailable", result["failures"])

    def test_verifier_rejection_names_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, artifact, _ = _make_signed(root)
            original = provenance._verify_bundle
            provenance._verify_bundle = _stub_verifier("rejected")  # type: ignore[method-assign]
            try:
                result = provenance.verify_provenance(document, require_signature=True)
            finally:
                provenance._verify_bundle = original  # type: ignore[method-assign]
        self.assertFalse(result["passed"])
        self.assertIn(f"signature:rejected:{artifact.resolve()}", result["failures"])

    def test_cli_rejects_malformed_bundle_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            code = provenance.main(["--root", str(root), "--output", str(root / "p.json"), "--artifact", str(artifact), "--signature-bundle", "badformat"])
        self.assertEqual(code, 1)

    def test_cli_rejects_duplicate_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            bundle = root / "b.sigstore"
            bundle.write_bytes(b"bundle-bytes")
            mapping = f"{artifact}={bundle}"
            code = provenance.main(["--root", str(root), "--output", str(root / "p.json"), "--artifact", str(artifact), "--signature-bundle", mapping, "--signature-bundle", mapping])
        self.assertEqual(code, 1)

    def test_cli_rejects_bundle_with_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "p.json"
            output.write_text("{}", encoding="utf-8")
            code = provenance.main(["--output", str(output), "--verify", "--signature-bundle", "a=b"])
        self.assertEqual(code, 1)

    def test_create_rejects_duplicate_resolved_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            first = root / "first.sigstore"
            first.write_bytes(b"first-bundle")
            second = root / "second.sigstore"
            second.write_bytes(b"second-bundle")
            alias = root / "sub" / ".." / "candidate.whl"
            with self.assertRaises(ValueError):
                provenance.create_provenance(
                    root,
                    root / "provenance.json",
                    [artifact],
                    signature_bundles={str(artifact): str(first), str(alias): str(second)},
                )

    def test_verify_timeout_is_unavailable(self) -> None:
        original = provenance.subprocess.run
        def _timeout(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=args[0] if args else "", timeout=0)
        provenance.subprocess.run = _timeout  # type: ignore[method-assign]
        try:
            outcome = provenance._verify_bundle(Path("bundle"), Path("artifact"))
        finally:
            provenance.subprocess.run = original  # type: ignore[method-assign]
        self.assertEqual(outcome, "unavailable")

    def test_cli_create_roundtrip_verifies_with_stub(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            bundle = root / "candidate.whl.sigstore"
            bundle.write_bytes(b"bundle-bytes")
            output = root / "provenance.json"
            code = provenance.main(["--root", str(root), "--output", str(output), "--artifact", str(artifact), "--signature-bundle", f"{artifact}={bundle}"])
            self.assertEqual(code, 0)
            original = provenance._verify_bundle
            provenance._verify_bundle = _stub_verifier("verified")  # type: ignore[method-assign]
            try:
                result = provenance.verify_provenance(output, require_signature=True)
            finally:
                provenance._verify_bundle = original  # type: ignore[method-assign]
        self.assertTrue(result["passed"], result["failures"])


class WorkflowRefIdentityTests(unittest.TestCase):
    def test_create_records_branch_workflow_ref_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            bundle = root / "candidate.whl.sigstore"
            bundle.write_bytes(b"bundle-bytes")
            document = provenance.create_provenance(
                root,
                root / "provenance.json",
                [artifact],
                signature_bundles={str(artifact): str(bundle)},
                identity=BRANCH_IDENTITY,
            )
            raw = json.loads(document.read_text(encoding="utf-8"))
        self.assertEqual(raw["signature"]["identity_policy"]["identity"], BRANCH_IDENTITY)

    def test_create_rejects_foreign_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            with self.assertRaises(ValueError):
                provenance.create_provenance(
                    root,
                    root / "provenance.json",
                    [artifact],
                    identity="https://github.com/other/repo/.github/workflows/ci.yml@refs/heads/main",
                )

    def test_verify_passes_expected_identity_to_bundles(self) -> None:
        seen: list[str | None] = []

        def _recording(bundle: Path, artifact: Path, expected_identity: str | None = None) -> str:
            assert bundle.is_file(), bundle
            assert artifact.is_file(), artifact
            seen.append(expected_identity)
            return "verified"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _, _ = _make_signed(root)
            original = provenance._verify_bundle
            provenance._verify_bundle = _recording  # type: ignore[method-assign]
            try:
                result = provenance.verify_provenance(
                    document, require_signature=True, expected_identity=BRANCH_IDENTITY
                )
            finally:
                provenance._verify_bundle = original  # type: ignore[method-assign]
        self.assertTrue(result["passed"], result["failures"])
        self.assertEqual(seen, [BRANCH_IDENTITY])

    def test_verify_rejects_foreign_expected_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _, _ = _make_signed(root)
            with self.assertRaises(ValueError):
                provenance.verify_provenance(
                    document,
                    require_signature=True,
                    expected_identity="https://example.invalid/workflow@refs/heads/main",
                )

    def test_cli_identity_with_verify_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "p.json"
            output.write_text("{}", encoding="utf-8")
            code = provenance.main(["--output", str(output), "--verify", "--identity", BRANCH_IDENTITY])
        self.assertEqual(code, 1)

    def test_cli_expected_identity_without_verify_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            code = provenance.main(
                ["--root", str(root), "--output", str(root / "p.json"), "--artifact", str(artifact),
                 "--expected-identity", BRANCH_IDENTITY]
            )
        self.assertEqual(code, 1)

    def test_cli_branch_identity_roundtrip_verifies_with_stub(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.whl"
            artifact.write_bytes(b"candidate-bytes")
            bundle = root / "candidate.whl.sigstore"
            bundle.write_bytes(b"bundle-bytes")
            output = root / "provenance.json"
            code = provenance.main(
                ["--root", str(root), "--output", str(output), "--artifact", str(artifact),
                 "--identity", BRANCH_IDENTITY, "--signature-bundle", f"{artifact}={bundle}"]
            )
            self.assertEqual(code, 0)
            original = provenance._verify_bundle
            provenance._verify_bundle = _stub_verifier("verified")  # type: ignore[method-assign]
            try:
                result = provenance.verify_provenance(
                    output, require_signature=True, expected_identity=BRANCH_IDENTITY
                )
            finally:
                provenance._verify_bundle = original  # type: ignore[method-assign]
        self.assertTrue(result["passed"], result["failures"])


if __name__ == "__main__":
    unittest.main()
