"""Build a disposable source copy and smoke test both installed artifacts."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent


def run(args, cwd):
    process = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=240)
    return {"command": args, "exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr}


with tempfile.TemporaryDirectory(prefix="meco-review-build-") as directory:
    source = Path(directory) / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "requirements-build.lock"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "meco_news", source / "meco_news", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    report = {"build": run([sys.executable, "-m", "build", "--no-isolation"], source), "smoke": [], "artifacts": []}
    if report["build"]["exit_code"] == 0:
        for artifact in sorted((source / "dist").iterdir()):
            report["artifacts"].append({"name": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()})
            report["smoke"].append(run([sys.executable, str(ROOT / "scripts/package-smoke.py"), str(artifact), "--config", str(ROOT / "config/watchlist.json"), "--build-tools", str(ROOT / "tmp-wheelhouse")], source))
    (OUT / "artifacts.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"build_exit": report["build"]["exit_code"], "smoke_exits": [entry["exit_code"] for entry in report["smoke"]], "artifacts": report["artifacts"]}, indent=2))
