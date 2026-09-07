"""Create and verify candidate-bound release provenance metadata."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Any


_FINGERPRINT_SKIP_DIRS = frozenset({".git", ".venv", "venv", "data", "logs", "backups", "dist", "build", "evidence", "tmp-wheelhouse"})
_FINGERPRINT_SKIP_NAMES = frozenset({".env", ".env.example"})


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


def create_provenance(root: str | Path, output: str | Path, artifacts: list[str | Path], context_report: str | Path | None = None) -> Path:
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
        "signature": {"state": "not_signed", "required_for_promotion": True},
        "sbom": {"state": "not_attached", "required_for_promotion": True},
    }
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return target


def verify_provenance(path: str | Path, *, require_signature: bool = False) -> dict[str, Any]:
    target = Path(path).resolve()
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("unsupported provenance document")
    failures: list[str] = []
    for record in raw.get("artifacts", []):
        artifact = Path(str(record.get("path", "")))
        if not artifact.is_file() or _hash(artifact) != record.get("sha256"):
            failures.append(str(artifact))
    if require_signature and raw.get("signature", {}).get("state") != "signed":
        failures.append("signature:not_signed")
    return {"passed": not failures, "failures": failures, "signature": raw.get("signature", {})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or verify candidate-bound provenance")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--context-report")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--require-signature", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            report = verify_provenance(args.output, require_signature=args.require_signature)
        else:
            if not args.artifact:
                raise ValueError("--artifact is required when creating provenance")
            path = create_provenance(args.root, args.output, args.artifact, args.context_report)
            report = {"passed": True, "provenance": str(path)}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"passed": False, "error_class": type(exc).__name__}, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
