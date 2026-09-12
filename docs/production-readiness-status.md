# Production-readiness status — NO-GO

**As of:** 2026-09-07 (Asia/Jakarta)<br>
**Release state:** **NO-GO — supervised non-production pilot only**<br>
**Controlling backlog:** [PRODUCTION_READINESS_CLOSURE_IMPLEMENTATION_PLAN.md](../PRODUCTION_READINESS_CLOSURE_IMPLEMENTATION_PLAN.md)<br>
**Local evidence:** [production-readiness/local-2026-09-07/index.json](evidence/production-readiness/local-2026-09-07/index.json)

This is an implementation checkpoint, not a closure certificate. Code, tests, and local tool output are implementer evidence. A finding remains open until its required independent review, exact-candidate evidence, target checks, and approval are recorded in the controlling plan.

## 2026-09-09 delta (main @ b6d8a55)

- Local suite re-verified on this checkout: 602 tests green (`skipped=1`); Ruff and strict mypy clean. The only change since that run is CHANGELOG-only, and the full CI matrix re-ran green on it.
- PR #13 merged: deterministic CycloneDX 1.5 SBOM generator from the hash-locked build/dev inputs; provenance `--require-sbom` attach/verify that fails closed; the signature gate is unchanged and still fail-closed with no trust root.
- PR #14 merged: CHANGELOG records the PR #12/PR #13 hardening; the release decision is unchanged.
- Remote CI is green on protected `main` (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14) with a GitGuardian pass.
- Unchanged blockers: signature trust-root decision, exact-candidate Docker/target evidence, target-host reports, human approvals, shadow/canary/rollback rehearsal, and 72-hour observation. Decision stays **NO-GO**.

## 2026-09-09 update (main @ 447d3e9)

- Build-context probe evidence: the CI `quality` job runs `scripts/verify-build-context.py --require-docker` on ubuntu runners, which exits 2 (failing the job) when Docker is unavailable. Post-merge run `34301017230` logs `build-context sentinel passed; actual Docker context/layer/history/runtime verification passed`, i.e. the disposable positive-control canary was detected and negative controls excluded on that build. Only `CHANGELOG.md` changed since, so the result carries over. This does not replace exact-candidate binding (F-024/C6.5) or target evidence; local Windows verification stays `blocked` with no Linux daemon.
- PR #16 merged: per-build SBOM/provenance enforcement in the `package` job plus the `datetime.now(UTC)` default-timestamp fix with regression coverage. PR #17 merged: changelog follow-up. Decision stays **NO-GO**.

## 2026-09-09 update (main @ c72bf8c)

