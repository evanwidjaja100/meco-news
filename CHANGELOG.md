# Changelog

## Unreleased

- Windows CI hardening (PR #12): hermetic Docker gate via `MECO_DISABLE_DOCKER_PROBE` (can only force "blocked", never fake "passed"); 8.3 short-path canonicalization in path comparisons; `maintenance.runtime_dir()` resolves its input so short/long aliases share one marker dir; stdlib parser maps hostile-markup `AssertionError` to `ValueError`.
- Build-context verifier: probe image carries a CMD so `docker create` works on scratch; scan tarballs stay outside the probe build context; dockerignore matcher handles dotfiles and bare directory patterns; `sys.platform` guards for Windows-only APIs.
- Release evidence (PR #13): deterministic CycloneDX 1.5 SBOM generator from the hash-locked build/dev inputs (`scripts/generate-sbom.py`, fixed-timestamp reproducible); provenance attach/verify gains an independent `--require-sbom` flag (`sbom:missing` / `sbom:mismatch` / `sbom:not_attached` fail closed). The signature gate stays fail-closed (`signature:unverifiable`) until a trust root is approved and wired.
- Release decision remains NO-GO; supervised non-production pilot only (see `PRODUCTION_READINESS_CLOSURE_IMPLEMENTATION_PLAN.md`).

## 2.0.0 - production-readiness implementation

- Added typed strict configuration and safe CLI/preflight/status/health/backup modes.
- Added versioned SQLite migrations, leases, immutable generations, delivery attempts, durable outbox chunks, ambiguity resolution, and URL/title history.
- Added freshness enforcement, deterministic bounded deduplication, URL/redirect/SSRF policy, bounded parsers, source quarantine, and Telegram size/error invariants.
- Added structured redacted events, backup/restore tooling, hardened Docker/Compose and Windows scheduling, packaging metadata, CI scaffolding, and runbooks.

## 1.0.0

- Initial dependency-free collectors, ranker, Telegram formatter, and SQLite history store.

