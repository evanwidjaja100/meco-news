"""Enforce separate statement/branch coverage and critical branch evidence.

A critical-branch entry is evidence only when its location is observed in
the measured coverage: the registered line must be a measured executable
line that originates a measured arc, and any explicitly claimed arc must
occur in the measured arc set. Entries naming unmeasured lines, arcs, or
files are reported as unknown, and an empty register never passes, so a
stale or invented register fails closed instead of passing silently.
"""

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
        if not isinstance(file_data, dict) or not isinstance(line, int):
            unknown.append({"entry": entry, "reason": "coverage_location_not_found"})
            continue
        executed_lines = {int(value) for value in file_data.get("executed_lines", []) if isinstance(value, int)}
        missing_lines = {int(value) for value in file_data.get("missing_lines", []) if isinstance(value, int)}
        executed_branches = {
            (int(pair[0]), int(pair[1]))
            for pair in file_data.get("executed_branches", [])
            if isinstance(pair, list) and len(pair) == 2 and all(isinstance(value, int) for value in pair)
        }
        missing_branches = {
            (int(pair[0]), int(pair[1]))
            for pair in file_data.get("missing_branches", [])
            if isinstance(pair, list) and len(pair) == 2 and all(isinstance(value, int) for value in pair)
        }
        if line not in executed_lines and line not in missing_lines:
            unknown.append({"entry": entry, "reason": "line_not_measured"})
            continue
        measured_arcs = executed_branches | missing_branches
        if not any(origin == line for origin, _ in measured_arcs):
            unknown.append({"entry": entry, "reason": "line_not_a_branch"})
            continue
        branch = entry.get("branch")
        if branch is None:
            # A registered decision line is satisfied only when its line and
            # every measured outgoing branch are covered.
            if line in missing_lines or any(origin == line for origin, _ in missing_branches):
                missing.append({"file": filename, "line": line, "branch": branch, "tests": entry.get("tests", [])})
            continue
        if not (isinstance(branch, list) and len(branch) == 2 and all(isinstance(value, int) for value in branch)):
            unknown.append({"entry": entry, "reason": "branch_malformed"})
            continue
        claimed = (int(branch[0]), int(branch[1]))
        if claimed in missing_branches:
            missing.append({"file": filename, "line": line, "branch": branch, "tests": entry.get("tests", [])})
        elif claimed not in executed_branches:
            unknown.append({"entry": entry, "reason": "branch_not_measured"})
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
            "empty_register": not isinstance(entries, list) or len(entries) == 0,
            "passed": isinstance(entries, list) and len(entries) > 0 and not missing and not unknown,
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