- Post-merge run 34301754577 (PR #18) completed success on protected main (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs; worktree clean.
- Local re-verification on this checkout: 603 tests green across 16 batches (skipped=1 in one batch); Ruff and strict mypy (22 modules) clean. No source changes since the green CI build; only this ledger update follows.
- Unchanged blockers: signature trust-root decision, exact-candidate Docker/target evidence, target-host reports, human approvals, shadow/canary/rollback rehearsal, and 72-hour observation. Decision stays **NO-GO**.

## 2026-09-09 update (main @ 997031f, PR #21 open)

- Post-merge CI green through PR #20 (run 34303202935 completed success). PR #20 merged a PROPOSED signature trust-root draft (Sigstore keyless recommended vs offline cosign) with the require-signature gate unchanged and fail-closed.
- Secret-hygiene sweep: no literal tokens or keys in the tree; Group 5 canary stays split; only the .env.example template.
- Adversarial spot-probes needed no code change: numeric-IP URL forms fail closed at the resolution layer with pinned connect, DTD/entity input is rejected as xml_dtd_disallowed across utf-8/16/32, Telegram uses the literal middot.
- New C4.5 proof in this PR (tests/test_c45_determinism.py): all 24 merge permutations agree on fingerprints and stats, caller-owned items are unmutated and unaliased, and the proof passes in subprocesses under PYTHONHASHSEED 0 and 42.
- Decision stays NO-GO.

## 2026-09-09 update (main @ 7da4c19, PR #22 open)

- PR #21 merged to protected main (merge 7da4c19); post-merge run 34303969200 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs after the merge; worktree clean.
- New C4.4 proof in this PR (tests/test_c44_worker_isolation.py): real spawn-context workers run through _source_process_entry and _terminate_worker. A fast worker returns a typed SourceResult frame and exits 0; a 60s hung worker is terminated and reaped with no survivors under mp.active_children(); the next spawn succeeds. This closes the real-process gap left by the fake-process supervisor corpus; no behavior change.
- Decision stays NO-GO.

## 2026-09-10 update (main @ 0eec0e5, PR #23 open)

- PR #22 merged to protected main (merge 0eec0e5); post-merge run 34304755480 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs after the merge; worktree clean.
- New C4.2 proof in this PR (tests/test_c42_numeric_ip_forms.py): obfuscated 127.0.0.1 spellings (decimal 2130706433, hex 0x7f000001, octal 0177.0.0.1 and variants) are locked in hermetically. The classifier fails closed on every unparseable form and the resolution layer rejects a glibc-style 127.0.0.1 answer with ssrf_address_class for each form under stubbed getaddrinfo, with a public-answer positive control. This converts the prior disposable probe sentence into a checked-in regression; no behavior change.
- Decision stays NO-GO.

## 2026-09-10 update (main @ ef36379, PR #24 open)

- PR #23 merged to protected main (merge ef36379); post-merge run 34475045879 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs after the merge; worktree clean.
- Local re-verification on this checkout: full pytest run green, 609 tests (608 passed, 1 skipped, zero failures); Ruff and strict mypy (22 modules) clean. No source changes since the green CI build; only this ledger update follows.
- Decision stays NO-GO.

## 2026-09-10 update (main @ c91789b, PR #25 open)

- PR #24 merged to protected main (merge c91789b); post-merge run 34475802001 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs after the merge; worktree clean.
- New C4.3 proof in this PR (tests/test_c43_dtd_encodings.py): DOCTYPE payloads must raise xml_dtd_disallowed across 8 encodings (utf-8, utf-8-sig, utf-16/16-le/16-be, utf-32/32-le/32-be), entity-only payloads without DOCTYPE are rejected in a 3-encoding sample, and a healthy feed still parses. Converting the prior disposable probe sentence into a checked-in regression exposed a real ordering defect: C4.1 UTF-8 sanitization (errors="replace") ran before the DTD scan and misaligned the BOM-prefixed UTF-32 byte stream, so that case surfaced xml_parse_error instead of xml_dtd_disallowed. The scan block in _parse_xml_once now runs on the raw payload before sanitization; the payload was still fail-closed throughout (the stdlib parser never fetched external entities). New tests 3/3 green; neighbors (collector coverage, hostile-red, C4.2, C4.4) 27 passed; ruff check and mypy clean on touched files. Follow-up: the +2 line shift stale-pinned the critical-branch register (collectors.py 625, now an except line), failing the coverage gate fail-closed as designed; re-pinned to 627, the same bounded-frame branch, verified locally as a measured arc origin under its test.
- Decision stays NO-GO.

## 2026-09-10 update (main @ 3cf2072, PR #26 open)

- PR #25 merged to protected main (merge 3cf2072); post-merge run 34477168065 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs after the merge; worktree clean.
- Local re-verification on this checkout: full pytest run green, 612 tests (611 passed, 1 skipped, zero failures); ruff check and mypy clean on touched files. The coverage gate failed closed mid-PR on a stale register pin (collectors.py 625) after the C4.3 +2 line shift and passed after re-pinning to the same bounded-frame branch at 627. No source changes since the green CI build; only this ledger update follows.
- Decision stays NO-GO.

## 2026-09-10 update (main @ df8ac9e, PR #27 open)

- PR #26 merged to protected main (merge df8ac9e); post-merge run 34478574014 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian; one windows py3.14 job stalled ~15 min in its test step then recovered green on the unchanged commit, an infra flake). No open PRs after the merge; worktree clean.
- Local re-verification on this checkout: full pytest run green, 612 tests (611 passed, 1 skipped, zero failures). No source changes since the green CI build; only this ledger update follows.
- Decision stays NO-GO.

## 2026-09-10 update (main @ 6dd99fc, PR #28 open)

- PR #27 merged to protected main (merge 6dd99fc); post-merge run 34479713900 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs after the merge; worktree clean.
- New C4.1 proof in this PR (tests/test_c41_unicode_scalar.py): bidi overrides/isolates/marks are stripped from title/summary output, lone surrogates in title/link/summary quarantine invalid_unicode_scalar with the healthy sibling surviving, emoji/astral and combining marks are preserved, C0/C1 controls are stripped, and illegal-XML controls fail the document closed. Converting the probe into a checked-in regression exposed a real gap: bidi controls (U+202E et al.) passed into stored output; _CONTROL_RE now also strips U+200E/200F, U+202A-202E and U+2066-2069 (U+061C deliberately kept as legitimate text). New tests 5/5 green; neighbors (collector coverage, hostile-red, C4.3 DTD) 26 passed; ruff check and mypy clean on touched files. Follow-up: the +3 line shift stale-pinned the register (627, now a statement) and the shipped-register unit test plus the coverage gate failed closed as designed; re-pinned to 630, the same bounded-frame branch.
- Decision stays NO-GO.

## 2026-09-10 update (main @ 4cc6232, PR #29 open)

- PR #28 merged to protected main (merge 4cc6232); post-merge run 34481004592 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs after the merge; worktree clean.
- Local re-verification on this checkout: full pytest run green, 617 tests (616 passed, 1 skipped, zero failures). The C4.1 +3 line shift stale-pinned the register mid-PR and the shipped-register unit test plus the coverage gate failed closed as designed before the 627 to 630 re-pin. No source changes since the green CI build; only this ledger update follows.
- Decision stays NO-GO.

## 2026-09-10 update (main @ d75d6e7, PR #31 open)

- PR #30 merged to protected main (merge d75d6e7); post-merge run 34483151867 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs after the merge; worktree clean.
- New C4.6 proof in the merged PR (tests/test_c46_omitted_history.py): an oversized digest-omitted item between two healthy siblings flows through build_digest into prepare_delivery; after every outbox chunk is accepted, the omitted fingerprint is absent from delivery_items, outbox payloads, and article_history while the included fingerprints map exactly and the delivery completes. No production code changed, so no register re-pin was needed.
- Local re-verification on this checkout: full pytest run green, 618 tests (617 passed, 1 skipped, zero failures); ruff check and strict mypy (22 modules) clean. No source changes since the green CI build; only this ledger update follows.
- Decision stays NO-GO.

## 2026-09-10 update (main @ e9f8558, PR #32 open)

- New C4.7 checker proof in this PR: scripts/critical-branches.json grows from 8 to 16 entries so the reviewed gate enforces every C4.7 decision set (Unicode scalar/control, URL/DNS/redirect, XML DTD/entity, MemoryError, worker termination, identity migration, merge/fuzzy budgets, Telegram omission/sizing). Measured on the full-suite coverage run for this tree, the gate reports statement 93.124% (floor 90), branch 90.977% (floor 90), 16/16 critical branches, overall passed; every new entry has both outgoing arcs executed with none missing. Publisher classification is branchless by design (total-order sort key) and stays outcome-proven by the C4.5 direct-over-aggregator proof, as documented in the closure report.
- New docs/reviews/2026-09-10-application-security-closure.md replays every CG4 probe as a checked-in regression (23 passed across the six probe files on this checkout) and records the decision-set table with residuals. No production code changed.
- Decision stays NO-GO.

## 2026-09-10 update (main @ 3af0757, PR #33 open)

- PR #32 merged to protected main (merge 3af0757); post-merge run 34485376412 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs after the merge; worktree clean.
- Local re-verification on this checkout: full pytest run green, 618 tests (617 passed, 1 skipped, zero failures); ruff check and strict mypy (22 modules) clean. No source changes since the green CI build; only this ledger update follows.
- Decision stays NO-GO.

## 2026-09-10 update (main @ c9cb26a, PR #34 open)

- New C5.1 command-terminal proof in this PR: `meco_news/app.py` adds `_finish_command` plus one `command` attempt lifecycle per operator/query mode (`config_show`, `preflight`, `preflight_online`, `status`, `healthcheck`, `metrics`, `backup`, `restore`, `resolve_chunk`, `migrate`, `test_telegram`, `discover_chat`) so every command path emits exactly one `attempt_terminal` record with its exit code; delivery/daemon paths keep their own records.
- New `tests/test_c51_command_terminal.py` (13 tests) asserts exactly one terminal record per command path with the expected outcome, machine-mode stdout stays exactly one JSON document, and covers config/preflight/status/metrics/healthcheck, backup/restore roundtrip plus failures, resolve success/failure, migrate applied/failed/unsupported, and telegram placeholder/mocked success/empty/failure cases.
- `docs/monitoring.md` documents the command `attempt_terminal` outcomes and the retention floor (attempts/source observations >=90 days, article identity >=365 days; unresolved work never pruned).
- Local verification on this checkout: full pytest green, 631 tests (630 passed, 1 skipped, zero failures); `ruff check meco_news tests scripts` clean; strict `mypy meco_news` clean (22 modules).
- Decision stays NO-GO.

## 2026-09-10 update (main @ 6269d9f, PR #35 open)

- PR #34 merged to protected main (merge 6269d9f); post-merge run 34488499738 completed success (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian). No open PRs after the merge; worktree clean.
- Local re-verification on this checkout: full pytest run green, 631 tests (630 passed, 1 skipped, zero failures); ruff check and strict mypy (22 modules) clean. No source changes since the green CI build; only this ledger update follows.
- Decision stays NO-GO.

## 2026-09-11 update (main @ 952a72a, owner approvals)

- Owner approvals recorded in chat 2026-09-11 (Asia/Jakarta): signature trust root Option A (Sigstore keyless), RPO 24h, RTO 60m, independent Telegram alert channel. `docs/decisions/signature-trust-root.md` signature block completed (status APPROVED, wiring pending); ADR-C13/C15/C16 index entries updated to the approved values with closure-pending evidence notes.
- Decision-record hygiene on this checkout (no branches): `.github/CODEOWNERS` approver transcribed from signed ADR-C03/D2; ADR-C18/C19/C20 index entries added; stale code anchors in C07/C08/C09/D6/D7/D8/D9/D10/D11 corrected to verified locations; ADR-C04 schema note corrected to v5/14 tables/7 indexes; unapproved RPO/RTO numbers removed in favor of the now-approved values.
- Gates re-ran green on this checkout: `ruff check meco_news tests scripts` clean, strict `mypy meco_news` clean (22 modules), spot suites green. No production code changed.
- Still open: trust-root CI wiring PR (CG6), timed second-operator restore drill against approved RPO/RTO (CG5), alert-channel wiring plus firing/recovery receipts (CG5), independent re-review packets (CG1-CG5), exact-candidate container proof, target-host reports, CG0 gate approval, shadow/canary/rollback rehearsal, 72-hour observation. Decision stays **NO-GO**.

## 2026-09-11 update (owner confirmations, phase 0 close-out answers)

- Owner confirmations recorded in chat 2026-09-11 (Asia/Jakarta): business approver Evan Widjaja; restore-drill second operator Evan Widjaja; off-host backup store Drive (encryption method TBD, recorded as an open item); escalation owner Evan Widjaja (alert-chat identity TBD at wiring); HMAC key in owner custody (rotation rule TBD); lease/retry values confirmed as-is (ADR-C06/C08 marked decided, closure pending independent review); gate-waiver authority Evan Widjaja (documented condition required per waiver).
- `docs/decisions/adr-index.json` updated: D2 decision/status carry the business approver; ADR-C03 status notes the addition; ADR-C06/C08 marked decided (owner-confirmed, review-pending); ADR-C09 notes owner HMAC custody with rotation TBD; ADR-C13/C15/C16 carry the approved RPO/RTO/channel/trust-root values with evidence-pending closure notes; D4 status corrected (digest pin verified present, final-image binding still CG6).
- `.github/CODEOWNERS` carries the business-approver line; `docs/decisions/signature-trust-root.md` stays APPROVED Option A (wiring pending).
- Still open (owner/reviewer action, nothing invented): Drive encryption method, HMAC rotation rule, alert-chat identity and wiring, CG0 gate pass, Phase 1 independent re-review. Decision stays **NO-GO**.
## 2026-09-11 update (phase 0 close-out, CG0 pass)

- Owner close-out answers recorded in chat 2026-09-11 (Asia/Jakarta): off-host encryption is Google-managed at-rest only (accepted for ADR-C13, no owner-managed layer); HMAC rotation every 90 days (ADR-C09 rule); independent alert channel is the MecoNews channel, named as the wiring target (ADR-C15; wiring plus firing/recovery receipts still pending); Phase 0 decision freeze approved as the CG0 baseline; Phase 1 re-review will be done by the owner alone (Evan Widjaja) - an accepted deviation from the different-reviewer rule, independence limited.
- `docs/decisions/adr-index.json` updated: ADR-C09 carries the 90-day rotation rule; ADR-C13 carries the Google-only encryption acceptance; ADR-C15 names the MecoNews channel.
- CG0 checklist against PRODUCTION_READINESS_CLOSURE_IMPLEMENTATION_PLAN.md: authoritative source recorded (D1); next-wave decisions approved (owner yes 2026-09-11); R01-R20/Section 7.2 linkage per the status register; C0.4 safety accepted under the owner-alone review model; status honestly NO-GO; credential-pattern scan of `meco_news`, `tests`, `config`, `scripts` clean on this checkout.
- Phase 0 is CLOSED on this checkout (roadmap Phase 0 done-criteria met: owner-signed decision block plus approval matrix entries exist out of band and in the evidence index). Still open: trust-root CI wiring (CG6), timed restore drill (CG5), alert wiring/receipts (CG5), Phase 1 self-review packets, exact-candidate container proof, target-host reports, shadow/canary/rollback rehearsal, 72-hour observation. Decision stays **NO-GO**.
## 2026-09-11 update (phase 1 local re-review, CG1-CG5 pass)

- Owner self-review (Evan Widjaja, accepted different-reviewer deviation) on main at 952a72a, uncommitted work only. Full suite 641 passed, 1 skipped, 0 failed in 125.88s; statements 6133/6589 (93.079 pct), branches 1968/2164 (90.943 pct), critical register 16/16; reviewer probes RP1-RP5 all PASS; ruff and strict mypy clean.
- Findings fixed with checked-in regressions: F01 fresh-target restore quarantine (backup.py), F-C4.1 C0 neutralization (collectors.py), F03 reconcile_delivery resume path with CLI flag (storage.py, app.py), F04 critical-register re-pin (critical-branches.json), F05 durable marker publish retry under 50-process load (maintenance.py).
- Records: docs/evidence/production-readiness/local-2026-09-11-phase1/REVIEW-PHASE1.md, verification.json, commands.txt. Roadmap Phase 1 marked COMPLETE for local re-review. Still open: signing and SBOM wiring (CG6), exact-candidate container proof, target-host reports with second operator (CG5/CG6), rollout and 72-hour observation (CG7). Decision stays NO-GO.
## 2026-09-11 update (phase 2 signing/SBOM wiring, CG6 local)

- Phase 2 wired on main at 952a72a, uncommitted work only. `scripts/release-provenance.py` binds `--signature-bundle ARTIFACT=BUNDLE` per artifact/SBOM and verifies each bundle against the pinned OIDC identity (Sigstore keyless, Option A). The CI `package` job holds the only `id-token: write` grant, installs dev plus build locks, signs wheel/sdist/`dist/sbom.json` with the pinned sigstore, and verifies with `--require-sbom --require-signature`.
- Tests: new `tests/test_release_signature.py` (17 tests: bind/pass, tamper to mismatch, deleted to missing, SBOM-without-bundle to missing, unknown/malformed CLI, unavailable/rejected/timeout verifier, unbound-create and duplicate-resolved-bundle ValueError) green; `tests/test_sbom_generation.py` merged count re-pinned 16 to 46 with PEP 503 name normalization plus a sigstore excluded-scope assertion (build lock now carries sigstore 4.5.0 plus transitive deps). Full pytest 658 passed plus 1 skipped, full unittest 659 OK (skipped=1); ruff plus strict mypy clean on this checkout.
- Local fail-closed proofs on the exact candidate build: unsigned `--verify --require-sbom` passes while `--require-signature` fails with `signature:not_signed`; dummy bundles fail with `signature:verifier_unavailable` (no signer/Rekor outside CI). Honesty note: the Rekor/OIDC positive control runs only in CI, so CG6 closes on the first green `package` run on protected `main`, not on local evidence.
- Records: docs/evidence/production-readiness/local-2026-09-11-phase2/REVIEW-PHASE2.md, verification.json, commands.txt, plus final suite logs. Decision stays **NO-GO**.
## 2026-09-12 update (PR #36 merged, CG6 Rekor positive control on main)

- PR #36 merged to main @ 6027e64: Phase 2 Sigstore keyless signing/SBOM wiring plus the workflow-ref identity fix. Post-merge run 34682764576 completed success on protected main (quality, package, container, coverage-and-critical-branches, unit ubuntu+windows py3.12-3.14, GitGuardian).
- CG6 Rekor/OIDC positive control: the main `package` job signed wheel, sdist, and `dist/sbom.json` with Fulcio ephemeral certificates bound to `https://github.com/evanwidjaja100/meco-news/.github/workflows/ci.yml@refs/heads/main` and verified with `--require-sbom --require-signature` (`"passed": true, "failures": []`, SBOM attached).
- Deadlock found and fixed from the first PR run: the pinned-main verify identity could never match branch-run signatures, so `package` failed closed on every branch while policy required it green before merge. CI now records and verifies the signing run's own `github.workflow_ref` identity (repo/workflow/ref-scoped; foreign identities rejected); the default pin stays `refs/heads/main`, which is the only identity that counts for promotion.
- Still open: exact-candidate container proof, target-host reports with second operator (CG5/CG6), timed restore drill and alert wiring/firing/recovery receipts (CG5), shadow/canary/rollback rehearsal and 72-hour observation (CG7). Decision stays **NO-GO**.
## Verified local facts

- 509 tests collected; the fresh full `pytest` suite passed.
- Statement coverage: 5,749 / 6,147 (93.525%). Branch coverage: 1,829 / 2,032 (90.010%). The critical-branch register passed: 8 registered, 0 missing, 0 unknown.
- Ruff passed for `meco_news`, `tests`, and `scripts`; mypy strict passed for 22 source modules.
- A wheel and sdist built successfully. Both were installed into clean temporary environments outside the checkout; each CLI smoke test imported from the temporary environment and passed.
- The safe synthetic build-context tests passed. `.dockerignore` lint passed, but actual Docker context/image/layer/history/runtime verification is **blocked** because the Docker Desktop Linux daemon is unavailable on this host. No fallback success is claimed.
- The migration, backup, restore, maintenance-lock, WAL-read, authority, Telegram-envelope, hostile-input, bounded-worker, scheduler, metrics, alert, and CLI regression suites use disposable fixtures. No production Telegram request, live scheduler installation, production database, or real credential was used.

## R01–R20 implementation status

The locally implementable behavior and regression coverage are present. All rows remain open for formal closure until the evidence and review requirements in Section 7.1 are satisfied.

| ID | Local checkpoint | Remaining closure dependency |
|---|---|---|
| R01 | Ack-failure uncertainty and orphan recovery paths implemented and tested | Independent fault/restart replay review; target durability evidence |
| R02 | WAL-aware read paths and unavailable/fail-closed classification implemented and tested | Target filesystem/WAL evidence |
| R03 | Shared/exclusive OS guard, process identity, and maintenance fencing implemented and tested | Cross-platform contention and target lock evidence |
| R04 | Verified backup/restore, sidecar handling, unresolved-work refusal, and post-backup reconciliation implemented and tested | Restore fault matrix, second operator, target RPO/RTO evidence |
| R05 | FULL synchronous policy, intent-before-send, and failure classification implemented and tested | Host power-loss/storage durability evidence |
| R06 | Persisted retry attempt/elapsed budgets and terminal exhaustion implemented and tested | Full target scheduler/date-transition replay evidence |
| R07 | Transaction-local lease/fence/owner checks and chunk/run/mapping binding implemented and tested | Independent mutator inventory review |
| R08 | Strict Telegram response-envelope and destination validation implemented and tested | Independent transport review |
| R09 | Truthful preflight, health, status, WAL probe, and exit precedence implemented and tested | Target health/alert observation |
| R10 | Disposable context verifier, canaries, positive leak control, and explicit Docker blocking implemented and tested | Real Docker inspection on candidate image |
| R11 | Validated version-1 frozen-input dry-run implemented and tested offline | Independent subprocess/no-side-effect review |
| R12 | Frozen delivery payload/config provenance and audited destination mismatch handling implemented and tested | End-to-end target destination-change review |
| R13 | Atomic backup reservation, manifest-last publication, missing-source refusal, and collision tests implemented | Scheduled/off-host backup and retention evidence |
| R14 | Source-document shape validation and per-source quarantine implemented and tested | Target feed corpus/review |
| R15 | Bounded deterministic fuzzy dedup with exhaustion-safe output implemented and tested | Independent adversarial corpus review |
| R16 | HTML validation, escaping, continuation sizing, limits, and sibling retention implemented and tested | Live API contract verification |
| R17 | Report-mode stdout isolation and stderr diagnostics implemented and tested | Subprocess matrix on candidate artifact |
| R18 | Recursive redaction for messages, fields, exceptions, worker output, and sinks implemented and tested | Security review and sink inspection |
| R19 | Framed bounded worker IPC, per-source/cycle deadlines, termination, and reaping implemented and tested | Target process/shutdown evidence |
| R20 | Typed scheduler outcomes, config revalidation, lease heartbeats, stop handling, and bounded shutdown implemented and tested | Target scheduler registration and live stop/restart evidence |

## Section 7.2 and release gates still open

- F-016 egress proof still needs controlled target endpoints and positive target firewall/egress evidence; application DNS pinning and proxy-disabled behavior are not a substitute for target proof.
- F-023 needs target-specific process identity/liveness evidence, including access-denied and PID-reuse cases.
- F-025/F-026 need remote CI evidence for the declared 3.12–3.14 matrix, transitive lock review, signed provenance, SBOM, and an exact candidate digest.
- F-021/F-022/F-027 need an independent alert channel, scheduled/off-host backups, retention/prune receipts, second-operator restore, and target-platform evidence.
- CG0–CG7 still require authoritative provenance/decisions, non-author review, protected source, exact-candidate Docker/target checks, human release approvals, shadow/canary/rollback authorization, and the 72-hour observation record.

Until RA-P-authorized cutover, operate only in a supervised non-production chat. Even after cutover, the permitted posture is `CONTROLLED_PRODUCTION_OBSERVATION` until CG7 passes.
