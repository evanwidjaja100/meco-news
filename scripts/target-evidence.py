"""Validate externally produced target-platform evidence without faking it."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import platform


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def evaluate(candidate: str | Path, evidence: str | Path, expected_platform: str) -> dict[str, object]:
    target = Path(candidate).resolve()
    evidence_path = Path(evidence).resolve()
    failures: list[str] = []
    if not target.is_file():
        failures.append("candidate_missing")
    if not evidence_path.is_file():
        failures.append("target_evidence_missing")
        raw: object = {}
    else:
        try:
            raw = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raw = {}
            failures.append("target_evidence_invalid")
    if isinstance(raw, dict):
        if str(raw.get("target_platform", "")).casefold() != expected_platform.casefold():
            failures.append("target_platform_mismatch")
        if target.is_file() and raw.get("candidate_sha256") != _hash(target):
            failures.append("candidate_hash_mismatch")
        checks = raw.get("checks")
        if not isinstance(checks, dict) or not checks or not all(value is True for value in checks.values()):
            failures.append("target_checks_incomplete")
        if not raw.get("operator") or not raw.get("captured_at"):
            failures.append("target_evidence_identity_missing")
    return {"passed": not failures, "current_platform": platform.system().casefold(), "failures": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify target evidence captured on the selected host")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--platform", choices=("windows", "linux"), required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.candidate, args.evidence, args.platform)
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"passed": False, "state": "blocked", "error_class": type(exc).__name__}, sort_keys=True))
        return 2
    if not report["passed"]:
        report["state"] = "blocked"
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
