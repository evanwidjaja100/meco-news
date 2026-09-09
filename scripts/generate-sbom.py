"""Generate a CycloneDX SBOM from the hashed requirement lock files.

The runtime service boundary is intentionally standard-library only, so the
candidate wheel ships no third-party runtime dependencies.  This SBOM records
exactly that: the candidate as ``metadata.component`` with zero runtime
components, plus the locked build/verification tooling as scoped components
(build lock is build-time-only, dev lock is verification tooling).

Serial numbers derive deterministically from the component set so the same
locks always produce the same SBOM bytes for a fixed timestamp.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import re
import uuid
from pathlib import Path


_VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")
_REQUIRE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)\s*\\?\s*$")
_HASH_RE = re.compile(r"^--hash=(\w+):([0-9a-fA-F]+)\s*\\?\s*$")
_HASH_ALG = {"sha256": "SHA-256", "sha384": "SHA-384", "sha512": "SHA-512"}


def _read_package_version(root: Path) -> str:
    match = _VERSION_RE.search((root / "meco_news" / "__init__.py").read_text(encoding="utf-8"))
    if not match:
        raise ValueError("package version is not declared in meco_news/__init__.py")
    return match.group(1)


def parse_lock(path: Path, scope: str) -> dict[tuple[str, str], dict[str, object]]:
    """Parse one ``pip hash-checking mode`` lock file into components.

    Malformed lines fail loudly: silently dropping a pinned dependency
    would produce a dishonest SBOM.
    """

    entries: dict[tuple[str, str], dict[str, object]] = {}
    current: tuple[str, str] | None = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        requirement = _REQUIRE_RE.match(line)
        if requirement is not None:
            current = (requirement.group(1), requirement.group(2))
            entries.setdefault(current, {"hashes": set(), "scope": scope})
            continue
        digest = _HASH_RE.match(line)
        if digest is not None and current is not None:
            algorithm = _HASH_ALG.get(digest.group(1).lower())
            if algorithm is None:
                raise ValueError(f"{path}:{number}: unsupported hash algorithm")
            entry = entries[current]
            hashes = entry["hashes"]
            assert isinstance(hashes, set)
            hashes.add((algorithm, digest.group(2).lower()))
            continue
        raise ValueError(f"{path}:{number}: malformed lock line")
    if current is None and not entries:
        raise ValueError(f"{path}: lock file declares no packages")
    return entries


def build_sbom(
    package: str,
    version: str,
    locks: list[tuple[Path, str]],
    timestamp: str,
) -> dict[str, object]:
    merged: dict[tuple[str, str], dict[str, object]] = {}
    for lock_path, scope in locks:
        for key, entry in parse_lock(lock_path, scope).items():
            # A pin present in both locks is verification tooling, not
            # build-only, so dev-lock membership upgrades its scope.
            if key in merged:
                existing = merged[key]
                assert isinstance(existing["hashes"], set)
                incoming = entry["hashes"]
                assert isinstance(incoming, set)
                existing["hashes"] |= incoming
                if existing["scope"] == "excluded":
                    existing["scope"] = entry["scope"]
            else:
                merged[key] = {"hashes": set(entry["hashes"]), "scope": entry["scope"]}  # type: ignore[arg-type]
    components = [
        {
            "bom-ref": f"{name}@{version_}",
            "type": "library",
            "name": name,
            "version": version_,
            "scope": entry["scope"],
            "hashes": [
                {"alg": algorithm, "content": content}
                for algorithm, content in sorted(entry["hashes"])  # type: ignore[union-attr]
            ],
        }
        for (name, version_), entry in sorted(merged.items())
    ]
    serial_source = json.dumps(
        {"package": package, "version": version, "components": components},
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_source)}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "component": {"bom-ref": f"{package}@{version}", "type": "application", "name": package, "version": version},
        },
        "components": components,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a CycloneDX SBOM from hashed lock files")
    parser.add_argument("--root", default=".")
    parser.add_argument("--build-lock", action="append", default=[])
    parser.add_argument("--dev-lock", action="append", default=[])
    parser.add_argument("--package", default="meco-news")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timestamp", default="")
    args = parser.parse_args(argv)
    try:
        root = Path(args.root).resolve()
        if not args.build_lock and not args.dev_lock:
            raise ValueError("at least one --build-lock or --dev-lock is required")
        locks = [(Path(value).resolve(), "excluded") for value in args.build_lock]
        locks += [(Path(value).resolve(), "optional") for value in args.dev_lock]
        for lock_path, _ in locks:
            if not lock_path.is_file():
                raise FileNotFoundError(str(lock_path))
        timestamp = args.timestamp or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        if not timestamp.strip():
            raise ValueError("timestamp must be nonempty")
        document = build_sbom(args.package, _read_package_version(root), locks, timestamp)
        target = Path(args.output).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error_class": type(exc).__name__}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "sbom": str(target)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
