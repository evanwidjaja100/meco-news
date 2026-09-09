# Changelog

## Unreleased

- Windows CI hardening (PR #12): hermetic Docker gate via `MECO_DISABLE_DOCKER_PROBE` (can only force "blocked", never fake "passed"); 8.3 short-path canonicalization in path comparisons; `maintenance.runtime_dir()` resolves its input so short/long aliases share one marker dir; stdlib parser maps hostile-markup `AssertionError` to `ValueError`.
- Build-context verifier: probe image carries a CMD so `docker create` works on scratch; scan tarballs stay outside the probe build context; dockerignore matcher handles dotfiles and bare directory patterns; `sys.platform` guards for Windows-only APIs.
- Release evidence (PR #13): deterministic CycloneDX 1.5 SBOM generator from the hash-locked build/dev inputs (`scripts/generate-sbom.py`, fixed-timestamp reproducible); provenance attach/verify gains an independent `--require-sbom` flag (`sbom:missing` / `sbom:mismatch` / `sbom:not_attached` fail closed). The signature gate stays fail-closed (`signature:unverifiable`) until a trust root is approved and wired.
- Release decision remains NO-GO; supervised non-production pilot only (see `PRODUCTION_READINESS_CLOSURE_IMPLEMENTATION_PLAN.md`).
- Release process (PR #15): the offline gates in `docs/release.md` now generate the SBOM and verify attached provenance (`--sbom-report` / `--require-sbom`); the readiness status carries the verified 2026-09-09 delta.
- CI release evidence (PR #16): every `package` build generates `dist/sbom.json`, binds it into `dist/provenance.json`, and verifies with `--require-sbom`. Fixed the `scripts/generate-sbom.py` default timestamp to `datetime.now(UTC)` (codebase spelling; the `datetime.datetime.UTC` attribute does not exist on this matrix) with a regression test covering the no-`--timestamp` path.

- Ledger (PR #17/PR #18): PR #17 records the PR #15/PR #16 release-process entries; PR #18 records the CI build-context probe evidence in the readiness status. No behavior change; release decision remains NO-GO.

## 2.0.0 - production-readiness implementation

- Added typed strict configuration and safe CLI/preflight/status/health/backup modes.
- Added versioned SQLite migrations, leases, immutable generations, delivery attempts, durable outbox chunks, ambiguity resolution, and URL/title history.
- Added freshness enforcement, deterministic bounded deduplication, URL/redirect/SSRF policy, bounded parsers, source quarantine, and Telegram size/error invariants.
- Added structured redacted events, backup/restore tooling, hardened Docker/Compose and Windows scheduling, packaging metadata, CI scaffolding, and runbooks.

## 1.0.0

- Initial dependency-free collectors, ranker, Telegram formatter, and SQLite history store.

