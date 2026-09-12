"""Create and verify candidate-bound release provenance metadata."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


_FINGERPRINT_SKIP_DIRS = frozenset({".git", ".venv", "venv", "data", "logs", "backups", "dist", "build", "evidence", "tmp-wheelhouse"})
_FINGERPRINT_SKIP_NAMES = frozenset({".env", ".env.example"})

_SIGSTORE_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_SIGSTORE_IDENTITY = "https://github.com/evanwidjaja100/meco-news/.github/workflows/ci.yml@refs/heads/main"
_WORKFLOW_IDENTITY_PREFIX = (
    "https://github.com/evanwidjaja100/meco-news/.github/workflows/ci.yml@refs/"
)
_VERIFY_TIMEOUT_SECONDS = 120


def _checked_identity(value: str | None) -> str:
    identity = value or _SIGSTORE_IDENTITY
    if not identity.startswith(_WORKFLOW_IDENTITY_PREFIX):
        raise ValueError("identity must be this repository workflow ref identity")
    return identity


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False, timeout=30)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _git_bytes(root: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False, timeout=30)
    return completed.stdout if completed.returncode == 0 else b""


def _skip_untracked(relative: str) -> bool:
    path = Path(relative)
    if any(part in _FINGERPRINT_SKIP_DIRS for part in path.parts):
        return True
    name = path.name
    if name in _FINGERPRINT_SKIP_NAMES or name.startswith(".coverage"):
        return True
    if name.startswith(("cov_run", "full_run", "pytest-", "tmp_", "tmp-")):
        return True
    return name.endswith((".db", ".db-wal", ".db-shm", ".bak", ".manifest.json", ".replication.json"))


def _working_tree_fingerprint(root: Path) -> str:
    """Hash tracked diffs and relevant untracked source bytes without printing them."""

    digest = sha256()
    digest.update(b"status\0" + _git_bytes(root, "status", "--porcelain=v1", "-z"))
    digest.update(b"tracked-diff\0" + _git_bytes(root, "diff", "--binary", "--no-ext-diff", "--no-color", "HEAD", "--"))
    untracked = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    for raw_path in sorted(value for value in untracked.split(b"\0") if value):
        relative = raw_path.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        if _skip_untracked(relative):
            continue
        candidate = root / Path(relative)
        if candidate.is_symlink() or not candidate.is_file():
            continue
        digest.update(b"untracked\0" + relative.encode("utf-8", errors="surrogateescape") + b"\0")
        digest.update(_hash(candidate).encode("ascii") + b"\0")
    return digest.hexdigest()


def create_provenance(
    root: str | Path,
    output: str | Path,
    artifacts: list[str | Path],
    context_report: str | Path | None = None,
    sbom_report: str | Path | None = None,
    signature_bundles: dict[str | Path, str | Path] | None = None,
    identity: str | None = None,
) -> Path:
    repository = Path(root).resolve()
    files = [Path(value).resolve() for value in artifacts]
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(missing))
    artifact_records = [{"path": str(path), "sha256": _hash(path), "bytes": path.stat().st_size} for path in files]
    context_record: dict[str, Any] = {"state": "not_provided"}
    if context_report:
        context_path = Path(context_report).resolve()
        if not context_path.is_file():
            raise FileNotFoundError(str(context_path))
        context_record = {"state": "provided", "path": str(context_path), "sha256": _hash(context_path)}
    sbom_record: dict[str, Any] = {"state": "not_attached", "required_for_promotion": True}
    if sbom_report:
        sbom_path = Path(sbom_report).resolve()
        if not sbom_path.is_file():
            raise FileNotFoundError(str(sbom_path))
        sbom_record = {"state": "attached", "path": str(sbom_path), "sha256": _hash(sbom_path)}
    covered = [str(path) for path in files]
    if sbom_record["state"] == "attached":
        covered.append(str(sbom_record["path"]))
    bundle_records: list[dict[str, Any]] = []
    if signature_bundles:
        seen: set[str] = set()
        for raw_artifact, raw_bundle in signature_bundles.items():
            key = str(Path(raw_artifact).resolve())
            if key not in covered:
                raise ValueError(f"signature bundle does not match a bound artifact: {raw_artifact}")
            if key in seen:
                raise ValueError(f"duplicate signature bundle for {raw_artifact}")
            seen.add(key)
            bundle_path = Path(raw_bundle).resolve()
            if not bundle_path.is_file():
                raise FileNotFoundError(str(bundle_path))
            bundle_records.append(
                {"artifact": key, "path": str(bundle_path), "sha256": _hash(bundle_path)}
            )
    bound = {entry["artifact"] for entry in bundle_records}
    signed = bool(covered) and all(path in bound for path in covered)
    signature_record: dict[str, Any] = {
        "state": "signed" if signed else "not_signed",
        "required_for_promotion": True,
        "bundles": bundle_records,
        "identity_policy": {"oidc_issuer": _SIGSTORE_OIDC_ISSUER, "identity": _checked_identity(identity)},
    }
    payload = {
        "schema_version": 1,
        "source": {
            "root": str(repository),
            "git_commit": _git(repository, "rev-parse", "HEAD"),
            "git_status_sha256": sha256(_git(repository, "status", "--porcelain=v1").encode()).hexdigest(),
            "working_tree_sha256": _working_tree_fingerprint(repository),
        },
        "artifacts": artifact_records,
        "build_context": context_record,
        "python_support": ">=3.12,<3.15",
        "schema_compatibility": "application-enforced current schema",
        "signature": signature_record,
        "sbom": sbom_record,
    }
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return target


def _context_failures(raw: dict[str, Any]) -> list[str]:
    """Check that provided build-context evidence still matches its record."""

    context = raw.get("build_context", {})
    if not isinstance(context, dict) or context.get("state") != "provided":
        return []
    candidate = Path(str(context.get("path", "")))
    if not candidate.is_file():
        return ["context:missing"]
    expected = context.get("sha256", "")
    if not expected or _hash(candidate) != expected:
        return ["context:mismatch"]
    return []


def _sbom_failures(raw: dict[str, Any]) -> list[str]:
    """Check that an attached SBOM still matches its record."""

    sbom = raw.get("sbom", {})
    if not isinstance(sbom, dict) or sbom.get("state") != "attached":
        return []
    candidate = Path(str(sbom.get("path", "")))
    if not candidate.is_file():
        return ["sbom:missing"]
    expected = sbom.get("sha256", "")
    if not expected or _hash(candidate) != expected:
        return ["sbom:mismatch"]
    return []


def _verify_bundle(bundle: Path, artifact: Path, expected_identity: str | None = None) -> str:
    """Verify one Sigstore bundle against the expected CI workflow-ref identity.

    The default is the refs/heads/main pin; CI passes its own workflow ref so
    branch runs verify the identity they actually signed with. Returns
    "verified", "rejected", or "unavailable" when sigstore-python is not
    installed or the verifier does not complete in time. Rejection and
    absence both fail the gate; a missing verifier never weakens it.
    """
    identity = _checked_identity(expected_identity)
    try:
        completed = subprocess.run(
            [
                sys.executable, "-m", "sigstore", "verify", "identity",
                "--bundle", str(bundle),
                "--cert-identity", identity,
                "--cert-oidc-issuer", _SIGSTORE_OIDC_ISSUER,
                str(artifact),
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=_VERIFY_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return "unavailable"
    except subprocess.TimeoutExpired:
        return "unavailable"
    if completed.returncode == 0:
        return "verified"
    if "No module named sigstore" in completed.stderr:
        return "unavailable"
    return "rejected"


def _signature_failures(raw: dict[str, Any], expected_identity: str | None = None) -> list[str]:
    """Check bound Sigstore bundles against the expected identity policy."""
    signature = raw.get("signature", {})
    if not isinstance(signature, dict) or signature.get("state") != "signed":
        return ["signature:not_signed"]
    bundles = signature.get("bundles", [])
    if not isinstance(bundles, list) or not bundles:
        return ["signature:unverifiable"]
    records = raw.get("artifacts", [])
    bound_paths = [str(record.get("path", "")) for record in records if isinstance(record, dict)]
    sbom = raw.get("sbom", {})
    if isinstance(sbom, dict) and sbom.get("state") == "attached":
        bound_paths.append(str(sbom.get("path", "")))
    by_artifact: dict[str, dict[str, Any]] = {}
    for entry in bundles:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("artifact"), str)
            or not entry.get("artifact")
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("sha256"), str)
        ):
            return ["signature:bundles_malformed"]
        by_artifact[str(entry["artifact"])] = entry
    failures: list[str] = []
    for path in bound_paths:
        if not path or path not in by_artifact:
            failures.append("signature:bundle_missing")
    for key in by_artifact:
        if key not in bound_paths:
            failures.append("signature:bundle_unknown")
    if failures:
        return failures
    for entry in by_artifact.values():
        candidate = Path(str(entry["path"]))
        if not candidate.is_file():
            failures.append("signature:bundle_missing")
        elif not entry["sha256"] or _hash(candidate) != entry["sha256"]:
            failures.append("signature:bundle_mismatch")
    if failures:
        return failures
    for key, entry in by_artifact.items():
        outcome = _verify_bundle(Path(str(entry["path"])), Path(key), expected_identity)
        if outcome == "unavailable":
            failures.append("signature:verifier_unavailable")
        elif outcome != "verified":
            failures.append(f"signature:rejected:{key}")
    return failures



def verify_provenance(
    path: str | Path,
    *,
    require_signature: bool = False,
    require_sbom: bool = False,
    expected_identity: str | None = None,
) -> dict[str, Any]:
    target = Path(path).resolve()
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("unsupported provenance document")
    records = raw.get("artifacts", [])
    if not isinstance(records, list):
        raise ValueError("provenance artifacts must be a list")
    failures: list[str] = []
    if require_signature and not records:
        failures.append("artifacts:empty")
    for record in records:
        if not isinstance(record, dict):
            failures.append("artifacts:malformed")
            continue
        artifact = Path(str(record.get("path", "")))
        if not artifact.is_file() or _hash(artifact) != record.get("sha256"):
            failures.append(str(artifact))
    if require_sbom:
        sbom = raw.get("sbom", {})
        if not isinstance(sbom, dict) or sbom.get("state") != "attached":
            failures.append("sbom:not_attached")
        else:
            failures.extend(_sbom_failures(raw))
    if require_signature:
        failures.extend(_context_failures(raw))
        failures.extend(_signature_failures(raw, expected_identity))
    return {
        "passed": not failures,
        "failures": failures,
        "signature": raw.get("signature", {}),
        "sbom": raw.get("sbom", {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or verify candidate-bound provenance")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--context-report")
    parser.add_argument("--sbom-report")
    parser.add_argument("--signature-bundle", action="append", default=[], metavar="ARTIFACT=BUNDLE")
    parser.add_argument("--identity", default=None, help="Workflow-ref identity to record when creating provenance")
    parser.add_argument("--expected-identity", default=None, help="Workflow-ref identity to verify against")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--require-signature", action="store_true")
    parser.add_argument("--require-sbom", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.signature_bundle and args.verify:
            raise ValueError("--signature-bundle is only used when creating provenance")
        if args.identity and args.verify:
            raise ValueError("--identity is only used when creating provenance")
        if args.expected_identity and not args.verify:
            raise ValueError("--expected-identity is only used when verifying provenance")
        bundles: dict[str, str] = {}
        for item in args.signature_bundle:
            name, separator, bundle = item.partition("=")
            if not separator or not name or not bundle:
                raise ValueError("--signature-bundle must be ARTIFACT=BUNDLE")
            if name in bundles:
                raise ValueError(f"duplicate signature bundle for {name}")
            bundles[name] = bundle
        if args.verify:
            report = verify_provenance(
                args.output,
                require_signature=args.require_signature,
                require_sbom=args.require_sbom,
                expected_identity=args.expected_identity,
            )
        else:
            if not args.artifact:
                raise ValueError("--artifact is required when creating provenance")
            path = create_provenance(
                args.root,
                args.output,
                args.artifact,
                args.context_report,
                args.sbom_report,
                bundles or None,
                identity=args.identity,
            )
            report = {"passed": True, "provenance": str(path)}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"passed": False, "error_class": type(exc).__name__}, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
