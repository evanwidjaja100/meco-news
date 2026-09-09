"""Run verified backup/retention/pruning operations with JSON receipts.

This script performs only local operations.  Off-host replication is an
explicit adapter boundary: ``--replication`` records a checksum receipt but
does not claim that bytes were transferred unless a caller supplies a
transport receipt and ``--confirmed``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# The script is also used from an installed checkout-independent scheduler;
# when run from a source tree, make the repository package import explicit.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meco_news.backup import BackupArtifact, _load_manifest, _sha256
from meco_news.operations import (
    apply_retention_plan,
    build_retention_plan,
    prune_state_history,
    scheduled_backup,
    write_replication_receipt,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verified MECO state backup and retention operations")
    parser.add_argument("--mode", choices=("backup", "retention", "prune", "replication"), default="backup")
    parser.add_argument("--state", default="data/meco_news.db")
    parser.add_argument("--output", help="Backup file or directory")
    parser.add_argument("--lock", help="Backup-job lock path")
    parser.add_argument("--receipt", help="Local JSON receipt path")
    parser.add_argument("--config-hash", default="")
    parser.add_argument("--apply", action="store_true", help="Apply retention/pruning instead of previewing")
    parser.add_argument("--attempt-days", type=int, default=90)
    parser.add_argument("--article-days", type=int, default=365)
    parser.add_argument("--backup", help="Existing verified backup database for --mode replication")
    parser.add_argument("--manifest", help="Existing backup manifest for --mode replication")
    parser.add_argument("--destination", help="Off-host destination label for --mode replication")
    parser.add_argument("--transport-receipt", default="")
    parser.add_argument("--confirmed", action="store_true")
    return parser


def _emit(payload: object, *, stream: object = sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream)  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mode == "backup":
            if not args.output:
                raise ValueError("--output is required for backup mode")
            artifact, receipt = scheduled_backup(
                args.state,
                args.output,
                lock_path=args.lock,
                receipt_path=args.receipt,
                config_hash=args.config_hash,
            )
            _emit({"database": str(artifact.database), "manifest": str(artifact.manifest), "receipt": receipt})
            return 0
        if args.mode == "retention":
            if not args.output:
                raise ValueError("--output is required for retention mode")
            plan = build_retention_plan(args.output)
            payload = {"mode": "retention", "applied": False, "plan": plan.as_dict()}
            if args.apply:
                payload["result"] = apply_retention_plan(plan, directory=args.output)
                payload["applied"] = True
            _emit(payload)
            return 0
        if args.mode == "prune":
            result = prune_state_history(
                args.state,
                apply=args.apply,
                attempt_retention_days=args.attempt_days,
                article_retention_days=args.article_days,
            )
            _emit({"mode": "prune", "result": result})
            return 0
        if not args.backup or not args.destination:
            raise ValueError("--backup and --destination are required for replication mode")
        backup = Path(args.backup).resolve()
        manifest = Path(args.manifest).resolve() if args.manifest else backup.with_suffix(backup.suffix + ".manifest.json")
        data = _load_manifest(manifest, backup)
        artifact = BackupArtifact(backup, manifest, _sha256(backup))
        receipt = write_replication_receipt(
            artifact,
            args.receipt or str(backup.with_suffix(backup.suffix + ".replication.json")),
            destination=args.destination,
            transport_receipt=args.transport_receipt,
            confirmed=args.confirmed,
        )
        _emit({"mode": "replication", "manifest_backup_id": data["backup_id"], "receipt": str(receipt)})
        return 0
    except Exception as exc:
        _emit({"code": 1, "outcome": "operation_failed", "error_class": type(exc).__name__, "detail": str(exc)}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
