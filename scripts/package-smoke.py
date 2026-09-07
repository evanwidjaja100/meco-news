"""Install a wheel or sdist into a clean environment outside the checkout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=120)


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
    """Return a compact, non-secret diagnostic for a failed subprocess."""

    detail = (result.stderr or result.stdout or "").strip().replace("\r", "")
    return detail[-1200:]


def run_smoke(artifact: str | Path, config: str | Path, build_tools: str | Path | None = None) -> dict[str, object]:
    package = Path(artifact).resolve()
    config_path = Path(config).resolve()
    if not package.is_file() or package.suffix.casefold() not in {".whl", ".gz", ".zip"}:
        raise ValueError("artifact must be an existing wheel or source archive")
    if not config_path.is_file():
        raise FileNotFoundError(str(config_path))
    with tempfile.TemporaryDirectory(prefix="meco-package-smoke-") as workspace:
        root = Path(workspace)
        venv = root / "venv"
        unrelated = root / "unrelated"
        unrelated.mkdir()
        create = _run([sys.executable, "-m", "venv", str(venv)], cwd=unrelated)
        if create.returncode != 0:
            raise RuntimeError("venv creation failed")
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install_command = [str(python), "-m", "pip", "install", "--no-index"]
        if package.suffix.casefold() != ".whl":
            if not build_tools:
                raise RuntimeError("sdist smoke requires a local build-tools wheelhouse")
            tools_path = Path(build_tools).resolve()
            build_tool_install = [
                *install_command,
                "--find-links",
                str(tools_path),
                "setuptools==75.8.0",
                "wheel==0.45.1",
            ]
            build_tools_result = _run(build_tool_install, cwd=unrelated)
            if build_tools_result.returncode != 0:
                raise RuntimeError(f"build-tools installation failed: {_command_detail(build_tools_result)}")
            install_command.extend(["--no-build-isolation"])
        install_command.append(str(package))
        pip_install = _run(install_command, cwd=unrelated)
        if pip_install.returncode != 0:
            raise RuntimeError(f"artifact installation failed: {_command_detail(pip_install)}")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        origin = _run(
            [str(python), "-c", "import meco_news; print(meco_news.__file__);"],
            cwd=unrelated,
            env=env,
        )
        cli = _run(
            [str(venv / ("Scripts/meco-news.exe" if os.name == "nt" else "bin/meco-news")), "--config", str(config_path), "--config-show", "--json"],
            cwd=unrelated,
            env=env,
        )
        if origin.returncode != 0 or cli.returncode != 0:
            failed = origin if origin.returncode != 0 else cli
            raise RuntimeError(f"installed artifact smoke test failed: {_command_detail(failed)}")
        origin_path = Path(origin.stdout.strip())
        checkout = Path(__file__).resolve().parents[1]
        if checkout in origin_path.parents:
            raise RuntimeError("smoke test imported the source checkout")
        report = json.loads(cli.stdout)
        if not isinstance(report, dict) or report.get("company") != "PT Meco Inoxprima":
            raise RuntimeError("installed CLI returned an unexpected config report")
        return {"artifact": str(package), "import_origin": str(origin_path), "cli": "passed"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test an installed MECO wheel or sdist outside the checkout")
    parser.add_argument("artifact")
    parser.add_argument("--config", default="config/watchlist.json")
    parser.add_argument("--build-tools", help="Wheelhouse containing pinned setuptools/wheel for sdist builds")
    args = parser.parse_args(argv)
    try:
        report = run_smoke(args.artifact, args.config, args.build_tools)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(json.dumps({"passed": False, "error_class": type(exc).__name__, "detail": str(exc)[:1600]}, sort_keys=True))
        return 1
    print(json.dumps({"passed": True, **report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
