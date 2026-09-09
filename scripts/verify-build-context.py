"""Verify Docker build-context hygiene without mutating the caller checkout.

The verifier has two deliberately separate results:

* the cheap ``.dockerignore``/Dockerfile lint, which is useful without Docker;
* an actual disposable Docker probe that builds a ``COPY .`` image and checks
  its saved layers, history, filesystem, and exported runtime filesystem.

Docker unavailability is a blocked external gate, not a successful context
verification. The disposable probe is independent of the application image,
so it never needs a network pull or a production secret.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid


REQUIRED_DOCKERIGNORE = (".env", "data/", "logs/", ".git", "__pycache__/")
CANARY_TEXT = "meco-build-context-canary-{token}"


class VerificationFailure(RuntimeError):
    """The verifier found an unsafe or failed condition."""


def _check_dockerignore_text(root: Path) -> dict[str, object]:
    dockerignore_path = root / ".dockerignore"
    dockerfile_path = root / "Dockerfile"
    try:
        dockerignore = dockerignore_path.read_text(encoding="utf-8")
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VerificationFailure(f"cannot read build metadata: {exc}") from exc

    missing = [rule for rule in REQUIRED_DOCKERIGNORE if rule not in dockerignore.splitlines()]
    if missing:
        raise VerificationFailure(".dockerignore is missing required rule(s): " + ", ".join(missing))
    if any(line.strip().startswith("COPY .") for line in dockerfile.splitlines()):
        raise VerificationFailure("Dockerfile uses a broad COPY")
    required_copy = ("COPY config ./config", "COPY meco_news ./meco_news")
    if any(entry not in dockerfile for entry in required_copy):
        raise VerificationFailure("Dockerfile does not copy required source/config explicitly")
    return {
        "status": "passed",
        "dockerignore": str(dockerignore_path),
        "dockerfile": str(dockerfile_path),
        "required_rules": list(REQUIRED_DOCKERIGNORE),
    }


def _patterns(root: Path) -> list[str]:
    return [
        line.strip()
        for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _dockerignored(relative: str, patterns: list[str]) -> bool:
    """Match the known canary corpus; Docker remains authoritative.

    Leading ``./`` segments are removed without mangling dotfile names, and
    a bare pattern naming a directory also excludes everything beneath it,
    mirroring Docker's own prefix behavior for excluded directories.
    """

    relative = relative.replace("\\", "/")
    while relative.startswith("./"):
        relative = relative[2:]
    relative = relative.lstrip("/")
    ignored = False
    for raw in patterns:
        negate = raw.startswith("!")
        pattern = raw[1:] if negate else raw
        pattern = pattern.lstrip("/")
        directory = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if directory:
            matched = relative == pattern or relative.startswith(pattern + "/")
        elif "/" not in pattern and not any(char in pattern for char in "*?["):
            matched = (
                relative == pattern
                or relative.startswith(pattern + "/")
                or fnmatch.fnmatchcase(Path(relative).name, pattern)
            )
        elif "/" not in pattern:
            matched = fnmatch.fnmatchcase(Path(relative).name, pattern)
        else:
            matched = fnmatch.fnmatchcase(relative, pattern)
        if matched:
            ignored = not negate
    return ignored


def _run(command: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def _docker_availability(root: Path) -> tuple[str, str]:
    if os.getenv("MECO_DISABLE_DOCKER_PROBE"):
        return "unavailable", "docker probe disabled by MECO_DISABLE_DOCKER_PROBE"
    try:
        result = _run(["docker", "version", "--format", "{{.Server.Version}}"], cwd=root, timeout=15)
    except FileNotFoundError:
        return "unavailable", "docker executable was not found"
    except subprocess.TimeoutExpired:
        return "unavailable", "docker daemon probe timed out"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "docker daemon is unavailable").strip().splitlines()[-1]
        return "unavailable", detail[:400]
    return "available", (result.stdout or "available").strip()[:80]


def _write_canaries(context: Path, token: str) -> tuple[list[Path], Path, str]:
    marker = CANARY_TEXT.format(token=token)
    negative = [
        context / ".env",
        context / "data" / f"state-{token}.db",
        context / "data" / f"backup-{token}.bak",
        context / "data" / f"manifest-{token}.json",
        context / "logs" / f"service-{token}.jsonl",
        context / ".git" / f"secret-{token}",
        context / "__pycache__" / f"secret-{token}.pyc",
        context / ".pytest_cache" / f"secret-{token}",
        context / "tests" / f"secret-{token}.txt",
        context / "research" / f"secret-{token}.txt",
    ]
    for path in negative:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(marker, encoding="utf-8")
    positive = context / "included" / f"leak-{token}.txt"
    positive.parent.mkdir(parents=True, exist_ok=True)
    positive.write_text(marker, encoding="utf-8")
    return negative, positive, marker


def _write_probe_dockerfile(context: Path) -> Path:
    probe = context / "Dockerfile.meco-context-probe"
    # The explicit CMD keeps `docker create` working on daemons that refuse
    # scratch images without a configured command; export only reads bytes.
    probe.write_text("FROM scratch\nCOPY . /context\nCMD [\"/bin/true\"]\n", encoding="utf-8")
    return probe


def _build_probe(context: Path, dockerfile: Path, tag: str, *, timeout: int) -> None:
    result = _run(
        [
            "docker",
            "build",
            "--no-cache",
            "--pull=false",
            "--file",
            dockerfile.name,
            "--tag",
            tag,
            ".",
        ],
        cwd=context,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "docker build failed").strip().splitlines()[-1]
        raise VerificationFailure(f"docker probe build failed: {detail[:500]}")


def _scan_image(tag: str, context: Path, marker: str, *, artifacts: Path, timeout: int) -> dict[str, bool]:
    """Inspect image output, writing exported tarballs outside the build context.

    Scan artifacts carry the canary marker by design; keeping them out of
    ``context`` prevents a later probe build from COPYing them back in.
    """
    """Inspect metadata, history, layer tar, and exported runtime bytes."""

    image_inspect = _run(["docker", "image", "inspect", tag], cwd=context, timeout=timeout)
    history = _run(["docker", "history", "--no-trunc", tag], cwd=context, timeout=timeout)
    if image_inspect.returncode != 0 or history.returncode != 0:
        raise VerificationFailure("docker image metadata inspection failed")
    if marker in image_inspect.stdout + image_inspect.stderr or marker in history.stdout + history.stderr:
        raise VerificationFailure("synthetic canary appeared in image metadata or history")

    saved = artifacts / f"{tag}-image.tar"
    save = _run(["docker", "image", "save", "--output", str(saved), tag], cwd=context, timeout=timeout)
    if save.returncode != 0:
        raise VerificationFailure("docker image layer export failed")
    if marker.encode("utf-8") in saved.read_bytes():
        raise VerificationFailure("synthetic canary appeared in an image layer")

    container = f"meco-context-container-{uuid.uuid4().hex[:12]}"
    created = _run(["docker", "create", "--name", container, tag], cwd=context, timeout=timeout)
    if created.returncode != 0:
        raise VerificationFailure("docker runtime container creation failed")
    runtime_tar = artifacts / f"{tag}-runtime.tar"
    try:
        exported = _run(["docker", "export", "--output", str(runtime_tar), container], cwd=context, timeout=timeout)
        if exported.returncode != 0:
            raise VerificationFailure("docker runtime filesystem export failed")
        if marker.encode("utf-8") in runtime_tar.read_bytes():
            raise VerificationFailure("synthetic canary appeared in the runtime filesystem")
    finally:
        _run(["docker", "rm", "--force", container], cwd=context, timeout=timeout)
    return {"metadata": True, "history": True, "layers": True, "runtime": True}


def _remove_image(tag: str, root: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError, subprocess.TimeoutExpired):
        _run(["docker", "image", "rm", "--force", tag], cwd=root, timeout=15)


def _check_actual_context(root: Path, *, timeout: int = 120) -> dict[str, object]:
    """Run the actual probe in a disposable copy and return a truthful report."""

    availability, detail = _docker_availability(root)
    if availability != "available":
        return {"status": "blocked", "reason": "docker_unavailable", "detail": detail}

    with tempfile.TemporaryDirectory(prefix="meco-build-context-") as temporary:
        context = Path(temporary) / "context"
        context.mkdir()
        artifacts = Path(temporary) / "artifacts"
        artifacts.mkdir()
        shutil.copy2(root / ".dockerignore", context / ".dockerignore")
        negative, positive, marker = _write_canaries(context, uuid.uuid4().hex)
        patterns = _patterns(context)
        unmatched = [
            path.relative_to(context).as_posix()
            for path in negative
            if not _dockerignored(path.relative_to(context).as_posix(), patterns)
        ]
        if unmatched:
            raise VerificationFailure("negative canary is not ignored: " + ", ".join(unmatched))
        probe = _write_probe_dockerfile(context)
        positive_tag = f"meco-context-positive-{uuid.uuid4().hex[:12]}"
        negative_tag = f"meco-context-negative-{uuid.uuid4().hex[:12]}"
        try:
            _build_probe(context, probe, positive_tag, timeout=timeout)
            positive_scan = _scan_image(positive_tag, context, marker, artifacts=artifacts, timeout=timeout)
            raise VerificationFailure("positive-control canary was not observed in image output")
        except VerificationFailure as exc:
            # The positive control is expected to be detected as a leak. Any
            # other failure is a real verifier failure and remains fatal.
            if "synthetic canary appeared" not in str(exc):
                raise
            positive_scan = {"detected": True}
        finally:
            _remove_image(positive_tag, root)
        positive.unlink(missing_ok=True)
        try:
            _build_probe(context, probe, negative_tag, timeout=timeout)
            negative_scan = _scan_image(negative_tag, context, marker, artifacts=artifacts, timeout=timeout)
        finally:
            _remove_image(negative_tag, root)
        return {
            "status": "passed",
            "docker": {"status": "available", "server": detail},
            "positive_control": {"status": "detected", **positive_scan},
            "negative_controls": {"status": "excluded", **negative_scan},
            "inspected": ["context-transfer-via-COPY", "metadata", "history", "layers", "runtime"],
        }


def _result(root: Path, *, require_docker: bool) -> tuple[int, dict[str, object]]:
    report: dict[str, object] = {"verifier": "meco-build-context-v2", "root": str(root)}
    try:
        report["lint"] = _check_dockerignore_text(root)
        actual = _check_actual_context(root)
        report["actual"] = actual
    except (OSError, VerificationFailure, subprocess.TimeoutExpired) as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        return 1, report
    actual_status = str(actual.get("status"))
    if actual_status == "passed":
        report["status"] = "passed"
        return 0, report
    report["status"] = "blocked"
    return (2 if require_docker else 0), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely verify Docker build-context exclusions in a disposable probe")
    parser.add_argument("--root", type=Path, help="source checkout to inspect; it is never written")
    parser.add_argument("--require-docker", action="store_true", help="return 2 when the Docker gate is unavailable")
    parser.add_argument("--json", action="store_true", help="emit exactly one JSON report on stdout")
    args = parser.parse_args(argv)
    root = (args.root or Path(__file__).resolve().parents[1]).resolve()
    if not root.is_dir():
        parser.error(f"root is not a directory: {root}")
    code, report = _result(root, require_docker=args.require_docker)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        lint = report.get("lint")
        print("build-context lint passed" if isinstance(lint, dict) and lint.get("status") == "passed" else "build-context lint failed")
        actual = report.get("actual")
        if isinstance(actual, dict) and actual.get("status") == "blocked":
            print(
                f"actual Docker context/layer/history/runtime verification BLOCKED: {actual.get('detail', actual.get('reason', 'unavailable'))}",
                file=sys.stderr,
            )
        elif report.get("status") == "passed":
            print("build-context sentinel passed; actual Docker context/layer/history/runtime verification passed")
        else:
            print(f"build-context verification failed: {report.get('error', 'unknown failure')}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
