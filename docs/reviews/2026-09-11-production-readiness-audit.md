# Production-readiness audit - 2026-09-11 (Asia/Jakarta)

Head: 952a72a (main, clean), version 2.0.0, schema v5.
Method: fresh worktree inspection plus live gate re-runs.

## Verdict: NO-GO for unattended production; GO for supervised pilot only

Agrees with docs/production-readiness-status.md and local-2026-09-07 evidence
(release_decision NO-GO, approvals not_obtained, external gates blocked/not_run).

## Fresh evidence collected this audit

- ruff check meco_news tests scripts: pass (re-ran).
- mypy meco_news: Success, no issues in 22 source files (re-ran).
- --config-show --json: valid redacted config, hash 38a641e6 (re-ran).
- --preflight --json without creds: truthful preflight_failed, exit 4 (re-ran, correct fail-closed).
- --dry-run --frozen-input tests/fixtures/frozen-empty-v1.json: 0 items, healthy/degraded (re-ran).
- Spot suites green: test_telegram plus test_ranking (6), test_c03_hostile_red (10) (re-ran).
- Full suite (631 tests, about 143s) cited via PR34 ledger (630 passed, 1 skipped) plus green
  protected-main CI matrix; not re-run in one window here.
- Secrets: only .env.example placeholder; .env gitignored; no live token in tree.
- Storage: journal_mode WAL plus synchronous FULL enforced fail-closed (storage.py:287-293),
  busy_timeout, foreign_keys ON, integrity checks.
- Network: fail-closed URL policy (urls.py), pinned-address TLS (network.py), max 2 redirects,
  proxy inheritance disabled, per-source and cycle deadlines, byte and parser budgets.
- Worker isolation: _source_process_entry installs redacting logging (collectors.py:676),
  typed bounded IPC frames; pickle is internal parent/child pipe only.
- Backup lock: stale-marker compare-and-swap (operations.py:119-130), no blind unlink.
- Release: --require-signature and --require-sbom fail closed; SBOM deterministic CycloneDX 1.5
  from hash-locked inputs; signature has no trust root by design.
- Container: digest-pinned slim-bookworm base, UID 10001, read-only FS, no-new-privileges,
  cap_drop ALL, pids/mem limits, healthcheck.

## Why still NO-GO

1. No signature trust root; provenance signature:not_signed.
2. No exact-candidate Docker context/layer/runtime evidence (local daemon blocked).
3. No target-host reports: Linux NAS, Windows scheduler, egress/firewall, power-loss durability.
4. No human approvals (business/operations/security/release not_obtained, approver TBD).
5. Sep-07 P1/P2 probes implementer-addressed (R01-R20, C4.x proofs, 16-entry critical register)
   but CG4 independent re-review still required per the closure plan.
6. No shadow/canary/rollback rehearsal and no 72-hour observation.

## To reach GO

- Approve trust root (Sigstore keyless vs offline cosign), sign exact wheel/sdist/image digests.
- Bind and verify the exact candidate image on Linux; record digests.
- Produce target-host reports (scheduler, egress, WAL/durability, second-operator restore).
- Collect 4 written approvals, then shadow to canary to 72h CONTROLLED_PRODUCTION_OBSERVATION.
- Get CG4 independent re-review closing F01-F12 against the new proofs.

Until then: supervised non-production chat only, per the standing status.
