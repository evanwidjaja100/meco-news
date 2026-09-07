# Release and rollback

Run the offline gates from a clean checkout:

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
python -B -m unittest discover -s tests -v
python -B -m pytest -q
python -m coverage erase
python -m coverage run --branch --source=meco_news -m pytest -q
python -m coverage json -o coverage.json
python scripts/coverage_gate.py --coverage-json coverage.json --critical-branches scripts/critical-branches.json
python -B -m meco_news --config-show --json
python -m build --no-isolation
python scripts/release-provenance.py --root . --output docs/evidence/production-readiness/local-YYYY-MM-DD/release/provenance.json --artifact dist/meco_news-2.0.0-py3-none-any.whl --artifact dist/meco_news-2.0.0.tar.gz
python scripts/release-provenance.py --output docs/evidence/production-readiness/local-YYYY-MM-DD/release/provenance.json --verify
```

For a dry-run, provide a validated version-1 frozen-input file rather than
calling live collectors:

```powershell
python -B -m meco_news --dry-run --frozen-input tests/fixtures/frozen-empty-v1.json --json
```

Build the wheel and source distribution once. Record the version, source commit, working-tree fingerprint, lockfile hash, immutable container base-image digest, final image digest, schema version, SBOM/provenance, and checksums. The repository Dockerfile defaults to a reviewed Python 3.14 slim-bookworm digest, but the release record must bind the exact candidate digest. Promote the same immutable image through shadow, canary, and production; never promote a mutable tag.

Before cutover:

1. Disable every old scheduler.
2. Confirm no active process or lease.
3. Create and verify an online SQLite backup.
4. Run offline preflight and one dry-run.
5. Migrate once, then enable exactly one scheduler.

Rollback stops the scheduler, preserves the current database/log evidence, and deploys the prior compatible digest. If schemas are incompatible, restore the verified pre-release backup before starting the old binary. Reconcile every ambiguous chunk first. Never run old and new binaries concurrently or use `--force` to bypass unresolved delivery state.
