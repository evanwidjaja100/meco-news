"""Record review inputs without reading .env, state, or other runtime data."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
paths = []
for directory in ("meco_news", "tests", "scripts", "config", ".github"):
    paths.extend(p for p in (ROOT / directory).rglob("*") if p.is_file() and p.suffix in {".py", ".ps1", ".json", ".yml", ".yaml"} and "__pycache__" not in p.parts)
for filename in ("README.md", "pyproject.toml", "requirements.lock", "requirements-build.lock", "requirements-dev.lock", "Dockerfile", "compose.yaml", ".dockerignore", ".gitignore", "SECURITY.md"):
    paths.append(ROOT / filename)
paths = sorted(set(paths))
manifest = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(ROOT).as_posix()}\n" for path in paths)
(OUT / "source-manifest.sha256").write_text(manifest, encoding="utf-8")
coverage = json.loads((OUT / "coverage.json").read_text(encoding="utf-8"))
register = json.loads((ROOT / "scripts/critical-branches.json").read_text(encoding="utf-8"))
locations = []
for entry in register["branches"]:
    data = coverage["files"].get(entry["file"], {})
    lines = (ROOT / entry["file"]).read_text(encoding="utf-8").splitlines()
    line = entry["line"]
    arcs = data.get("executed_branches", []) + data.get("missing_branches", [])
    locations.append({**entry, "current_source_line": lines[line-1] if 0 < line <= len(lines) else None, "measured_executable_line": line in data.get("executed_lines", []) + data.get("missing_lines", []), "measured_branch_origin": any(pair[0] == line for pair in arcs)})
report = {
    "review_date": "2026-09-07",
    "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    "scope": "current modified/untracked working tree; application inputs left unchanged",
    "python": platform.python_version(), "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
    "sqlite_source_id": sqlite3.connect(":memory:").execute("SELECT sqlite_source_id()").fetchone()[0],
    "tools": {name: importlib.metadata.version(name) for name in ("pytest", "coverage", "ruff", "mypy", "build", "setuptools", "wheel")},
    "pytest": {"passed": 509, "failed": 0, "duration_seconds": 143.08, "command": ".venv/Scripts/python.exe -m coverage run --data-file=docs/reviews/review-2026-09-07.coverage --branch --source=meco_news -m pytest --junitxml=docs/reviews/review-2026-09-07-pytest.xml"},
    "coverage_totals": coverage["totals"],
    "ruff": {"exit_code": 0, "command": ".venv/Scripts/python.exe -m ruff check meco_news tests scripts"},
    "mypy": {"exit_code": 0, "source_modules": 22, "command": ".venv/Scripts/python.exe -m mypy meco_news"},
    "coverage_gate": {"reported_passed": True, "critical_register_reliable": False, "locations": locations},
    "compose_syntax": "passed",
    "docker_runtime": {"state": "blocked", "reason": "dockerDesktopLinuxEngine named pipe missing", "required_context_verifier_exit_code": 1},
    "base_manifest": {"state": "registry_resolved", "digest": "sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f", "version_annotation": "3.14.7-slim-bookworm"},
    "review_probes": json.loads((OUT / "reproductions.json").read_text(encoding="utf-8")),
    "source_manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
    "manifest_files": len(paths),
    "limitations": ["No target container execution", "No live Telegram sends", "No target scheduler installation", "No host power-loss test", "No remote CI/protection verification", "No complete secret-history or dependency/image vulnerability scan"],
}
(OUT / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
bad_links = []
for path, line in re.findall(r"\]\(<([A-Za-z]:/[^>]+?)(?::(\d+))?>\)", (OUT / "REVIEW.md").read_text(encoding="utf-8")):
    target = Path(path)
    if not target.exists() or line and int(line) > len(target.read_text(encoding="utf-8").splitlines()):
        bad_links.append({"path": path, "line": line})
print(json.dumps({"manifest_files": len(paths), "invalid_report_links": bad_links, "probe_errors": [name for name,value in report["review_probes"].items() if "probe_error" in value]}, indent=2))
