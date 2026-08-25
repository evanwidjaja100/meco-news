# Release and rollback

Run the offline gates from a clean checkout:

```powershell
python -B -m unittest discover -s tests -v
python -B -m meco_news --config-show --json
python -m build --no-isolation
```

Build the wheel and source distribution once. Record the version, source commit, lockfile hash, container base-image digest, image digest, schema version, SBOM/provenance, and checksums. Promote the same immutable image through shadow, canary, and production; never promote a mutable tag.

Before cutover:

1. Disable every old scheduler.
2. Confirm no active process or lease.
3. Create and verify an online SQLite backup.
4. Run offline preflight and one dry-run.
5. Migrate once, then enable exactly one scheduler.

Rollback stops the scheduler, preserves the current database/log evidence, and deploys the prior compatible digest. If schemas are incompatible, restore the verified pre-release backup before starting the old binary. Reconcile every ambiguous chunk first. Never run old and new binaries concurrently or use `--force` to bypass unresolved delivery state.

