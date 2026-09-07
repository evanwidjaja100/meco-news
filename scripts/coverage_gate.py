"""Enforce separate statement/branch coverage and critical branch evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _normalise_file(value: str) -> str:
    return value.replace("\\", "/").lstrip("./")


def evaluate(
    coverage_path: str | Path,
    critical_path: str | Path,
    *,
    statement_floor: float = 90.0,
    branch_floor: float = 90.0,
) -> dict[str, Any]:
    data = json.loads(Path(coverage_path).read_text(encoding="utf-8"))
    totals = data.get("totals", {})
    statements = int(totals.get("num_statements", 0) or 0)
    covered_statements = int(totals.get("covered_lines", 0) or 0)
    branches = int(totals.get("num_branches", 0) or 0)
    covered_branches = int(totals.get("covered_branches", 0) or 0)
    statement_percent = (100.0 * covered_statements / statements) if statements else 0.0
    branch_percent = (100.0 * covered_branches / branches) if branches else 0.0
    files = {_normalise_file(str(name)): value for name, value in data.get("files", {}).items()}
    register_raw = json.loads(Path(critical_path).read_text(encoding="utf-8"))
    entries = register_raw.get("branches", []) if isinstance(register_raw, dict) else []
    missing: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            unknown.append({"entry": entry, "reason": "entry_not_object"})
            continue
        filename = _normalise_file(str(entry.get("file", "")))
        line = entry.get("line")
        file_data = files.get(filename)
        if not file_data or not isinstance(line, int):
            unknown.append({"entry": entry, "reason": "coverage_location_not_found"})
            continue
        missing_lines = {int(value) for value in file_data.get("missing_lines", []) if isinstance(value, int)}
        missing_branches = {
            (int(pair[0]), int(pair[1]))
            for pair in file_data.get("missing_branches", [])
            if isinstance(pair, list) and len(pair) == 2 and all(isinstance(value, int) for value in pair)
        }
        branch = entry.get("branch")
        if isinstance(branch, list) and len(branch) == 2 and all(isinstance(value, int) for value in branch):
            failed = (int(branch[0]), int(branch[1])) in missing_branches
        else:
            # A registered decision line is satisfied only when its line and
            # every measured outgoing branch are covered.
            failed = line in missing_lines or any(origin == line for origin, _ in missing_branches)
        if failed:
            missing.append({"file": filename, "line": line, "branch": branch, "tests": entry.get("tests", [])})
    result = {
        "statement": {
            "covered": covered_statements,
            "total": statements,
            "percent": round(statement_percent, 3),
            "floor": statement_floor,
            "passed": statement_percent >= statement_floor,
        },
        "branch": {
            "covered": covered_branches,
            "total": branches,
            "percent": round(branch_percent, 3),
            "floor": branch_floor,
            "passed": branch_percent >= branch_floor,
        },
        "critical_branches": {
            "registered": len(entries) if isinstance(entries, list) else 0,
            "missing": missing,
            "unknown": unknown,
            "passed": not missing and not unknown,
        },
    }
    result["passed"] = bool(result["statement"]["passed"] and result["branch"]["passed"] and result["critical_branches"]["passed"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enforce MECO statement, branch, and critical-branch coverage")
    parser.add_argument("--coverage-json", default="coverage.json")
    parser.add_argument("--critical-branches", default="scripts/critical-branches.json")
    parser.add_argument("--statement-floor", type=float, default=90.0)
    parser.add_argument("--branch-floor", type=float, default=90.0)
    args = parser.parse_args(argv)
    try:
        report = evaluate(
            args.coverage_json,
            args.critical_branches,
            statement_floor=args.statement_floor,
            branch_floor=args.branch_floor,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"passed": False, "error_class": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
