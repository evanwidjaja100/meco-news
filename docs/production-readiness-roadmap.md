# Roadmap to production-ready - 2026-09-11

Current state: NO-GO, supervised pilot only (head 952a72a, v2.0.0).
Controlling contract: PRODUCTION_READINESS_CLOSURE_IMPLEMENTATION_PLAN.md (gates CG0-CG7, RA-S/C/P).
This file is the practical build order. Nothing here waives a gate or authorizes cutover early.

## Phase 0 - Freeze decisions (CG0/G0, owner required)

1. Name the release approver and domain reviewers (replace TBD in .github/CODEOWNERS).
2. Sign docs/decisions/signature-trust-root.md: pick Option A (Sigstore keyless, recommended)
   or Option B (offline cosign). Until signed plus wiring PR merged, --require-signature stays red by design.
3. Record RPO/RTO targets, target hosts (Linux/NAS plus Windows scheduler), independent alert channel,
   backup retention and off-host destination, shadow/canary thresholds and stop rules.
4. Done when: owner-signed decision block plus approval matrix entries exist out of band and in the evidence index.
5. Status 2026-09-11: CLOSED - owner-signed Option A block plus CODEOWNERS approval matrix (owner/release/business: Evan Widjaja) exist out of band and in docs/decisions/adr-index.json; RPO 24h/RTO 60m, Drive off-host (Google-only encryption accepted), MecoNews alert channel, 90-day HMAC rotation recorded. CG0 checklist in docs/production-readiness-status.md.

## Phase 1 - Independent re-review (CG1-CG5, different reviewer per packet)
Reviewer 2026-09-11: owner self-review (Evan Widjaja) - accepted deviation from the different-reviewer rule; independence limited, recorded in docs/production-readiness-status.md.


1. A reviewer who did not write the fix replays every Sep-07 probe (F01/F02 restore replay and crash,
   lease expiry, backup lock, worker redaction, coverage gate, signature) plus the R01-R20 and C4.x corpus
   against the current tree, attempting counterexamples.
2. Commands (provisioned interpreter, disposable dirs only):
   .venv/Scripts/python.exe -m ruff check meco_news tests scripts
   .venv/Scripts/python.exe -m mypy meco_news
   .venv/Scripts/python.exe -B -m pytest -q (full suite after C0.4 safety, about 143 s)
   .venv/Scripts/python.exe -m coverage run --branch --source=meco_news -m pytest
   .venv/Scripts/python.exe scripts/coverage_gate.py --coverage-json coverage.json --critical-branches scripts/critical-branches.json
3. Done when: statement and branch coverage each >= 90, critical register 16/16, every probe passes,
   reviewer signs CG1-CG5 records. Any failure reopens its finding.
4. Status 2026-09-11: COMPLETE (local re-review) - 641 passed 1 skipped, statements 93.079 pct, branches 90.943 pct, register 16/16, RP1-RP5 pass, ruff and strict mypy clean; findings F01, F-C4.1, F03, F04, F05 fixed with regressions; records in docs/evidence/production-readiness/local-2026-09-11-phase1/REVIEW-PHASE1.md, verification.json, commands.txt. Target-host and second-operator evidence stays open for Phase 4. Decision stays NO-GO.

## Phase 2 - Wire signing and SBOM (CG6)

1. Option A: add minimal id-token:write to the package job, sign wheel plus sdist plus dist/sbom.json
   with sigstore-python (pin its hash in requirements-build.lock), log to Rekor, pin expected identity
   (repo URI, workflow path, ref) plus Rekor entry out of band. Keep self-asserted signed state rejected.
2. Per build, run:
   python scripts/generate-sbom.py --root . --build-lock requirements-build.lock --dev-lock requirements-dev.lock --output dist/sbom.json
   python scripts/release-provenance.py --root . --output dist/provenance.json --artifact <wheel> --artifact <sdist> --sbom-report dist/sbom.json
   python scripts/release-provenance.py --output dist/provenance.json --verify --require-sbom --require-signature
3. Done when: --verify with both flags passes on the exact candidate artifacts.
4. Status 2026-09-11: WIRED (local, uncommitted) - provenance binds `--signature-bundle` per artifact/SBOM and verifies against the pinned OIDC identity; `tests/test_release_signature.py` (17 tests) plus existing group-8 gates green; local `--verify --require-sbom --require-signature` fails closed (`signature:not_signed`, dummy bundles `signature:verifier_unavailable`). Honesty note: the Rekor/OIDC positive control runs only in the CI `package` job, so Phase 2 closes on the first green run on protected `main`, not on local evidence.

## Phase 3 - Exact-candidate container proof (CG6, Linux with daemon)

1. Build once, record digests, never rebuild between stages:
   docker build --build-arg PYTHON_IMAGE=<pinned-digest> -t meco-news:<candidate> .
   python scripts/verify-build-context.py --require-docker
   plus layer/history/runtime canary verification and multi-arch smoke.
2. Done when: base digest, final image digest, SBOM/provenance hashes bound in the release record;
   the promoted digest is this exact image, not a retag or rebuild.
3. Status 2026-09-12: COMPLETE (local) - sentinel passed on Docker Desktop Linux daemon; candidate `meco-news:phase3-20260912` (image `sha256:ff74ac361f241fef7510f3b04c65b04190618cecc7b07fd53b215edc13c06a89`) built once from pinned base `sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f`; runtime smoke passed; single-arch amd64 per owner Intel target; evidence in docs/evidence/production-readiness/local-2026-09-12-phase3/. Promotion must ship this exact digest.

## Phase 4 - Target-host validation (CG5/CG6, second operator)

1. On each target (Linux/NAS and Windows): identity, storage ACL/mode, WAL behavior, egress block with
   app checks bypassed, scheduler install plus signal/stop/restart, backup plus second-operator restore
   meeting RPO/RTO, runbook rehearsal (rollback, disk-full, corruption, no-delivery).
2. Suggested: python scripts/target-evidence.py (review first), docker compose config, Task Scheduler
   XML plus PSScriptAnalyzer/Pester where applicable.
3. Done when: per-host reports plus second-operator receipts are in the evidence index.

## Phase 5 - Rollout (RA-S, RA-C, RA-P, CG7)

1. RA-S shadow: exact digest, separate state and sink (never the prod chat), thresholds plus owners approved.
2. RA-C canary: shadow accepted first; scoped canary with expiry, fault drills, rollback rehearsed.
3. RA-P cutover: disable old schedulers, confirm no active lease, verify online backup, preflight plus
   dry-run, migrate once, enable exactly one scheduler. Posture becomes CONTROLLED_PRODUCTION_OBSERVATION.
4. CG7: clean 72-hour observation, rollback kept ready, same digest throughout.
5. Done when: shadow/canary/rollback/cutover/72h evidence signed by business, operations, security,
   release. Only then does status become production-ready.

## Stop rules (from the closure plan)

Stop and reopen on: uncertain schema/provenance, unverifiable pre-migration or pre-restore backup,
ambiguous delivery state, falsely green health, missing target evidence, or any failed later gate.

## Fast status check (anytime, this checkout)

.venv/Scripts/python.exe -m ruff check meco_news tests scripts
.venv/Scripts/python.exe -m mypy meco_news
.venv/Scripts/python.exe -B -m meco_news --config-show --json
.venv/Scripts/python.exe -B -m meco_news --preflight --json
.venv/Scripts/python.exe -B -m meco_news --dry-run --frozen-input tests/fixtures/frozen-empty-v1.json
