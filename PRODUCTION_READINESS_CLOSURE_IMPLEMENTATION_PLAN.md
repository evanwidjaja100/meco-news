# MECO News Scraper — Production Readiness Closure Implementation Plan

Status: **Active remediation plan — all closure gates open**  
Created from implementation audit: **2026-08-24**; revised after repository review: **2026-09-06**\
Predecessor contract: [PRODUCTION_READINESS_IMPLEMENTATION_PLAN.md](PRODUCTION_READINESS_IMPLEMENTATION_PLAN.md)  
Current audited baseline: **`1e820fc968c3dbb3dfa35904ee01a3131f7dfc70` plus the reviewed working-tree changes**\
Current release decision: **NO-GO — supervised non-production pilot only**

---

## 1. Purpose, authority, and completion rule

This plan closes the gap between the production-readiness design and the code that was reported as fully implemented. The repository contains substantial scaffolding and passing smoke tests, but the implementation audit reproduced correctness, security, recovery, observability, and release-control failures.

The predecessor plan remains the architectural and acceptance contract. This document is the controlling implementation-audit backlog. It does not replace, waive, or reinterpret an original requirement downward.

The documents are used together:

- the predecessor defines the intended production properties and original waves;
- this plan defines the defects found in the implemented snapshot and the exact closure sequence;
- a closure task may be marked complete only when its original requirement also passes;
- if the documents appear to conflict, the stricter safety invariant applies until the coordinator records an explicit amendment in both documents;
- code presence, documentation presence, a passing happy-path test, or an implementer's statement is not closure.

Every finding follows this lifecycle:

1. **Open** — audit evidence exists but no checked-in deterministic reproducer is accepted.
2. **Red reproduced** — the defect is captured by a stable failing test or a documented external evidence protocol.
3. **Implemented** — a bounded change makes the focused reproducer pass.
4. **Cumulatively verified** — focused, full offline, static, fault, and applicable platform checks pass.
5. **Independently reviewed** — a different agent replays the original probe and attempts a counterexample.
6. **Externally evidenced** — target-host, release, or rollout evidence exists where the requirement needs it.
7. **Closed** — the coordinator links the evidence manifest, reviewer, decision, and rollback note.

Any regression, missing artifact, unverifiable assertion, expired waiver, incompatible design change, or failed later gate reopens the affected finding automatically. The implementation loop repeats until the finding ledger contains zero open mandatory rows and Closure Gates CG0 through CG7 all pass.

No task in this document authorizes use of production Telegram credentials, a production chat, destructive database migration, release publication, or deployment cutover before its explicit gate and approval.

Release-state progression is explicit: current NO-GO; RA-S permits non-production shadow; RA-C permits non-production canary; RA-P permits only CONTROLLED_PRODUCTION_OBSERVATION; CG7 alone permits a production-ready declaration.

---

## 2. Verified rebaseline — 2026-09-06

The current evidence is [the repository review](docs/reviews/2026-09-06/REVIEW.md), [verification transcript](docs/reviews/2026-09-06/verification.txt), [disposable reproducer](docs/reviews/2026-09-06/reproduce_findings.py), [observations](docs/reviews/2026-09-06/reproductions.json), and [source manifest](docs/reviews/2026-09-06/source-manifest.sha256). The commit alone does not identify the reviewed implementation: the working tree contains existing storage, migration, inspection, architecture, and test changes. Preserve those changes and identify future evidence with both commit and a sanitized working-tree manifest. Plan edits do not close a finding.

| Area | Verified result | Readiness meaning |
|---|---|---|
| Existing tests | Initial run: 379 passed, 1 failed (`test_verify_with_context`); safe repeat: 379 passed, that one test deselected | Preserve both results. The context verifier can overwrite/delete `.env`; repair it in isolation before running the full suite again |
| Coverage | Safe repeat measured 79% combined statement/branch coverage | Below the existing 90% gate. Spawned-process collection is not yet configured; measure subprocess coverage deliberately, never infer that every unmeasured line is untested |
| Static quality | Ruff and mypy passed; mypy checked 19 source files | Positive local evidence; does not prove behavioral safety |
| Packaging | Local no-isolation build could not run because the environment lacked setuptools/wheel backend requirements | Blocked local verification, not proof of a package defect; rerun in a provisioned clean build environment |
| Context verification | Disposable probe confirmed destructive `.env` handling and false-pass fallback paths | R10 is the first executable repair packet; never reproduce against real credentials or the working checkout |
| Docker runtime | CLI present; daemon unavailable and local Docker configuration access restricted | Context/image/runtime proof remains blocked locally, not passed |
| Provenance | Git commit available with pre-existing uncommitted changes | Reconcile authoritative remote/history and release controls; do not repeat the old claim that this directory has no Git repository |
| State and behavior | Schema-v4 working tree; review records R01–R20 and 22 observation groups | Several groups cover one finding. Neither observation count nor test count is a closure metric |
| Platforms and rollout | No live feed/Telegram, target scheduler, Linux runtime, power-loss, shadow/canary, or 72-hour observation validated in this review | Retain external gates and named evidence requirements |

The first implementation priorities are:

- stop duplicate sends after local acknowledgment persistence fails, including recovery with no lease row;
- establish WAL-aware live reads, FULL synchronous durability, real runtime/maintenance exclusion, and transaction-local ownership;
- make backup, migration, and restore preserve unresolved work and external-send uncertainty;
- prevent ordinary invocations from resetting exhausted delivery budgets;
- make Telegram envelopes, health, CLI reports, worker deadlines, and scheduler shutdown truthful and bounded;
- close ingestion/rendering defects, test isolation, artifact verification, and operational evidence gaps.

The 2026-08-24 baseline (23 tests, 59% coverage, imported snapshot) and F-001–F-028 remain historical obligations, not current measurements. Existing closure receipts and status documents must be reconciled against this source, including older schema-v3 and coverage claims. Do not erase past evidence or blanket-close historical findings. All program gates remain open for the current candidate until their evidence is reconciled and affected probes pass.

### 2.1 Contract amendments applied to both plans

These corrections define the implementation target; Section 6 still requires recording operational choices and responsible owners. They do not grant deployment or production-data authority.

1. **Live inspection:** use a WAL-aware read transaction for a running database. `immutable=1` is permitted only for a verified quiescent offline artifact. If a strict no-sidecar environment prevents a correct live read, return unavailable/non-ready or use an explicitly supplied consistent snapshot. Never report stale main-file data as current. Read-only means no application-state mutation; SQLite coordination sidecars are documented and tested. Dry-run retains its stronger zero-write contract by using supplied frozen input and, if requested, an offline history snapshot.
2. **Output:** report-mode `--json` stdout contains exactly one JSON document, with diagnostics on stderr. Normal run/daemon structured lifecycle logs remain JSONL on stdout. No startup log may precede a machine report on stdout.
3. **Frozen delivery:** persist the general configuration digest as provenance, separate from destination identity. Resume uses frozen content and retry policy; a source/ranking-only edit cannot invalidate delivery. Actual bot/chat/thread/endpoint/send-option changes require the audited resolution path.
4. **Durability and restore:** authoritative writers require effective `synchronous=FULL`. Restore must reconcile unresolved work and sends after the recovery point, not merely look for an unexpired lease. Process-crash tests do not certify host power-loss behavior.
5. **Test safety:** canaries, context experiments, restore/migration faults, and synthetic credentials run only in dedicated disposable directories. Do not write a canary over a real `.env`, database, backup, or configuration file. Full-suite execution is conditional on the R10 safety repair.

SQLite's [immutable URI contract](https://www.sqlite.org/uri.html#uriimmutable) and [synchronous behavior](https://www.sqlite.org/pragma.html#pragma_synchronous) support the live-read and durability requirements. These are contract corrections, not evidence that the new implementation already meets them.

### 2.2 Local implementation checkpoint — 2026-09-07

The working tree now contains the locally implementable R01–R20 repairs, focused regression suites, operational scripts, and release documentation. The fresh local verification record is [docs/evidence/production-readiness/local-2026-09-07/index.json](docs/evidence/production-readiness/local-2026-09-07/index.json). It records 509 collected tests, separate statement/branch coverage, Ruff, mypy, wheel/sdist smoke results, and the Docker limitation without treating any of these as independent closure or release approval.

The current implementation evidence is bounded to this Windows checkout and disposable local fixtures. Real Docker image/context inspection is `blocked` because the Docker Desktop Linux daemon is unavailable; Linux/NAS and target Windows scheduler execution, target egress controls, independent alert delivery, power-loss durability, signing/SBOM, protected-source review, human approvals, shadow/canary, rollback rehearsal, and 72-hour observation remain external gates. The release decision therefore remains **NO-GO**.

---

## 3. Scope and non-goals

### 3.1 In scope

- close every implementation-audit finding in Section 7;
- add deterministic regression, property, fault, concurrency, security, and platform tests;
- correct implementation and documentation where they contradict required behavior;
- establish authoritative source and immutable release provenance;
- create auditable operational, restore, target-host, rollout, and approval evidence;
- repeat review and remediation until all gates pass.

### 3.2 Not in scope without a separately approved design change

- claiming mathematically exact-once Telegram delivery;
- making SQLite a multi-region or network-filesystem datastore;
- adding product features unrelated to production readiness;
- silently narrowing supported platforms;
- inventing a license, source history, approval, target-host result, scan result, or rollout record;
- weakening a limit merely to make a hostile fixture pass;
- automatically replaying an ambiguous Telegram request;
- treating dry-run as a substitute for durable shadow or canary execution.

---

## 4. Closure goals

### G0 — Attributable source and decisions

Every change is traceable to an authoritative baseline, approved contract, reviewed change, immutable artifact, and named approver.

Success measures:

- an authoritative remote/history is restored, or the owner signs an imported-snapshot declaration with pre-import provenance explicitly unknown;
- D1–D12 and the additional closure decisions in Section 6 are recorded;
- every finding, task, commit, review, test artifact, and release artifact is cross-linked;
- protected source and signed release tags exist before rollout.

### G1 — Truthful and side-effect-safe control plane

CLI, preflight, status, and health never report success when a mandatory condition fails and never cause an unintended side effect.

Success measures:

- invalid command combinations exit 2 before file logging, state, migration, backup, collection, scheduling, or Telegram initialization;
- dry-run is offline: it makes no remote network call and leaves state, WAL/SHM, logs, status, timestamps, leases, and schedulers unchanged; candidate evaluation uses explicitly supplied frozen local input;
- schema compatibility is exact and fails closed;
- status distinguishes latest terminal delivery from active delivery;
- every mandatory failed check produces ready=false or healthy=false and a nonzero exit.

### G2 — Authorized, immutable, recoverable state

Every runtime mutation is authorized inside its transaction, completed evidence remains immutable, and migration/restore never exposes a mixed state.

Success measures:

- all runtime mutators require a live lease capability and current maintenance fence checked in the same write transaction;
- each runtime process holds the shared execution guard for its lifetime; maintenance requires the exclusive guard and a distinct audited capability;
- force requires a prior completion and atomically creates N+1;
- historical migration checksums do not change when a future migration is added;
- legacy writers are fenced from migrated databases;
- migration and restore fault matrices yield either the verified old state or the verified new state, never a partial hybrid.

### G3 — Ambiguity-safe delivery and scheduler recovery

Telegram side effects are classified conservatively, immutable chunks resume safely, and retries are durable and bounded.

Success measures:

- confirmed chunks never auto-replay;
- acceptance-unknown outcomes become ambiguous and block later work;
- retry delay, attempts, and total elapsed time have independent hard caps;
- retry decisions and deadlines survive restart without recomputation drift; a backward wall-clock jump blocks automatic retry rather than extending its budget;
- daemon scheduling consumes typed outcomes, reloads the resolved config path safely, and never loses terminal/attention state.

### G4 — Hostile-input isolation and deterministic content

Untrusted feeds cannot escape resource, URL, parser, Unicode, or output boundaries, and equivalent frozen inputs always produce byte-identical selected content.

Success measures:

- a bad source or item cannot abort healthy-source processing;
- only valid Unicode scalar values cross validation boundaries;
- DTD/entity rejection works across supported encodings before expansion;
- multicast and every other forbidden destination class fail closed at URL, DNS, redirect, and deployment layers;
- source deadlines terminate and reap workers;
- title identity is source-independent, direct publishers beat aggregators, merge ordering is total, and fuzzy work is completely budgeted.

### G5 — Diagnosable, recoverable operations

Operators can see, alert on, back up, restore, and resolve every material state without manual SQL.

Success measures:

- normal lifecycle JSONL logs go to stdout; single-report JSON commands put diagnostics on stderr; both redact every message/field, remove prohibited controls/bidi, and emit exactly one terminal event per attempt kind;
- status/metrics expose stable outcome, source, retry, ambiguity, DB, lease, and dedup signals;
- alerts are tested through an independent channel;
- disk health fails below 1 GiB or 10% free;
- backup retention, restore drills, Linux/NAS, and Windows target gates meet approved RPO/RTO and security requirements.

### G6 — Reproducible, defended release

The same reviewed artifact passes test, security, platform, shadow, canary, and production promotion.

Success measures:

- line and branch coverage is at least 90% overall and 100% for listed critical decision branches;
- Linux and Windows Python matrices, fault/concurrency/migration tests, PowerShell tests, and multi-architecture image tests pass;
- metadata, Python support, license, dependencies, and transitive hash locks are consistent;
- one candidate is built from the protected tag, its exact context/layers/history/runtime are inspected, and only that same digest is signed;
- the protected tag/candidate produces SBOM, checksums, provenance, signature, compatibility manifest, and one immutable candidate digest.

### G7 — Evidence-led rollout and feedback

Rollout progresses only through approved shadow, canary, rollback-rehearsal, cutover, and observation evidence.

Success measures:

- 3–7 production-like shadow days pass on separate state and a recording sink;
- at least three scheduled canary cycles pass on separate credentials;
- rollback is rehearsed on a disposable production-like target;
- explicit shadow, canary, and controlled-production-observation authorizations are signed by business, operations, security, and release approvers;
- cutover verifies one scheduler, a verified backup, exact digest, migration, health, and first delivery;
- the 72-hour observation window meets SLOs with no unresolved incident or alert.

---

## 5. Non-negotiable closure invariants

1. A mandatory readiness check cannot be false while ready=true, healthy=true, or the command exit code is 0.
2. Normal runtime startup never performs an implicit schema migration; migration is an explicit audited maintenance operation.
3. Schema compatibility distinguishes missing, migration-required, compatible, newer-incompatible, malformed, and corrupt states.
4. Historical migration checksums are immutable per migration and independent of later schema additions.
5. Every runtime delivery-state mutation requires a live owner capability and current maintenance fence checked inside the same write transaction.
6. Every runtime process holds a shared execution guard for its lifetime. Migration/restore obtains the exclusive guard only after existing processes drain; every non-runtime mutation also requires a distinct maintenance/operator capability and an audit record.
7. Transactions are never held open across remote calls.
8. Prepared items, rendered chunks, payload hashes, acknowledged attempts, non-secret destination/send-option snapshot, and completed generations are immutable.
9. A confirmed Telegram chunk is never automatically resent.
10. A request whose acceptance is not proven becomes ambiguous and blocks later chunks.
11. Raw HTTP 5xx, malformed responses, and failures after possible transmission are ambiguous unless a valid explicit Telegram rejection proves non-acceptance.
12. Lease expiry or absence does not prove that an in-flight remote request failed; recovery converts orphaned in-flight work to ambiguous atomically under valid authority.
13. Retry delay, retry count, and total elapsed retry time are independently bounded and persisted; backward wall-clock movement cannot extend a budget and blocks automatic retry when elapsed time cannot be trusted.
14. Forced delivery requires an already completed generation, operator, reason, predecessor, and atomic N+1 allocation.
15. Old binaries and legacy tables cannot write a migrated database.
16. Restore requires exclusive process-lifetime maintenance, proved-stopped schedulers/processes, verified compatibility, preserved recoverable state, and reconciliation of all unresolved work and sends after the recovery point. Unverifiable history keeps delivery disabled.
17. Dry-run makes no remote network call and does not create or mutate state, WAL/SHM, lease, migration, log file, status file, backup, scheduler, or Telegram client. Candidate evaluation uses explicitly supplied frozen local input.
18. Every untrusted identity field contains valid Unicode scalar values before hashing, encoding, persistence, or rendering.
19. One malformed item is quarantined; one failed source degrades coverage but cannot terminate healthy-source processing.
20. XML DTD/entity rejection is parser-level and encoding-independent; MemoryError is never converted into ordinary bad-feed data.
21. Every source has a parent-owned monotonic deadline and a killable, reapable isolation boundary.
22. URL validation covers syntax, canonicalization, IDNA, all DNS answers, every redirect, IPv4/IPv6 mapped forms, and all non-global or explicitly forbidden address classes including multicast.
23. Deployment egress controls remain effective if application URL validation is bypassed.
24. Content identity is source-independent; merge and ranking use immutable inputs and a documented total order.
25. All fuzzy work is counted before shortcuts and bounded by postings, candidate-pair, similarity-call, per-item, and global budgets.
26. Every final Telegram payload is valid UTF-8/scalar text, escaped HTML, bounded by raw bytes and UTF-16 units, and mapped back to delivered or omitted items.
27. Normal lifecycle logs are stdout JSONL; report-mode JSON stdout is one document with diagnostics on stderr. Every stream uses stable schemas/reasons, redacts messages and fields, and has exactly one terminal event per attempt kind.
28. Latest terminal delivery and active delivery are separate status concepts.
29. Backups, manifests, evidence, and diagnostics never contain secrets or raw hostile payloads.
30. The exact build-once candidate is context/layer/runtime inspected before signing; shadow, canary, controlled production observation, and production-ready promotion use that same signed digest.
31. No high or critical security finding is silently waived.
32. Missing evidence means a failed gate.

---

## 6. Decisions and design records to freeze in Closure Wave 0

The coordinator must record the predecessor decisions D1–D12 and the following closure records before dependent implementation is frozen. Recommended defaults may guide discussion, but ownership, license, deployed schema history, platform support, and approvals must not be invented.

| Record | Required decision | Blocking dependency |
|---|---|---|
| ADR-C01 | Authoritative source history or owner-signed imported-snapshot declaration | All code integration and release provenance |
| ADR-C02 | Exact supported Python versions and pinned production patch | Packaging, CI, container |
| ADR-C03 | Actual license, copyright owner, release approver, operations approver, security approver | Metadata and release |
| ADR-C04 | Supported schema/version compatibility matrix and inventory of any deployed databases/binaries | Migration catalog and restore |
| ADR-C05 | Explicit migration command; process-lifetime shared/exclusive execution guard; transaction-visible maintenance epoch/fence; stale-guard recovery; old-writer fence | Migration, state, restore |
| ADR-C06 | Runtime lease capability shape, scopes, heartbeat interval, expiry, and optional fencing token | All state transitions |
| ADR-C07 | Force grammar plus terminal retry: same failed generation, existing frozen content unchanged (or first snapshot after an audited collection-only retry), allowlisted reason, no ambiguous/in-flight chunk, audited one-shot authorization, no automatic-budget reset | Generations and CLI |
| ADR-C08 | Telegram transport-stage classification; delay/attempt/elapsed retry caps; persisted wall-clock high-water mark and fail-closed rollback handling | Outbox and scheduler |
| ADR-C09 | Scheduler behavior after invalid config reload; DeliveryTargetSnapshot algorithm; stable HMAC-key custody/version/rotation; frozen-outbox mismatch policy | Scheduler and outbox |
| ADR-C10 | XML parser/isolation implementation and dependency review | Parser and worker security |
| ADR-C11 | Unicode scalar, control, bidi, quarantine, and display-sanitization policy | Ingestion, logs, identity, Telegram |
| ADR-C12 | Zero-story/all-source-failure policy and health/alert thresholds | Outbox, health, runbooks |
| ADR-C13 | RPO, RTO, retention, encryption/off-host store, and restore ownership/ACL policy | Backup/restore |
| ADR-C14 | Actual Linux/NAS architecture/filesystem and Windows service identity; both predecessor-supported targets remain mandatory | Platform gates |
| ADR-C15 | Independent alert channel and escalation ownership | Alerts and rollout |
| ADR-C16 | CI host, registry, signing identity, provenance format, SBOM format, scan tools, and waiver authority | Release gate |
| ADR-C17 | Offline dry-run input contract; any future live-source preview is a separate explicit mode and plan amendment | CLI, outcome policy, docs |
| ADR-C18 | WAL-aware live snapshot versus immutable offline artifact; FULL authority writers; sidecar permission/unavailable semantics | C2.0 and final preflight/restore integration |
| ADR-C19 | Acknowledgment persistence failure, missing-lease recovery, terminal collection reopen, and post-backup external-send reconciliation | State transition table, C2.4/C2.5/C3.3 |
| ADR-C20 | Per-source execution, queue and whole-cycle budgets; bounded IPC frame size/read deadline; shutdown grace; fairness policy | C4.4/C3.4 and target resource tests |

Narrowing Linux/NAS or Windows support is not an ADR-C14 shortcut. It requires a separately approved design-change task that atomically amends both plans, D3, the Definition of Done, F-023 mappings, CI/target evidence, and support documentation before candidate signing.

Decision changes after implementation starts require:

1. an ADR amendment;
2. impact mapping to findings, tasks, tests, migration/rollback, docs, and rollout;
3. domain-owner and independent-reviewer approval;
4. rerun of every affected cumulative gate.

---

## 7. Complete implementation-audit finding ledger

Each row is mandatory. “Proof to close” is the minimum, not a substitute for the task's full gate.

| Finding | Audited failure | Closure task(s) | Minimum proof to close |
|---|---|---|---|
| F-001 | No usable Git provenance, protected review path, approved production decisions, or release identity | C0.1, C0.2, C6.5 | Source declaration, decision index, baseline manifest, protected-branch evidence, signed release linkage |
| F-002 | Orphan CLI resolution/reason/operator flags can fall through; invalid config may create logs before validation; dry-run network semantics were inconsistent | C1.1 | Exhaustive CLI matrix plus byte-for-byte and no-remote-network side-effect assertions |
| F-003 | Preflight can return exit 0/ready=true for migration-required or newer schema | C1.2, C2.1 | N-1/N/N+1/malformed/corrupt truth table with nonzero incompatible exits |
| F-004 | Health can be green for terminal failure or incompatible schema; latest terminal state is hidden | C1.3, C5.1 | Terminal/schema/no-history-overdue/disk fault matrix |
| F-005 | Generation zero is treated as absent; force can recreate zero and does not require prior completion | C2.4 | Full predecessor-state and concurrent-force table |
| F-006 | Migration checksums depend on current schema; legacy v1 tables remain writable; pre-migration backup lacks a verified manifest | C2.1, C2.2 | Immutable-catalog test, legacy write fence, every-statement crash matrix, manifest restore |
| F-007 | Non-owners and compatibility mutators can change delivery state; ownership is optional/incomplete | C2.3, C3.3 | Every mutator wrong-owner/expired-owner tests plus 50 real process races |
| F-008 | Restore accepts active work and lacks process-lifetime exclusion plus complete version, permission, migration, WAL, and post-swap safety | C2.2, C2.3, C2.5, C5.3 | Live-process/guard race, restore crash matrix, active-work refusal, automatic rollback, second-operator drill |
| F-009 | Raw Telegram 5xx and broad transport errors are retried despite unknown acceptance | C3.1 | Transport-stage × response-envelope fake-server matrix |
| F-010 | retry_after can exceed the cap; no total elapsed retry budget; backward clock movement can extend an undefined budget; exhaustion visibility is incomplete | C3.2, C1.3 | Delay/attempt/elapsed/clock independent boundary and restart tests |
| F-011 | Outbox content/destination identity and mapping are incomplete; compatibility completion/terminal-retry paths can bypass acknowledged chunk/history semantics | C2.3, C2.4, C3.3 | Kill-point recovery, immutable chunk/target map, mismatch blocking, audited one-shot terminal retry/reconciliation, no compatibility bypass |
| F-012 | Scheduler ignores typed outcomes, does not reliably reload default/environment config, and lacks heartbeat/restart proof | C3.4 | Fake-clock, reload, midnight, lease-loss, typed-outcome restart matrix |
| F-013 | Empty success, all-source outage, degraded coverage, dry-run, and terminal outcomes are not fully distinct/visible | C3.5, C1.3 | Exact policy state/outbox/exit/health table |
| F-014 | Escaped lone surrogates can reach URL/title hashing or rendering and abort a healthy batch | C4.1, C4.6 | Scalar corpus with one hostile and one healthy item through the whole pipeline |
| F-015 | UTF-16 XML can bypass raw DTD scanning; entity expansion occurs; MemoryError can be swallowed | C4.3, C4.4 | Multi-encoding entity corpus, zero external access, explicit MemoryError propagation |
| F-016 | URL policy accepts multicast and lacks independent deployment egress enforcement | C4.2, C5.4, C5.5, C6.6 | Full address/DNS/redirect matrix plus exact-candidate routable-class/rebinding bypass tests and static/synthetic policy proof for non-unicast classes on both targets |
| F-017 | Source deadlines cannot reliably terminate blocked DNS/parser/thread work; lease heartbeat coverage is incomplete | C4.4, C2.3, C3.4 | Spawned worker timeout/reap/next-run recovery and lease-loss tests |
| F-018 | Title identity is source-dependent; merge ties are permutation-dependent; aggregator recency can beat direct publisher; fuzzy work is undercounted/unbounded | C4.5 | Identity-v2 migration, permutation/hash-seed properties, publisher priority, exact work counters |
| F-019 | Telegram final-payload and per-item omission guarantees are incomplete | C4.6 | Scalar/HTML/byte/UTF-16 property suite and persisted delivered/omitted map |
| F-020 | Logs default to stderr, redaction is not key-aware, bidi survives, and terminal/run identity is incomplete | C1.4, C5.1 | Stream, recursive token-canary, bidi, stable-schema, exactly-one-terminal-event tests |
| F-021 | Required metrics and alerts are absent; disk floor is 1 MiB instead of 1 GiB or 10% | C5.1, C5.2 | Metrics schema, injected health/alert matrix, delivery and recovery receipts |
| F-022 | Backup scheduling/retention/off-host proof and RPO/RTO restore evidence are absent | C5.3 | 7/4/12 preview/apply tests, verified backup receipts, timed restore drill |
| F-023 | Linux/NAS and Windows deployment scripts lack production-identity, ACL, storage, egress, scheduling, and architecture proof | C5.4, C5.5, C6.6 | Target-host evidence reports for the signed candidate on every supported platform |
| F-024 | Build-context sentinel searches text only and does not prove actual Docker context, layers, history, or runtime cleanliness | C6.4, C6.5 | Unique secret canary excluded from the exact candidate context, layers, history, filesystem, and runtime, with report digest bound before signing |
| F-025 | Coverage is 59%; property, fake-server, kill, multiprocess, migration, security, PowerShell, container, and multi-architecture CI are incomplete | C4.7, C6.1, C6.2 | Required adversarial/test layers, ≥90% overall, 100% critical branches, retained CI outputs |
| F-026 | License and Python support conflict; locks are incomplete; release workflow, SBOM, signature, provenance, and compatibility manifest are absent | C6.3, C6.5 | Consistent metadata/hash locks and signed build-once release bundle |
| F-027 | Production-status and target-behavior documentation overstate executable coverage; audit evidence is not indexed; runbooks are not independently exercised | C0.3, C5.6 | Honest status, finding/evidence schema, target-vs-verified labels, second-operator runbook records |
| F-028 | Shadow, canary, rollback rehearsal, cutover authorizations, controlled observation, and 72-hour production evidence do not exist | C7.1, C7.2, C7.3, C7.4, C7.5 | RA-S/RA-C/RA-P plus approved immutable rollout bundle for the exact release digest |

Ledger closure rules:

- one tracked issue is created per F-### row;
- an issue may contain several implementation packets but cannot close until every mapped task closes;
- new findings receive the next F-### identifier and are inserted before release;
- duplicate findings link to one canonical row; they are not deleted;
- waivers cannot close correctness, ambiguity, schema, lease, secret, or destructive-restore invariants;
- an allowed security waiver records owner, rationale, compensating control, expiry, and release approval.

---

### 7.1 Current review traceability and closure proof

Every row below starts **Open**. IDs R01–R20 are stable identifiers from the September review; F-### IDs retain the historical ledger. One repair may satisfy both, but each row needs its own evidence link and closure decision. Goals use G0–G7 to avoid confusing them with review IDs. P1 rows block production safety/correctness; P2 rows are also mandatory before release. Repair R10 first to make subsequent verification safe, then follow dependencies rather than severity alone.

| Finding / goal | Priority | Existing obligations and implementation tasks | Required observable closure proof |
|---|---|---|---|
| R01 Acknowledgment persistence failure / G2, G3 | P1 | F-007, F-011; C2.3, C2.4, C3.3 | Accepted request plus injected local commit failure, with missing/expired/live lease: exactly one request across normal reruns; durable sent or blocked uncertainty; no new generation or later chunk |
| R02 Stale immutable live reads / G1, G2 | P1 | F-003, F-004, F-008; C1.2, C1.3, C2.5 | Writer keeps committed lease/delivery only in WAL; all live inspectors see it or report unavailable; no stale ready/healthy/restore authorization |
| R03 Ineffective maintenance exclusion / G2 | P1 | F-006, F-007, F-008; C2.0, C2.2, C2.3 | Two contenders cannot both hold exclusive authority; existing real writable connection prevents maintenance; stale owner cannot commit after fence change |
| R04 Destructive/incomplete restore / G2, G5 | P1 | F-008, F-022; C2.5, C5.3 | Source/target unresolved-state matrix, exact schema catalog, atomic activation faults, and post-backup send reconciliation preserve evidence and prevent silent replay |
| R05 Weak durable intent/ack policy / G2 | P1 | F-007, F-011; C2.0, C2.3, C5.4, C5.5 | Every authority connection reports FULL; failed intent commit sends nothing; process-crash matrix plus separately identified storage durability evidence |
| R06 Retry limits reset by next invocation / G3 | P1 | F-005, F-010, F-011; C2.4, C3.2 | Attempt/elapsed exhaustion remains terminal across ordinary run, run-if-due, daemon, restart and midnight; explicit same-generation one-shot permit is audited and cannot reset history |
| R07 State ownership bypasses / G2 | P1 | F-007, F-011; C2.3, C3.3 | Complete mutator inventory rejects wrong/missing/expired/superseded authority in the write transaction; attempt/run binding, payload mapping, and chunk order enforced |
| R08 Telegram envelope validation / G3 | P1 | F-009; C3.1 | Exact boolean and positive integer response matrix, including null/string/bool IDs, wrong destination, malformed success, and explicit negative; unknown acceptance never retries |
| R09 False-green readiness/health / G1, G5 | P1 | F-003, F-004, F-010, F-013; C1.2, C1.3, C3.5, C5.1 | WAL failure, corrupt state, ancient collecting/no-lease/no-success, overdue and ordinary retry states yield correct fields/reasons/exits; actual CLI output agrees |
| R10 Unsafe/false-pass context test / G6 | P1 | F-024, F-025; C0.4, C6.4, C6.5 | Existing synthetic `.env` survives; positive leak control fails; real context/layers inspected; missing Docker remains blocked; no writes outside disposable fixture |
| R11 Dry-run ignores real input / G1, G4 | P2 | F-002, F-013; C1.1, C3.5 | Populated frozen input traverses real normalization/ranking/rendering; missing input is explicit usage failure; no remote client or writable state created |
| R12 General config hash strands outbox / G3 | P2 | F-011, F-012; C3.3, C3.4 | Minimum-score/source change resumes identical frozen bytes; true destination mismatch blocks with supported audited resolution and no SQL surgery |
| R13 Backup fabricates missing source / G2, G5 | P2 | F-006, F-008, F-022; C2.0, C2.2, C5.3 | Missing source creates nothing; WAL rows retained; concurrent same-time jobs cannot collide; manifest-last publication never exposes incomplete backup as valid |
| R14 Invalid source document counted empty / G4 | P2 | F-013, F-015; C4.3, C3.5 | RSS/Atom/GDELT shape matrix separates valid empty from XHTML outage, missing articles list, invalid records and all-quarantined input |
| R15 Dedup exhaustion resurrects duplicates / G4 | P2 | F-018; C4.5 | Already rejected duplicate stays rejected after a later comparison exhausts budget; unprocessed candidates remain deterministic with exact work accounting |
| R16 HTML/entity and continuation sizing / G4 | P2 | F-019; C4.6 | Supported entities, final byte/UTF-16 limits including headers, 900-unit long-URL reproducer, and healthy sibling retention all pass |
| R17 JSON report stdout polluted / G1 | P2 | F-002, F-020; C1.1, C1.4 | Parse actual subprocess stdout as one JSON document for every machine command, including failures; diagnostics remain on stderr |
| R18 Message/event redaction bypass / G1, G5 | P2 | F-020; C1.4, C5.1 | Synthetic secret absent in formatted message, event, args, exception/stack, nested fields, worker output and persisted sinks |
| R19 Global deadline starvation/unbounded IPC / G4 | P2 | F-017; C4.4 | Ten jobs/two workers with slow first jobs proves queue/execution/cycle budgets; partial IPC cannot hang parent; every child reaped; next cycle succeeds |
| R20 Scheduler ignores outcomes/reload/signal / G3 | P2 | F-012, F-017; C3.4 | Config changed during long wait takes effect before work; typed terminal/attention outcomes affect supervision; per-chunk authority loss and SIGTERM stop new sends |

### 7.2 Additional obligations from the review

These are tracked work, not newly confirmed exploits. Carry them into the ledger under the existing finding/task IDs and give each a separate acceptance record:

- **F-016 / C4.2, C5.4–C5.5:** close the gap between validated DNS addresses and the actual connection, and make proxy behavior explicit. Prove mixed answers, redirects, DNS rebinding and proxy bypass against controlled endpoints plus target egress positive controls. Do not claim an exploit was observed in this review.
- **F-023 / C2.0, C5.5:** use platform-specific process identity/liveness queries. A PID alone is not an authority token; account for reuse, access denied, dead process and boot/session identity. The Windows `os.kill(pid, 0)` probe did not terminate the tested child; do not describe that as a confirmed kill bug. See [Python's platform-specific os.kill documentation](https://docs.python.org/3/library/os.html#os.kill).
- **F-025–F-026 / C6.1–C6.3:** installed wheel/sdist smoke must run from an unrelated directory with an explicit configuration and prove the import path belongs to the installed artifact. Decide the support mismatch (metadata 3.12–3.14, CI 3.14, README/CONTRIBUTING 3.12/3.13, ADR target 3.14) through ADR-C02 and amend all declarations together. Add transitive hash locks, pinned build inputs, coverage enforcement and signed provenance.
- **F-021–F-023, F-027 / C5.1–C5.6:** provision an independent alert sink, scheduled/off-host backups and retention, state/history pruning with unresolved-work exclusions, second-operator restore, and actual target evidence. Correct stale status/architecture/coverage/schema claims with dated supersession links.

## 8. Multi-agent execution model

The environment permits four concurrent slots including the coordinator. Use at most three workers simultaneously.

### 8.1 Roles

#### Coordinator / integration owner

- owns both plans, decisions, issue ledger, dependency graph, shared-file locks, and final integration;
- is the only role allowed to close a task, finding, or gate;
- owns final wiring in meco_news/app.py unless one narrow packet explicitly delegates it;
- verifies evidence rather than accepting self-reported completion;
- stops parallel work when interfaces or files overlap;
- runs cumulative gates and coordinates external approvals;
- never approves their own implementation without another reviewer.

#### Agent A — State, delivery persistence, and recovery

Primary ownership:

- migrations, schema inspection contracts, state transitions, leases, attempts, generations;
- immutable outbox persistence, article history, backup/restore state semantics;
- true multiprocess, transaction-fault, crash, and recovery tests.

Default files:

- meco_news/storage.py
- meco_news/migrations.py
- migration resources/fixtures
- state-machine, migration, generation, concurrency, and restore tests

#### Agent B — Ingestion, content, transport security

Primary ownership:

- Unicode policy, URL/SSRF validation, network/redirect policy;
- XML/JSON parsing, source worker isolation, hostile-input corpus;
- identity, deterministic merging/deduplication, Telegram transport/rendering;
- adversarial, property, fake-server, and security tests.

Default files:

- meco_news/collectors.py
- meco_news/network.py
- meco_news/urls.py
- meco_news/models.py
- meco_news/ranking.py
- meco_news/telegram.py
- ingestion/security/content tests

#### Agent C — Control plane, operations, platform, and release

Primary ownership:

- CLI/config/preflight/health/logging/metrics/alerts;
- package metadata, locks, CI, Docker, Compose, Windows scripts;
- backup operations, deployment validation, runbooks, release evidence.

Default files:

- meco_news/config.py
- meco_news/preflight.py
- meco_news/observability.py
- Dockerfile, compose.yaml, scripts, CI configuration, packaging, and operations docs
- control-plane, observability, packaging, and platform tests

#### Rotating independent reviewer

For every packet, a different agent:

- reads the task contract and diff;
- replays the original audit reproducer;
- adds at least one credible counterexample or records why none is available;
- checks rollback, kill, and failure paths;
- reports findings without silently rewriting the implementation;
- does not approve code they authored.

### 8.2 Shared-file serialization

| Shared area | Integration owner | Rule |
|---|---|---|
| meco_news/app.py | Coordinator | Workers submit interface and test requirements; coordinator performs final wiring |
| meco_news/storage.py, inspection.py, migrate.py and migrations | Agent A | C2.0 → C2.1 → C2.3 authority core → C2.2 → C2.3 recovery → C2.4; one writer at a time |
| meco_news/telegram.py | Agent B | Transport and render packets merge serially; Agent A reviews state mapping |
| meco_news/config.py | Agent C | New fields freeze before dependent workers consume them |
| meco_news/preflight.py | Agent C | Agent A supplies read-only schema/status API; one editor at a time |
| meco_news/models.py and ranking.py | Agent B | Agent A reviews identity migration interface before merge |
| meco_news/backup.py | Agent A then Agent C | State safety freezes before operational scheduling/retention |
| test fixtures shared by domains | Coordinator | Domain-specific directories; shared fixtures change through an integration packet |
| plans/status/release evidence index | Coordinator | Agents propose edits; coordinator reconciles authoritative state |

### 8.3 Required task packet

Every dispatch includes:

- task and finding IDs;
- goal and invariant;
- dependencies and frozen interfaces;
- files owned and files forbidden;
- deterministic red test or external evidence protocol;
- focused and cumulative commands;
- failure/kill/rollback cases;
- evidence path and sensitive-data handling;
- reviewer and counterexample requirement;
- explicit stop conditions.

Every handoff includes:

- files changed;
- tests added and exact commands/results;
- original reproducer result;
- edge cases still open;
- schema/config/API compatibility effect;
- rollback instructions;
- evidence artifact hashes;
- suggested independent counterexample.

### 8.4 Dispatch, review, and progress protocol

Sub-agents are bounded implementers/reviewers, not independent release authorities. A coordinator plus A/B/C uses all four slots; a reviewer role is a reassignment of an existing worker, never an assumed fifth concurrent slot. Default review rotation is A's work → C, B's work → A, C's work → B; coordinator-authored app integration → the relevant non-author domain owner. If a reviewer co-authored that packet, rotate to another non-author. Human operations/security/release approvals remain distinct from agent review.

Before dispatch, record a packet ID, source/working-tree fingerprint, finding IDs, goal, acceptance matrix, dependency state and exclusive file allocation. Workers may read shared files but propose shared-interface edits to the owner. Acquire the file allocation before editing; release it after a complete handoff. Record blocked dependencies rather than editing around another worker. Do not stage, revert or discard pre-existing user changes to make a diff look clean.

Each worker reports at meaningful milestones: red reproduced; design/API ready; focused fix verified; review requested; or blocked with a precise missing input. A review rejection returns the packet to implementation with numbered counterexamples. Coordinator integration checks both the author's focused evidence and the reviewer's independent replay. No worker marks its own row Closed, publishes, sends real Telegram messages, or migrates/restores live data merely because a packet mentions those operations.

Use this concrete dispatch form; replace every bracketed value before execution:

```text
Packet: [P-ID]  Findings: [Rxx / F-xxx]  Goal: [Gx]
Source: [commit + working-tree manifest]  State: [Open / Red reproduced]
Implementer: [A/B/C/Coordinator]  Independent reviewer: [non-author]
Depends on: [accepted interface/packet IDs; external dependency if any]
Owns: [exact source/test/doc paths]  Read-only shared files: [paths]
Behavioral change: [concrete trigger -> intended state/output]
Red proof: [safe fixture, exact command, expected failure, evidence path]
Acceptance: [state/exit/request count/payload/limit and negative cases]
Loops: [CL identifiers]  Integration gate: [CG identifier]
Rollback: [code/config/schema recovery constraints; no production action]
Stop: [specific invariant failure or unavailable prerequisite]
Return: diff summary, actual commands/exits, artifact hashes, open cases,
        migration/config impact, reviewer counterexample, proposed ledger update.
```

---

## 9. Recurring closure loops

These loops are mandatory work with bounded iterations, retained failures and explicit exits. They are implementation instructions, not scheduled automations. An iteration consumes a versioned fixture/contract and emits an actual result/evidence record; it never waits indefinitely for a platform or approval. Run focused checks on each change and the cumulative offline gate after an integrated packet batch. Repeat a passed check only after a relevant change, new counterexample, failure or evidence invalidation.

If the same failure survives three materially attempted corrections, stop blind patching: the coordinator requests a root-cause/design review with the retained attempts and changes the packet before retrying. This is an escalation point, not a waiver or automatic closure. Unrelated dependency-ready work continues. External checks marked Blocked need an identified owner, missing prerequisite and resume condition; absence of evidence never becomes a pass.

The current 79% baseline is a recorded deficit, not permission to lower the final gate. Bootstrap a safe cumulative suite after C0.4; enforce no unexplained regression for each packet, close affected critical decision branches to 100%, and raise coverage toward the final separate ≥90% statement and ≥90% branch gates. Do not require the entire unrepaired repository to reach 90% before the first repair can merge. Known-red probes remain linked and visible; any temporary deselection/strict expected failure needs an issue, owner and removal packet. None may satisfy final acceptance for a mandatory finding.

### CL0 — Finding-to-red-test loop

1. Select the highest-priority dependency-ready Rxx/F-### row, with C0.4 test safety first.
2. Reduce the audit probe to a deterministic checked-in reproducer.
3. Record the current failing output and intended contract.
4. Confirm the test fails for the correct reason.
5. Have a reviewer check that the reproducer does not merely encode the proposed implementation.
6. Move the finding to Red reproduced.

Exit: one stable red test or approved external protocol is linked to the finding.

### CL1 — Red/green/review loop

1. Implement the smallest coherent change that satisfies the invariant.
2. Run the focused tests.
3. Run neighboring regressions and applicable Ruff/typing checks; after integration run the safe full offline suite and coverage under the ratchet above.
4. Run all applicable hostile/fault/concurrency tests.
5. Have another agent replay the audit and attempt a counterexample.
6. Address review findings through a new explicit packet.
7. Record commands, outputs, hashes, reviewer, and rollback.

Exit: focused and cumulative checks pass and independent review accepts the invariant.

Evidence per iteration: input source hash, fixture/seed, requested invariant, exact command/exit, actual state/output, diff hash, reviewer counterexample, and next action. A green assertion of internal implementation text is insufficient; check persisted state and externally visible behavior.

### CL2 — State-transition and kill loop

For every state mutation, test:

1. missing, wrong, expired, and wrong-scope owner;
2. invalid predecessor state and repeated idempotent call;
3. competing real processes;
4. kill before transaction, during transaction, before commit, and after commit;
5. kill before external request, after possible transmission, after acknowledgment, and before local acknowledgment;
6. restart, lease reclaim, and subsequent-run recovery;
7. row hashes proving forbidden calls changed nothing.

Exit: the transition table is complete, no optional runtime owner remains, and crash recovery preserves invariants.

### CL3 — Delivery ambiguity and retry loop

1. Exercise every transport stage against a local fake Telegram server.
2. Cross it with valid success, explicit negative, 429, raw 5xx, malformed body, disconnect, timeout, and process death.
3. Verify classification, persisted attempt, chunk state, next deadline, blocking behavior, and health.
4. Restart at every persistence/network boundary.
5. Verify confirmed chunks never replay and ambiguous chunks never auto-retry.
6. Test delay, attempt, and elapsed caps independently at boundary and boundary+1.

Exit: the complete decision table and kill matrix pass without public network access.

### CL4 — Hostile-input and isolation loop

For every external text, URL, feed, redirect, and parser boundary:

1. run normal, exact-limit, and limit+1 cases;
2. run malformed encoding, lone surrogate, bidi/control, nested structure, oversized, slow, hanging, crash, and MemoryError cases;
3. include one healthy sibling source/item;
4. assert stable reason code, bounded time/memory/work, no secret/raw payload leak, and unchanged healthy output;
5. assert no worker/process/thread survives timeout;
6. rerun the next scheduled collection to prove cleanup.

Exit: the full corpus passes on Linux and Windows spawn semantics.

### CL5 — Deterministic-content loop

1. Freeze canonical input fixtures.
2. Run all permutations for small groups and seeded properties for larger groups.
3. run multiple process hash seeds;
4. compare canonical selected items, keys, ordering, payload bytes, omission reasons, and work counters;
5. compare direct publisher versus aggregator conflicts;
6. compare against frozen, sanitized production-sample inputs and obtain business approval for content changes; durable scheduled shadow is reserved for C7.1 after CG6.

Exit: outputs are byte-identical for equivalent inputs and all work stays within budget.

### CL6 — False-green observability loop

1. Enumerate every mandatory check and terminal state.
2. Flip one condition at a time and representative combinations.
3. verify ready/healthy booleans, exit code, reason list, status, metric, log, and alert;
4. assert probes are read-only;
5. inject nested secret canaries, raw URLs, controls, bidi, and long errors;
6. group lifecycle events by attempt ID and prove exactly one terminal event.

Exit: all mandatory truth tables and redaction/event schemas pass.

### CL7 — Migration, backup, restore, and platform loop

1. Start from empty, every supported prior schema, current, malformed, corrupt, and future schema fixtures.
2. Inject failure after every migration/restore step and immediately before commit/swap.
3. verify manifest, checksum, logical equivalence, WAL handling, owner/mode/ACL, and automatic rollback;
4. repeat on supported target OS/filesystem/account/architecture identities;
5. time backup and restore against approved RPO/RTO;
6. have a second operator execute the runbook unaided.

Exit: old or new verified state is always recoverable and platform reports are approved.

### CL8 — CI, security, and release-evidence loop

1. Build one unsigned candidate from a clean protected source revision while capturing its actual context.
2. Run the full test/coverage and candidate context/layer/runtime/security matrix.
3. Generate SBOM, provenance, checksums, and compatibility manifest, then sign that same verified digest without rebuilding.
4. Run both exact-signed-candidate target protocols.
5. Verify artifacts, scans, target reports, and evidence hashes independently.
6. Rebuild only in a separate reproducibility test; never substitute that rebuild for promotion.

Exit: one signed build-once candidate is eligible for rollout and every scan finding is closed or explicitly allowed.

### CL9 — Rollout and feedback loop

1. Promote the exact signed digest to shadow only after RA-S, canary only after RA-C, and controlled production observation only after RA-P.
2. compare content, state, retries, resources, health, alerts, backups, and operator actions;
3. stop and roll back on a threshold breach;
4. convert every incident or unacceptable delta into a new F-### row;
5. repeat the relevant closure loops and rollout phase;
6. observe for 72 hours after cutover.

Exit: no open rollout finding remains and named approvers sign the stable-release evidence.

---

## 10. Dependency-ordered remediation roadmap

### Closure Wave 0 — Baseline, decisions, and reproducible evidence

Production deployment allowed: **no**.

#### C0.1 — Establish authoritative source provenance

Owner: Coordinator with Agent C  
Reviewer: named owner and release approver  
Dependencies: none

Work:

- inspect the existing Git history/remotes and identify the authoritative source without reinitializing or resetting this working tree;
- reconcile the reviewed commit and pre-existing user changes as a reviewable diff; if historical provenance needs recovery, preserve current work before any separately reviewed import;
- if not found, obtain an owner-signed statement that the directory is the authoritative imported snapshot and pre-import provenance is unknown;
- create a SHA-256 file manifest and preserve the audited snapshot;
- establish protected integration branch, CODEOWNERS/reviewer policy, named approvers, signed tags, and one issue per F-###;
- ensure secrets and generated evidence are excluded before the baseline commit.

Proof:

- source declaration, baseline manifest, remote/branch protection export, CODEOWNERS, issue index, and approver record.

Rollback/stop:

- never fabricate history or rewrite an authoritative remote;
- stop release work if the snapshot's authority is disputed.

#### C0.2 — Freeze production contracts

Owner: Coordinator  
Reviewers: Agents A, B, C and required business/operations owners  
Dependencies: C0.1 source identity for durable records

Work:

- resolve predecessor D1–D12 and ADR-C01–ADR-C20 for dependent work, distinguishing technical contract decisions from human ownership/platform/release choices;
- publish exact state/transition, schema, transport, retry, scheduler, Unicode, zero/outage, RPO/RTO, platform, and release contracts;
- mark target behavior separately from currently verified behavior in architecture/status documents;
- prevent schema/state/retry implementation from merging until its ADR is approved.

Proof:

- signed decision index with no unresolved placeholder on a dependent task.

Rollback/stop:

- a decision can be revised through change control; code relying on a superseded decision cannot proceed.

#### C0.3 — Freeze audit reproducers and evidence schema

Owner: Coordinator; domain agents own their fixtures  
Reviewer: rotating independent reviewer  
Dependencies: none

This task may run in parallel with C0.1 and C0.2 without production-code edits.

Work:

- convert every local audit probe into checked-in domain-specific tests or a precise external protocol;
- create stable reason codes and fixture builders, including a security corpus;
- define the evidence manifest, task-state ledger, command capture, artifact hashes, reviewer signature, and sensitive-evidence references;
- update docs/production-readiness-status.md to NO-GO and remove unsubstantiated completion language;
- retain the original failing behavior only as red-test evidence, never as executable sample data containing secrets.

Proof:

- all F-### rows link to a red test/protocol and intended contract.

#### C0.4 — Make verification safe before replaying the suite

Owner: Agent C; reviewer: Agent B. Goal: G6. Findings: R10, F-024/F-025. This is the first executable packet and may run alongside read-only C0 inventory; it does not wait for final release/provenance decisions.

Work and proof:

1. Reproduce the context verifier only in a temporary fake checkout with synthetic `.env`, configuration and backups; record their initial bytes and hashes. Never call the unsafe original test in the real checkout.
2. Refactor the verifier to take an explicit disposable context root, generate exclusive unique canary paths, and clean up only paths it created after resolving them beneath that root. Existing files must remain byte-for-byte unchanged on success, exception, interruption and unavailable Docker.
3. Remove mock/manual matcher success fallbacks from the real verification path. A textual `.dockerignore` lint may pass independently; actual-context verification must report Blocked when its daemon/permissions are unavailable. Add a deliberately included leak as a positive control that must fail.
4. Add backups, `.bak`, manifests, state, local evidence/config and credential-shaped synthetic files to the context test corpus. C0.4 proves local filesystem preservation and unavailable-daemon/detection behavior with controlled doubles; label these as harness tests. Real transmitted-context and image inspection belongs to C6.4–C6.5 and may remain Blocked while the safety bootstrap closes. Explicit Dockerfile COPY entries alone never certify context exclusion.
5. Run the repaired context test in isolation; reviewer proves pre-existing synthetic `.env` survives. Then run the full offline suite from the real checkout with all filesystem-mutating fixtures redirected to temporary directories. Keep Docker integration distinctly marked and visibly blocked if unavailable.

Exit: the dangerous test no longer mutates caller data, its safety regressions pass, and the verifier cannot claim a real context pass without real evidence. Actual candidate/layer closure still belongs to C6.4–C6.5. Record any residual deselection explicitly rather than reporting “all tests pass.”

#### Closure Gate CG0

- authoritative source status is recorded;
- decisions required by the next wave are approved;
- F-001–F-028, R01–R20 and additional Section 7.2 obligations have linked issue, owner, red test/protocol, reviewer and evidence path;
- C0.4 test-safety repair is independently accepted before full-suite verification;
- current status is honestly NO-GO;
- no production credential is present in test/config/evidence.

---

### Closure Wave 1 — Truthful control plane and observability foundation

Production deployment allowed: **no**.

#### C1.1 — Close the CLI mode grammar and side-effect ordering

Owner: Agent C  
Reviewer: Agent A  
Dependencies: ADR-C07, ADR-C17, and mode contract from C0.2

Files:

- meco_news/app.py
- dedicated CLI-mode tests
- README/configuration CLI documentation

Work:

- define one mutually exclusive command/mode grammar;
- reject orphan --resolution, --reason, and --operator and incomplete force/reconciliation arguments with exit 2;
- validate parser combinations before config, file logging, state, backup/restore, collection, scheduler, or Telegram construction;
- validate configuration/secrets before initializing writable file logging or state;
- ensure explicit maintenance-only options cannot fall through to normal delivery;
- make dry-run construct no remote collector or mutating service and return a structured non-delivery outcome from explicitly supplied frozen local input;
- reserve any future live-source preview for a separately named mode with its own reviewed contract.

Red tests:

- exhaustive valid/invalid single, pair, and representative triple option matrix;
- instrumentation proving invalid modes call none of configure-file-logging, StateStore, backup, restore, collector, scheduler, or Telegram constructors;
- byte-for-byte DB/WAL/SHM/log/status/timestamp invariance for dry-run;
- instrumentation proving dry-run makes no DNS/HTTP/Telegram call;
- invalid config creates no log or state artifact.

Exit:

- 100% branch coverage of the option validator and all no-side-effect tests pass.

September acceptance detail (R11/R17): define a versioned frozen-input schema with explicit source outcomes, article records, collection timestamp/timezone and optional offline history. Supply a populated fixture and assert actual normalization, dedup, ranking, omissions and final rendering; reject missing/invalid input before side effects rather than synthesize empty success. Capture files and client-constructor calls to prove zero writable state/network/log-file initialization. Run config-show, preflight, status, health and dry-run JSON modes as real subprocesses against success and error fixtures, and parse all stdout with one `json.loads()` call; assert exit code and absence of trailing records. Document the chosen input flag and schema in CLI help/runbooks when implemented, not as an already-existing command here.

#### C1.2 — Make preflight exact, read-only, and fail-closed

Owner: Agent C  
Schema API owner/reviewer: Agent A  
Dependencies: ADR-C04/C18 and C2.0 LiveStateSnapshot for integrated verification; fixture and command grammar work may begin earlier

C2.1 must later consume this same inspection interface without weakening or replacing its fail-closed classifications.

Work:

- use one read-only inspector with missing, migration_required, compatible, newer_incompatible, malformed, and corrupt results;
- make ready a pure conjunction of mandatory checks;
- return the documented nonzero schema exit for N-1 and N+1;
- verify the exact supported Python range;
- probe directory/database/WAL capability safely without mutating the live state database;
- define deterministic exit-code precedence for multiple failures;
- prohibit implicit migration in normal startup/preflight;
- while the exclusive maintenance guard is held, normal preflight returns ready=false with maintenance_in_progress and a nonzero exit;
- expose a non-public maintenance_verify routine that requires the live MaintenanceContext, reports verified_for_maintenance rather than ready, and performs the integrity/schema/storage checks needed for a temporary or swapped database.

Red tests:

- empty/missing, N-1, N, N+1, malformed ledger, a missing object from the ADR-C04 current structural signature, checksum mismatch, and corrupt DB;
- every mandatory check false individually and representative multi-failure combinations;
- preflight never creates a missing DB or changes application rows; live WAL-only commits are visible under Section 2.1, with sidecar-denied reads explicitly unavailable; strict byte/timestamp invariance is tested only for supplied offline artifacts;
- normal preflight during maintenance is non-ready; maintenance_verify rejects missing/stale/wrong-scope context and never emits ready=true;
- unsupported Python and unwritable/non-WAL-capable storage.

Exit:

- for every truth-table row, all mandatory checks ok if and only if ready=true and exit=0.
- C2.1/C2.2 must add the approved post-migration signature fixtures and rerun this entire truth table before CG2; CG1 does not pre-approve a future schema.

#### C1.3 — Separate status from health and eliminate false green

Owner: Agent C  
State-query owner/reviewer: Agent A  
Dependencies: C1.2 schema classification; state names frozen

Work:

- expose latest_delivery including terminal states separately from active_delivery;
- include latest attempt/success, current leases, generation, chunk, retry due, ambiguity, error class/reason, schema/app versions;
- fail health for active maintenance, terminal/attention delivery, ambiguity, retry exhaustion, incompatible/corrupt/unwritable state, stale heartbeat, overdue delivery even with no prior success, and disk below 1 GiB or 10%;
- distinguish no-history-not-yet-due from no-history-overdue;
- keep status and health read-only.

Red tests:

- maintenance_in_progress, failed_terminal, needs_attention, exhausted retry, incompatible schema, corrupt state, stale lease/heartbeat, no history due/not due, disk thresholds at boundary and boundary-1;
- representative simultaneous failures preserve all stable reasons and nonzero health;
- probe calls leave logical application state unchanged and follow the live/offline sidecar contract; corruption is an error, never reported as a missing database with exit 0.

Exit:

- health truth table has 100% decision-branch coverage.

September acceptance detail (R09): seed a WAL-only current lease, mandatory WAL capability failure, corrupt database, collecting since a fixed ancient date with no lease/success, no-history before/after due time, ordinary all-source backoff and actual exhaustion. With a controlled clock, assert ready/healthy flags, stable reasons, all concurrent failures, exit precedence and alert relevance. Missing, corrupt and unavailable are distinct outputs. Check freshness even when an active/latest row exists. Persist ordinary retry and exhausted retry as different reasons; a positive remaining budget cannot be called exhausted. Re-run this table through the real CLI after C2/C3 integration.

#### C1.4 — Establish safe structured logging and lifecycle identity

Owner: Agent C  
Reviewers: Agent B for hostile text/redaction; Agent A for attempt semantics  
Dependencies: ADR-C11; stable attempt kinds

Work:

- send normal lifecycle JSONL logs to stdout; when a command emits a single JSON report, send all diagnostics/lifecycle logs to stderr and reserve stdout for that document alone;
- define command, collection, delivery, and chunk attempt schemas with run/attempt/delivery/generation/chunk IDs;
- emit exactly one terminal event per attempt kind through one lifecycle finalizer;
- redact the final formatted message, event, arguments, sensitive keys and values, mappings, sequences, exceptions and stack text; use the same policy in spawned workers and every persisted sink;
- strip URL userinfo/query unless allowlisted, control/bidi characters, and cap hostile fields;
- persist stable error class/reason separately from sanitized display text.

Red tests:

- nested token/cookie/authorization/.env/URL-query canaries across config, exception, stack, DB, source, and Telegram paths;
- stdout/stderr stream capture;
- early return, success, retryable, ambiguous, terminal, exception, and recovery paths grouped by attempt ID with exactly one terminal record;
- valid Unicode retained while prohibited controls/bidi are absent.

Exit:

- log schema and canary report pass with no raw response/rejected URL/secret in logs, state, or status.

#### Closure Gate CG1

- C1.1–C1.4 independently reviewed;
- original CLI, schema false-green, terminal-health, logging, and redaction probes pass;
- control-plane critical branches are 100% covered;
- all probes are read-only where required;
- full offline/static suite remains green.
- this gate requires C2.0 live-read/exclusion foundations and accepts the current structural signature only; C1 fixture work may precede C2.0, but cannot certify live behavior; any later state/schema/signature change reruns C1.2/C1.3 and CG1 evidence before CG2 or CG3 closes.

---

### Closure Wave 2 — Migration, state authority, generations, and restore

Production deployment allowed: **no**.

#### C2.0 — Establish live inspection, durability, exclusion, and safe backup foundations

Owner: Agent A; reviewer: Agent C, with Agent B reviewing hostile/corrupt inputs. Goals: G1/G2/G5. Findings: R02/R03/R05/R13. Dependencies: C0.4 safe harness and the relevant ADR-C04/C05/C06/C18 contracts. This foundation begins before final CG1 certification; the wave number is a domain label, not a ban on dependency-ready foundation work.

Freeze these interface responsibilities before app integration (names are contracts, not a requirement to introduce unnecessary classes):

| Interface | Required behavior and authority |
|---|---|
| LiveStateSnapshot | One WAL-aware read transaction; coherent schema, leases, deliveries, attempts and freshness; missing/corrupt/unavailable distinct; never creates a missing source |
| OfflineArtifactInspection | Only a staged, consistent, quiescent artifact with explicit identity; immutable access permitted here, never as a live-DB optimization |
| RuntimeGuard + LeaseContext | Shared OS guard held through writable connection/runtime lifetime; transaction checks owner, scope, expiry and fence |
| MaintenanceContext | Exclusive OS guard with bounded acquisition and process identity; transaction-visible epoch; no marker-file check-then-write substitute |
| VerifiedBackup | Existing source, SQLite-consistent snapshot including WAL commits, verified catalog/integrity/digest, versioned manifest, unique completed artifact identity |

Implement in four serial packets on storage/inspection/backup paths:

1. **Live inspection:** keep a writer open with committed data exclusively in WAL. Read readiness/status/maintenance prerequisites through the same inspector. Test sidecar permission denial, active writer commit during a read, missing path, corrupt file and genuine offline snapshot. Define snapshot consistency rather than pretending a read can freeze later writers; write decisions must revalidate inside their authority transaction.
2. **Durability:** set and verify effective `PRAGMA synchronous=FULL`, foreign keys and required WAL mode on every authority connection. Failure to obtain the required policy prevents sends/mutation. Cover runtime, migration, restore staging, recovery and operator writer factories. Intent must commit successfully before any external request. Record the filesystem/device assumptions; process termination alone does not prove power-loss persistence.
3. **Exclusion:** acquire a real shared/exclusive OS primitive before opening writable state and retain it until all handles close. Marker metadata describes ownership but is not the lock. Use platform liveness/query APIs and process creation/boot identity where needed; PID reuse and access denied cannot authorize stale-lock removal. Test two simultaneous exclusive acquisitions, live runtime with no lease, long idle scheduler, holder death, reused PID, stale metadata and timeout. Wire the guard into actual StateStore construction, not just a separately tested context manager.
4. **Backup primitive:** open only an existing source without schema initialization. Use SQLite backup/snapshot semantics, unique exclusive names and a same-volume staging file; verify integrity, exact catalog, schema/app/source identity and digest. Flush completed artifact data before publishing its versioned manifest last; readers ignore missing/incomplete manifests. This is recoverable two-file publication, not a claim of atomic file-pair creation. Test missing source (no file created), concurrent same-timestamp requests, existing destination, disk full, WAL-only row, permission failure and crash at every publication boundary.

Exit: reviewers replay R02/R03/R05/R13 foundation cases; live readers, authority factories and the common backup primitive are accepted. C2.1–C2.5 reuse these primitives instead of independent implementations. Operations scheduling/retention and actual target durability remain later evidence obligations.

#### C2.1 — Replace mutable schema checksums with an immutable migration catalog

Owner: Agent A  
Reviewer: Agent C  
Dependencies: ADR-C04 and ADR-C05

Work:

- inventory any actually deployed schemas/checksums before assigning compatibility;
- preserve issued historical checksums if they exist; otherwise record an owner-approved imported baseline;
- define each migration from immutable canonical bytes/resources so adding a future migration cannot alter prior checksums;
- verify ordered versions, gaps, duplicates, checksum, required objects, and future versions;
- make runtime StateStore open refuse migration-required state;
- provide the explicit audited migrate command grammar, but keep execution disabled/fail-closed until C2.2 supplies and verifies the exclusive maintenance guard/fence.

Red tests:

- add a dummy future migration and prove all prior checksum values are unchanged;
- empty, every exact supported prior/intermediate/current fixture, malformed ledger, gap, duplicate, missing object, mismatch, and current+1;
- runtime open on prior schema changes no bytes and raises migration-required;
- repeated catalog-runner migration under a test guard is a no-op, while the public command fails closed with maintenance_unavailable until C2.2.

Exit:

- every supported fixture migrates/verifies and every unknown/newer/malformed fixture fails closed.

#### C2.2 — Make migration atomic, manifested, and old-writer-safe

Owner: Agent A  
Backup reviewer: Agent C  
Dependencies: C2.0, C2.1 and the accepted C2.3 authority core

Work:

- acquire the accepted exclusive maintenance authority and drain runtime holders first; then use the C2.0 common backup primitive to create and verify a pre-migration artifact before BEGIN with backup ID, SHA-256, UTC time, integrity, schema/app/source versions and redacted config hash;
- abort before schema change if artifact creation/verification fails;
- implement the process-lifetime shared runtime/exclusive maintenance execution guard and a transaction-visible maintenance epoch/fence before migration can run;
- require a MaintenanceContext carrying the acquired exclusive guard/fence; validate its authority inside the migration transaction and immediately before commit, not only on command entry; reuse the fence-check API from the accepted C2.3 authority core;
- provision every currently known downstream durable fact in the approved target schema, including force audit/predecessor, retry first/last/high-water/deadline/elapsed/manual-authorization fields, title-v2 identity, destination/send-option fingerprint, transition audit, and maintenance fencing;
- install tested BEFORE INSERT/UPDATE/DELETE fences on legacy runs/sent_articles tables or an equivalently proven old-writer barrier;
- migrate only under the exclusive maintenance guard;
- preserve the exact pre-migration database/application pair for rollback.

Red/fault tests:

- old v1 INSERT, UPDATE, DELETE, and INSERT OR REPLACE all fail after migration;
- a runtime holding the shared process guard blocks maintenance; exclusive maintenance blocks new runtime startup; stale-guard recovery follows ADR-C05 and cannot bypass a live process;
- inject failure after each SQL statement and before commit;
- backup/manifest corruption or failure leaves source logical data/schema unchanged; compare byte hashes only for unchanged immutable artifacts, not across legitimate WAL checkpoints;
- superseded/missing maintenance authority at the precommit boundary rolls back all schema/ledger changes;
- restore the manifest and prove logical equivalence to the starting fixture;
- repeated public migrate under the verified exclusive guard is an audited no-op;
- run the matrix on Linux and Windows.

Exit:

- every crash yields fully verified old or new schema; no legacy write succeeds; the real CLI migrate path is enabled only after this proof and tested for supported migration, audited no-op and fail-closed rejection (a permanent migrate-unavailable stub does not satisfy this task).

#### C2.3 — Require an authorized state capability for every mutation

Owner: Agent A  
Reviewer: Agent C; Agent B reviews untrusted stored fields  
Dependencies: C2.0, C2.1 and ADR-C06 for the authority core; C2.2 for post-migration recovery integration. Accept the authority core before C2.2 can enable real migration; this explicitly breaks the old migration/ownership dependency cycle.

Work:

- introduce mandatory runtime LeaseContext or equivalent and consume the C2.2 MaintenanceContext;
- hold the shared execution guard for the complete runtime-process lifetime;
- check scope, owner, expiry, lease fence, and the current maintenance epoch/fence in the same BEGIN IMMEDIATE transaction as every mutation and heartbeat;
- cover delivery create/start, source results, prepare, retry, chunk begin/finish, failure, completion, reopen, and reconciliation;
- remove/private owner-optional and compatibility mutators such as direct complete/fail paths;
- re-read lease and fence inside the write transaction, then atomically reclaim and mark orphaned in_flight work ambiguous; include absent/released leases, not only expired rows;
- heartbeat throughout collection/sending and treat heartbeat loss as fatal/unhealthy;
- make maintenance acquisition drain/refuse all shared runtime guards; a process that opened state before maintenance cannot mutate after the fence changes;
- keep acknowledged attempts and completed history immutable.

Red/fault tests:

- wrong/missing/expired/wrong-scope context for every mutator leaves row hashes unchanged;
- second connection cannot prepare, retry, fail, reopen, begin/finish chunk, or complete another owner's delivery;
- 50 real Windows-safe spawned-process races yield exactly one owner and sender;
- kill before/after lease, prepare, in-flight, acknowledgment, and release;
- race a live scheduler/process against maintenance acquisition and restore preparation; maintenance cannot proceed until the process exits, and a superseded process cannot heartbeat/mutate;
- confirmed chunks never replay.

Exit:

- no optional owner remains on a runtime mutator and the state transition/kill matrix passes.

September acceptance detail (R01/R03/R07): maintain a mutator inventory covering start, source results, prepare, retry, heartbeat, begin/finish chunk, fail, complete, reopen and operator resolution. Runtime and maintenance operations use distinct required capabilities; bootstrap/lease acquisition uses an explicitly scoped guarded transaction, not an owner-optional compatibility path. For every entry, test missing/wrong/expired/superseded capability and unchanged logical rows. Verify that `begin_chunk` permits only the next unacknowledged chunk, checks the frozen hash/item mapping, and creates an attempt bound to run, delivery, chunk, owner and fence; `finish_chunk` must reject an attempt from any other scope. Race recovery against heartbeat renewal with deterministic barriers before repeated real-process stress.

An accepted remote response followed by failed `finish_chunk` must not flow through a generic terminal finalizer that releases the only recovery signal. If persisting ambiguity also fails, stop sending, surface storage failure, and leave the previously durable intent sufficient for the next live-state inspection to block replay. A lease is coordination, not the sole record of uncertainty. C3.3 proves this through the actual app path.

#### C2.4 — Repair and atomize forced generations

Owner: Agent A  
Reviewer: Coordinator  
Dependencies: C2.3 and ADR-C07

Work:

- distinguish SQL NULL from integer generation zero;
- implement one transactional start_or_resume_generation decision for normal/resume/skip/force;
- reject force for no predecessor, active, failed, retry-wait, or ambiguous predecessor;
- allow force only after completed/completed_empty and create exactly N+1;
- persist operator, bounded reason, timestamp, and predecessor delivery ID;
- keep all prior generations and sent history immutable;
- make terminal retry/reopen an explicit audited operation distinct from force.
- define terminal retry as failed_terminal → retry_wait on the same frozen generation only when the reason class is allowlisted retry-safe, no chunk is ambiguous/in-flight, and destination/content snapshots are unchanged;
- atomically transition the explicitly named failed_terminal chunk to retry_wait with its delivery; previously sent chunks remain sent and later pending chunks stay blocked until this exact chunk is acknowledged;
- append the operator/reason/authorization audit record, preserve every prior attempt and acknowledged chunk, and grant one separately bounded manual attempt without resetting automatic attempt/elapsed history.

Red tests:

- the original generation-zero reproducer;
- every predecessor state, multiple generations, missing audit fields, and duplicate command;
- two simultaneous forced starts create at most one next generation;
- a failed forced generation cannot alter generation zero.
- terminal retry allowed/forbidden reason and chunk-state table, repeated authorization idempotency, one-shot budget, and proof that no content/generation/history row is rewritten.
- multi-chunk fixture with prior sent chunks, one terminal chunk, and later pending chunks proves only the named chunk is retried and the authorization is consumed on its transition to in_flight.

Exit:

- full force state/concurrency table passes and generation rows/history remain immutable.

September acceptance detail (R06): enumerate ordinary run, run-if-due, daemon, resume and force for every terminal predecessor. Exhausting attempt or elapsed budget must not create generation N+1 on the next invocation, including after restart or midnight while unresolved work remains. A later legitimately scheduled date must follow the explicit outage/backlog policy and cannot reset the failed delivery. Only completed/completed_empty permits force; only an audited, allowlisted, one-shot operation permits terminal retry on the same generation. Also define failed collection with no frozen chunks: an explicit audited same-generation collection retry can produce its first snapshot, preserving prior attempts and granting one separate bounded manual attempt. It cannot mutate an existing frozen snapshot or reset automatic history. Test concurrent/repeated authorizations and their expiry/consumption at the transactional start boundary.

#### C2.5 — Make restore exclusive, compatible, and automatically recoverable

Owner: Agent A for state safety; Agent C for operational wiring  
Reviewers: Agent B and Agent C  
Dependencies: C2.2, C2.3, ADR-C13

Work:

- acquire the C2.2 exclusive process-lifetime maintenance guard, advance the maintenance fence, and prove every existing runtime process has drained;
- refuse a live process/shared guard, active scheduler/delivery lease, or unresolved prepared/retry-wait/ambiguous/in-flight/terminal-with-unsent work in either target or source; “no lease” alone never proves drained delivery or stopped scheduling;
- verify manifest, checksum, schema/application/source compatibility, and integrity;
- restore to a temporary path, handle WAL/SHM consistently, apply only supported migrations there, and run maintenance_verify with the live MaintenanceContext;
- preserve the current target through the C2.0 verified backup primitive without first renaming the live path away; close all connections and settle sidecars under exclusive authority before replacement;
- restore POSIX owner/mode and Windows ACL, failing closed if impossible;
- atomically swap only after prechecks; run maintenance_verify again and automatically restore the original before releasing the guard if post-swap verification fails;
- keep all schedulers/process launch disabled, release the exclusive guard, and require normal offline preflight exit 0;
- if the final normal preflight fails, reacquire exclusive maintenance, restore the preserved target, leave scheduling disabled, and report terminal restore failure;
- make scheduler re-enable an explicit later step.

Red/fault tests:

- active future scheduler lease, delivery lease, in-flight chunk, bad checksum, corruption, incompatible version, migration failure, permission/ACL failure, post-swap failure;
- live process with and without a current lease racing every guard/fence/restore boundary;
- normal-preflight-versus-maintenance and valid/invalid MaintenanceContext truth tables;
- crash before/after every restore step;
- every failure preserves or restores verified logical state, history and unresolved-work evidence; compare artifact hashes only for byte-identical immutable files, since a valid WAL checkpoint may change database bytes;
- portable/local owner/mode/ACL preservation tests; actual target-account/filesystem assertions are retained for C5.3–C5.5 and C6.6.

Exit:

- the disposable automated restore-safety/fault matrix proves maintenance verification, automatic rollback, guard release, and a final normal read-only preflight exit 0; timed scheduling/retention, target owner/mode/ACL, second-operator execution, and RPO/RTO closure belong to C5.3/CG5.

September acceptance detail (R04/R13): cross source and target states (absent, clean completed, prepared, retry_wait, failed_terminal with unsent work, ambiguous, in_flight), lease states (live, expired, absent), and schema states (exact supported, explicitly migratable, newer, unknown signature, corrupt). Default refusal for unresolved work has a documented reconciliation route under maintenance authority; do not silently delete it to satisfy the precondition. A clean old backup is insufficient if Telegram sends occurred after its recovery point. Compare retained acknowledgment/uncertainty evidence and require operator reconciliation before scheduling. If the target is lost/corrupt and that evidence is unavailable, restore into a delivery-disabled recovery state and require reconciliation; do not claim safe replay from missing evidence.

Stage on the target volume, verify exact migration catalog and common manifest format, restore permissions, and activate with one supported atomic replacement after closing Windows/SQLite handles. Persist a recoverable operation record for staged/published/postcheck/rollback phases. Inject faults before and after each boundary, including failed rollback; any indeterminate outcome retains exclusive/recovery blocking, leaves scheduling disabled and names the preserved artifacts. A process restart must consult the recovery record before opening normal writable state. Re-run live WAL-aware preflight after releasing maintenance; re-enable scheduling only as a separately evidenced operator action.

#### Closure Gate CG2

- C2.0–C2.5 independently reviewed, including common backup publication, effective FULL policy and actual runtime guard wiring;
- full migration fixture/crash matrix and 50-process lease test pass;
- original generation, non-owner, legacy-write, and active-restore probes pass;
- no runtime compatibility API bypasses ownership;
- the complete C1.2/C1.3 schema and health truth tables pass against the post-migration structural signature;
- the approved target schema contains all durable fields currently required by C3/C4; any later schema delta automatically reopens C2.1, C2.2, the migration fault matrix, C1.2/CG1 schema evidence, and CG2;
- state-critical branches are 100% covered;
- only after CG2 may automatic delivery retry work integrate.

---

### Closure Wave 3 — Telegram ambiguity, bounded retries, outbox, and scheduler

Production deployment allowed: **no**.

#### C3.1 — Classify Telegram outcomes by proof, not convenience

Owner: Agent B  
State reviewer: Agent A  
Dependencies: ADR-C08; integration waits for CG2

Work:

- define a typed SendOutcome containing acceptance certainty, classification, response metadata, and safe reason;
- track whether failure is provably before transmission, possibly after transmission, or an explicit valid Telegram rejection;
- classify raw 5xx, malformed response, reset/read timeout after transmission, and unknown stage as ambiguous;
- make only provable pre-transmission failures or validated explicit negative Telegram envelopes retry-safe;
- keep a valid explicit 429 retryable with bounded retry_after;
- never derive replay safety solely from HTTP status or a broad URLError class.

Fake-server tests:

- body accepted then raw 500, disconnect, malformed JSON, or read timeout becomes ambiguous and receives no second request;
- connection refused before transmission is retryable;
- valid success, valid permanent negative, and valid 429 map correctly;
- process death after possible transmission recovers ambiguous.

Exit:

- complete transport-stage × envelope decision table passes.

September acceptance detail (R08): success requires `ok is True`, a correctly shaped result, a positive integer `message_id` excluding booleans, and destination fields that match the expected request under the documented API contract. Never coerce null/string/bool IDs or use truthiness for `ok`. Test `ok: "false"`, `ok: 1`, `message_id: null/true/"1"/0/-1`, missing/result-list fields, wrong chat/thread, empty JSON, duplicate/invalid JSON and oversized bodies. A malformed apparent success after transmission is ambiguous; only a validated explicit negative can establish non-acceptance. Retain the request count, received response class and persisted classification for every fake-server case. No live bot is needed for this matrix.

#### C3.2 — Bound and persist retry decisions

Owner: Agent B for policy; Agent A for persisted fields  
Reviewer: Agent C  
Dependencies: C3.1, CG2, and the C2.2 persisted retry/clock fields

Work:

- validate retry_after and never send earlier than a valid server-requested delay; if it exceeds a hard delay or elapsed budget, stop automatic retry with a visible reason rather than clamp it downward and send early;
- independently cap attempts, per-delay backoff, and max_elapsed_seconds;
- persist first-attempt UTC, last-observed UTC high-water mark, exact next deadline, attempt count, automatic elapsed consumption, and manual-authorization deadline;
- use monotonic time within a process; after restart, a backward wall-clock jump beyond the approved tolerance transitions to needs_attention/clock_rollback and schedules no automatic retry, while a forward jump consumes/exhausts the existing budget;
- make retry decisions once and stable across ordinary restart/clock progress;
- provide an operational kill switch preventing new automatic retries;
- transition exhausted work to a visible terminal/attention state.

Tests:

- caps at boundary and boundary+1, huge/negative/malformed retry_after;
- attempts and elapsed budget each exhaust independently;
- restart preserves exact deadline and payload hash;
- backward and forward wall-clock jumps before/after restart follow the fail-closed rule and can never lengthen max_elapsed_seconds;
- kill switch preserves state but schedules no new automatic retry;
- exhaustion is unhealthy and alertable.

Exit:

- retry timing cannot exceed any configured/hard budget.

Cross attempt and elapsed exhaustion with C2.4's invocation matrix. Capture generation, attempt count, first-attempt time, exact deadline and remote request count before and after every restart. Restart must preserve the frozen retry policy; configuration edits cannot enlarge its budget. Malformed negative/overflow retry_after follows the transport certainty table and a bounded safe policy, never an unchecked sleep. Test clock rollback/forward jump, manual permit expiry, kill switch, storage failure while scheduling a retry and a request that consumes the last available attempt.

#### C3.3 — Freeze outbox identity and audited reconciliation

Owner: Agent A  
Telegram reviewer: Agent B  
Dependencies: C2.4, C3.1, C3.2

Work:

- freeze item order, final HTML, raw payload hash, delivery/chunk ID, item-to-chunk mapping, and a non-secret DeliveryTargetSnapshot before send;
- bind the target snapshot to the bot's public identity, a versioned HMAC fingerprint of chat/thread destination, approved API endpoint class, parse mode and link-preview/send options; store the frozen retry policy and general configuration digest separately as policy/provenance, not as a source/ranking-sensitive destination comparison; never persist the token, HMAC key, or raw secret destination;
- manage the destination-fingerprint key separately from the Telegram token; new deliveries use the current key version, prior key versions remain available only until every unresolved snapshot using them is terminal, and a missing/unknown key version becomes needs_attention rather than triggering snapshot mutation;
- before every send/recovery, resolve and validate the current bot identity and recompute the target fingerprint; any bot/chat/thread/endpoint/send-option mismatch becomes needs_attention and sends nothing;
- include a visible deterministic delivery/chunk identifier on every content chunk and coverage/note message;
- validate chunk mapping indexes and prohibit prepared-content mutation;
- make attempts append-only;
- provide explicit status/resolve sent/resolve retry/terminal retry commands requiring operator, reason, maintenance authority, and audit record; terminal retry follows the exact C2.4 same-generation one-shot transition;
- resolve sent only with recorded external evidence and update history only for acknowledged/delivered mapped items;
- remove direct complete_run or other compatibility paths that bypass chunk acknowledgment.

Fault tests:

- kill at every prepare/send/persist boundary across multi-chunk payloads;
- corrupt/invalid mapping refuses send;
- acknowledged first chunk plus later failure never resends the first;
- omitted/quarantined items never enter sent history;
- manual resolution is idempotent and refuses active runtime ownership.
- token rotation resolving to the same bot/target may resume; a different bot, chat/thread, endpoint class, parse/send option, or destination fingerprint blocks without a request;
- destination-HMAC key rotation validates unresolved snapshots with their recorded prior key version; deleting that key early blocks safely and never rehashes frozen rows;
- target snapshot and prior payload bytes remain unchanged across every reload/restart.

Exit:

- immutable outbox and reconciliation state machine passes every CL2/CL3 kill point.

September acceptance detail (R01/R07/R12): use the actual app orchestration with a fake Telegram endpoint and disposable SQLite. Cross accepted response with failure at acknowledgment execute/commit/rollback/finalization/lease release, then restart with live/expired/absent lease. The endpoint must receive exactly one request for that chunk; later chunks/new generations remain blocked unless durable acknowledgment or audited resolution establishes safety. Include failure to persist the pre-send intent (zero requests), failure to persist the fallback ambiguity, and a second invocation while storage is still unavailable. Catch-all failure handlers may not erase uncertainty.

Prepare at least two chunks, fail a retry-safe first attempt, edit only `minimum_score` or source configuration, then resume. Exact stored bytes, mappings, destination and prior budget must be reused. For a true target mismatch, provide a documented audited operation that either restores the original target for continuation or abandons unresolved unsent work with preserved evidence before separately authorized new delivery. Ambiguous/sent chunks require their existing reconciliation; do not add a general “rewrite snapshot” shortcut. Test that an operator can exit the mismatch state without manual SQL even when there is no ambiguous chunk to resolve.

#### C3.4 — Make scheduler outcomes, reload, and time deterministic

Owner: Agent C  
State reviewer: Agent A  
Dependencies: C3.2, C3.3, C1.3, ADR-C09

Work:

- return and consume typed success/skip/retry_wait/attention/terminal outcomes;
- resolve the effective config path once and reload/validate immediately before executing work after every wait, including default/MECO_CONFIG cases; a config loaded before a long sleep is not the execution config;
- swap config only after full validation and record its hash;
- block new collection on invalid reload; allow recovery of already-frozen outbox policy only when the current validated credentials/destination/send options match its DeliveryTargetSnapshot;
- recover incomplete/due work before planning a new date;
- recalculate wake time at least every 60 seconds from delivery and durable retry deadlines;
- make heartbeat failure fatal and visible;
- handle WIB/host timezone differences, midnight, and backward/forward clock movement idempotently.

Tests:

- invalid/valid reload, default/env/explicit path, retry due earlier than daily due;
- ignored/nonzero typed result probes;
- restart in retry wait, after midnight, after config change, after lease expiry;
- host timezone changes and fake-clock jumps, cross-checked with C3.2 backward-clock needs_attention and forward-clock exhaustion behavior;
- repeated run-if-due never duplicates a delivery date.

Exit:

- daemon cannot swallow terminal/attention state and all restart/time matrices pass.

September acceptance detail (R20): define a typed-outcome dispatch table showing next wake, exit/continue behavior, health and alert effect for success/skip/retry_wait/attention/terminal. An ignored result assignment is not consumption. Heartbeat and revalidate ownership/fence before each chunk; renew during bounded waits and collection supervision. SIGTERM/Windows stop handling sets a stop request, prevents new collection/send, gives a bounded grace period, reaps workers and closes handles. If transmission may have occurred, preserve in-flight/ambiguous recovery evidence rather than label the send safely retryable. Test real child-process shutdown before intent, during request, after remote acceptance and between chunks; a signal handler only sets state, with durable finalization performed on the normal control path. Target process/scheduler shutdown proof remains C5/C6 evidence.

#### C3.5 — Implement explicit zero/outage/degraded/dry outcomes

Owner: Agent A with Agent C health/docs review  
Dependencies: C3.3, ADR-C12, ADR-C17

Work:

- distinguish successful zero eligible stories, all-source failure, partial degradation, skipped/not-due, dry-run, retry exhausted, and terminal configuration;
- implement the approved zero-story outboxed coverage notice and all-source retry/alert behavior;
- persist stable source/outcome reasons and expose them through status/health/metrics;
- ensure offline dry-run uses explicitly supplied frozen local input, is structured, and creates no network client, outbox, history, or state.

Tests:

- exact source-success × eligible-count table for collection-capable modes, plus the equivalent frozen-input table for offline dry-run;
- restart after empty notice preparation/send;
- all-source outage never masquerades as completed_empty;
- zero/partial/outage outcomes map to documented exit, state, log, status, health, and alert.

Exit:

- all outcome classes are distinguishable and restart-safe.

#### Closure Gate CG3

- original raw-5xx, retry-after, scheduler-return, config-reload, completion-bypass, and zero/outage probes pass;
- destination mismatch/rotation and backward-clock fail-closed matrices pass;
- fake Telegram, kill-point, restart, midnight, and retry-budget matrices pass;
- confirmed chunks never replay and ambiguity never auto-retries;
- outbox/transport/retry/scheduler critical branches are 100% covered;
- full offline/static suite remains green.

---

### Closure Wave 4 — Hostile input, deterministic identity, and message correctness

Production deployment allowed: **no**.

#### C4.1 — Centralize Unicode scalar and text policy

Owner: Agent B  
Reviewers: Agent C for logs; Agent A for stored fields  
Dependencies: ADR-C11 and C0.3 security corpus

Work:

- create one owned text-policy boundary used before model construction, identity, persistence, logging, and Telegram;
- quarantine title or URL containing an unpaired surrogate with invalid_unicode_scalar;
- sanitize optional display fields deterministically under the approved policy;
- remove prohibited bidi overrides/isolates and C0/C1 controls from output/diagnostics while preserving approved whitespace;
- preserve valid emoji/astral characters, combining text, and documented normalization;
- revalidate at process/IPC and persistence boundaries.

Tests:

- lone high/low surrogate independently in title, URL, domain, summary with a healthy sibling item;
- emoji, combining marks, NFKC equivalents, bidi, controls, HTML/entity-like text;
- hashing, JSON, SQLite, logs, and rendering never receive unchecked invalid scalars.

Exit:

- hostile item is isolated and healthy output is unchanged end to end.

#### C4.2 — Complete URL, DNS, redirect, and SSRF policy

Owner: Agent B  
Deployment reviewer: Agent C  
Dependencies: C4.1

Work:

- use one canonical URL module; remove duplicate canonicalization from models;
- validate scheme, userinfo, hostname syntax, IDNA, NFKC delimiter cases, brackets, port, and percent-encoding;
- explicitly reject loopback, private, link-local, multicast, unspecified, reserved, non-global, mapped forbidden IPv4, and metadata destinations;
- validate every A/AAAA answer and reject the host if any answer is forbidden;
- validate each redirect before following and enforce approved scheme/host transitions;
- keep raw rejected URL/location out of logs/state;
- document that DNS validation alone does not eliminate rebinding;
- create a deferred sequential-rebinding/bypass protocol for C5.4/C5.5/C6.6, where reachable controlled sinks and installed egress-rule counters must prove enforcement. CG4 does not claim that application DNS checks alone block a post-validation rebind.

Tests:

- 224.0.0.1, ff02::1, 0.0.0.0, ::, reserved ranges, mapped loopback/link-local/private;
- mixed public/private answers, redirect to private/multicast, downgrade, userinfo, malformed brackets/ports, IDNA/NFKC delimiters;
- sequential rebinding fixture records the application-layer residual without making a network request during CG4; its actual forbidden connection is required at the target egress gates;
- all original allowlisted public cases remain accepted.

Exit:

- complete syntax/address/DNS/redirect matrix passes with stable sanitized reasons.

#### C4.3 — Enforce parser-level XML/JSON safety across encodings

Owner: Agent B  
Reviewer: Agent A  
Dependencies: C4.1, C4.2, and ADR-C10

Work:

- replace ASCII byte-pattern DTD scanning with parser-level prohibition before entity expansion/access;
- reject internal, external, parameter, SYSTEM, and PUBLIC entities in UTF-8, UTF-16, and UTF-32 supported variants;
- ensure DTD/entity, encoding, and resource-limit failures never enter XML repair;
- preserve hard byte, depth, node, entry, text, and field limits with incremental enforcement;
- make JSON types, nesting, entry count, text, URLs, and Unicode scalars strict;
- add except MemoryError: raise before broad Exception catches at every parser/collector boundary.

Tests:

- internal/external/parameter and bounded expansion corpus across encodings/BOM variants;
- DTD token split across parser-feed chunks;
- HTTP/file entity sentinels prove zero access;
- every parser limit at boundary and boundary+1;
- MemoryError injected from XML feed, JSON decode, normalization, item creation.

Exit:

- no entity expands/accesses a resource; MemoryError is never labeled ordinary parse error.

September acceptance detail (R14): validate an expected RSS root/channel or Atom namespace/feed structure, and require a GDELT object with an `articles` list. Record syntactic parse, document validity and individual-record quarantine separately. Fixtures include valid empty RSS/Atom/GDELT, XHTML challenge/outage, arbitrary well-formed XML, missing/wrong-type articles, malformed records mixed with healthy records, and every record quarantined. Define source and overall collection outcomes explicitly so invalid documents cannot produce a healthy completed-empty notice. Keep schema validation bounded and preserve a healthy sibling source. C3.5 replays these cases through collection-to-outbox/status behavior after parser integration.

#### C4.4 — Enforce hard source deadlines with killable isolation

Owner: Agent B  
State reviewer: Agent A  
Dependencies: C4.2, C4.3

Work:

- run each source in a separately killable spawned process under a parent monotonic deadline;
- apply concurrency limits before launch;
- on deadline terminate, bounded join, kill if required, close IPC, and prove no worker remains;
- cap and revalidate returned result shape at the parent;
- classify worker MemoryError/crash/deadline distinctly while retaining healthy-source results;
- heartbeat the delivery lease from the parent and stop work on lease loss;
- apply reviewed process memory/CPU/result limits without relying on detached unkillable executor threads.

Tests:

- hanging DNS, socket read, slow drip, endless response, false Content-Length, partial read, infinite parser, child crash, MemoryError, unpicklable/oversized result;
- deadline plus bounded termination grace on Linux/Windows spawn;
- no live child afterward and the next run succeeds;
- healthy sibling source survives every fault.

Exit:

- no source can delay the run beyond the reviewed bound or outlive its supervisor.

September acceptance detail (R19): assign separate monotonic queue-wait, launched-source execution and whole-cycle deadlines, with approved finite defaults/maxima in ADR-C20. Record queued/started/completed times and whether a job expired unstarted or while executing. A total cycle cap may legitimately prevent a job starting; report that truthfully and use a deterministic fair order so the same late sources do not starve every cycle. Bound IPC frame size and incremental receipt/deserialization time; `poll()` before an unbounded `recv()` is insufficient. The supervisor must remain able to renew leases, process stop requests and reap children while a worker writes a partial frame.

Use ten source jobs/two slots, slow first jobs plus healthy later jobs, worker crash, partial/oversized frame, stalled writer, MemoryError and cycle expiration. Assert source/cycle time bounds with documented scheduling tolerance, parent memory bounds, distinct reason codes, preserved healthy results and no live descendants/open pipes after termination. Repeat the next cycle and vary launch order/hash seed. Fake clocks cover decision boundaries; real spawned-process tests cover actual IPC blocking and cleanup on both supported OS families.

#### C4.5 — Make identity, merge, and fuzzy dedup deterministic and bounded

Owners: Agent B for content; Agent A for migration/history  
Reviewer: Coordinator  
Dependencies: C4.1, C4.2, and the C2.1/C2.2 migration contract

Work:

- add versioned title-v2 as a domain-separated hash of NFKC/case/whitespace-normalized title only, never source name;
- preserve old keys and backfill v2 atomically without rewriting frozen delivery evidence;
- use one URL canonicalization implementation;
- never mutate caller-owned NewsItem objects during merge;
- select identity URL/source independently from enrichment fields;
- classify direct publisher versus aggregator only from code-owned collector provenance plus the validated canonical article hostname against a reviewed code/config-owned registry; never trust feed-supplied source labels or provenance metadata for this decision;
- prefer a validated direct-publisher identity over Google/GDELT/known aggregators regardless of recency/summary length;
- define deterministic keys for summary, publication time, topics, matches, and a final total-order tie-break over canonical serialized fields;
- replace retained-list fuzzy scanning with inverted token buckets;
- count each unique candidate pair before overlap/similarity shortcuts and separately cap postings, pairs, similarity calls, per-item, and global work;
- retain empty-token/unmatched items on budget exhaustion and emit stable metrics/reasons.

Tests:

- exhaustive 24 permutations of a four-item fixture, seeded properties, and multiple PYTHONHASHSEED subprocesses;
- newer/longer aggregator against older direct publisher;
- exact-quality ties differing in every enrichment field;
- deep canonical snapshots prove every caller-owned NewsItem and nested value is byte-for-byte unchanged before/after every merge permutation;
- disjoint/common/near-threshold/empty-token corpora and every budget at limit/+1;
- v1-key to v2-key backfill/rollback fixtures.

Exit:

- canonical selected output and payload inputs are byte-identical across order/hash seed and every work counter is exact/bounded.

September acceptance detail (R15): represent processed-kept, processed-confirmed-duplicate and unprocessed candidates distinctly. At exhaustion, never add a confirmed duplicate back to output. Replay the review's duplicate pair at comparison budgets 0, 1, exact required budget and boundary+1, then combine it with a later exhaustion trigger and healthy unrelated story. Enumerate small input permutations and seeded larger fixtures. Assert output identities/order, comparison counts, exhaustion reason and healthy retention rather than internal source text. A change to fuzzy grouping must retain exact dedup/history behavior and cannot expand work beyond the frozen hard budget.

#### C4.6 — Guarantee final Telegram payload and per-item isolation

Owner: Agent B  
Reviewers: Agent C for logging; Agent A for delivered history  
Dependencies: C4.1, C4.5, C3.3

Work:

- validate/sanitize each item before fingerprinting or rendering;
- omit only the hostile/oversized item with a stable persisted reason;
- construct final chunks first, then enforce raw HTML bytes and ≤3,900 UTF-16 units;
- guarantee valid Unicode scalars/UTF-8, prohibited-control/bidi exclusion, HTML escaping, timezone-derived labels, and visible delivery/chunk IDs;
- persist exact delivered/omitted item mapping;
- never add omitted items to sent history.

Property tests:

- unit/byte limits and +1, giant URLs, emoji-heavy blocks, lone surrogate beside healthy item, hostile HTML/bidi/controls;
- every emitted message strict-encodes, parses under Telegram HTML assumptions, respects both limits, and maps to frozen items;
- one item failure cannot abort healthy chunks.

Exit:

- full message property suite and outbox/history mapping pass.

September acceptance detail (R16): emit literal `·` or a numeric entity instead of `&middot;`, consistent with the [Telegram HTML contract](https://core.telegram.org/bots/api#html-style). Compute final raw-byte and UTF-16-unit sizes after delivery/chunk ID, continuation prefix, escapes, tags, separators and coverage notes are reserved. Include the review's 900-unit build with a URL suffix of 715 characters, first/continuation/final chunks, astral Unicode, escaped quotes/ampersands and numbering-width changes. Omit only an item that cannot fit safely; retain healthy siblings and exact item-to-chunk history mapping. If every item is omitted, produce the documented zero/quarantine outcome rather than claim sent content. Local contract verification is not a claim that the live API rejected the old named entity.

#### C4.7 — Run the full adversarial application corpus

Owner: Agent B  
Reviewer: independent agent who did not implement the packet  
Dependencies: C4.1–C4.6

Work/proof:

- run all Unicode, XML/JSON, URL/DNS/redirect, deadline/worker, identity/dedup, and Telegram corpus families;
- assert stable reasons, bounded CPU/memory/time/work, no raw hostile payload in logs/DB, healthy-output preservation, no entity access, and no surviving worker;
- replay every original ingestion/security audit probe;
- add the reviewed critical-branch checker before CG4 and enforce 100% decision-branch coverage for Unicode scalar/control policy, URL/DNS/redirect classification, XML DTD/entity and parser limits, MemoryError propagation, worker termination/reaping, identity migration, publisher classification, deterministic merge/fuzzy budgets, and Telegram omission/sizing;
- publish a focused application-security closure report.

#### Closure Gate CG4

- C4.1–C4.7 independently reviewed;
- all original surrogate, UTF-16 entity, MemoryError, multicast, deadline, identity, deterministic merge, fuzzy-accounting, and final-message probes pass;
- the C4.7 reviewed checker exists and reports 100% coverage for every listed content/security decision set;
- no unbounded or detached worker remains;
- application security report has no open high/critical finding.

---

### Closure Wave 5 — Metrics, alerts, backup operations, platform hardening, and runbooks

Production deployment allowed: **no**; successful Wave 5 only makes the signed candidate eligible for final release validation.

#### C5.1 — Complete status, metrics, and terminal-event coverage

Owner: Agent C  
Reviewers: Agents A and B  
Dependencies: stable outcomes from CG3 and reason codes from CG4

Work:

- expose bounded read-only JSON metrics/status through CLI or equivalent stdout;
- include latest attempt/terminal/success, lease, generation/chunk/retry/ambiguity/schema/app/disk fields;
- expose run/chunk totals/durations, source requests/failures/items/quarantine/bytes/deadlines, URL/redirect/SSRF rejects, dedup postings/pairs/similarity/budget, DB/lease errors;
- ensure one terminal event for every command/collection/delivery/chunk path;
- document metric type, unit, labels, cardinality, and retention.

Proof:

- metrics schema snapshot, outcome fault matrix, terminal-event grouping, and no-side-effect query tests.

#### C5.2 — Implement independent alerts and recovery receipts

Owner: Agent C with operations owner  
Reviewer: Agent A  
Dependencies: C1.3, C5.1, ADR-C15

Work:

- define stable alert ID, severity, threshold, first-seen, deduplication, delivery receipt, recovery receipt, and escalation;
- alert on stale/missed schedule, terminal/ambiguous/exhausted delivery, schema/corruption/state/WAL/disk/lease faults, source outage, backup/restore failure, and sustained resource breach;
- use a channel independent of production delivery where feasible;
- test every rule against a fake sink before target integration.

Proof:

- injected-failure alert matrix with both firing and recovery receipts.

#### C5.3 — Automate manifested backup, retention, and restore drills

Owner: Agent C; Agent A owns restore semantics  
Reviewer: second operator  
Dependencies: C2.5 and ADR-C13

Work:

- unify manual/automatic/pre-migration backup manifest format;
- schedule verified backups, encryption/off-host copies as approved, and receipt monitoring;
- implement retention preview/apply for 7 daily, 4 weekly, and 12 monthly without deleting newest or last verified backup;
- enforce attempts ≥90 days and article identity ≥365 days or approved stronger policy;
- run timed automatic and manual restore drills without silently migrating source;
- record logical equivalence, RPO, RTO, owner/mode/ACL, and post-restore preflight.

Proof:

- retention boundary tests, backup/off-host receipts, and two-operator RPO/RTO report.

September operations detail: C5.3 reuses C2.0's single backup manifest/parser for routine, migration and restore artifacts. Add an idempotent scheduled backup operation with non-overlap, off-host checksum receipt, last-success age and independent overdue/failure alert. Retention tests span daily/weekly/monthly boundaries, interrupted upload and deletion failure; never remove the only verified restore point or an artifact referenced by unresolved recovery. State/history pruning uses an explicit transaction and dry preview, preserving unresolved deliveries, audit/reconciliation records and the approved dedup horizon. A second operator must restore a selected retained backup, reconcile post-backup sends, measure RPO/RTO and leave exactly one scheduler. Evidence must include the alert's arrival and recovery receipt; a local log line does not prove notification.

#### C5.4 — Harden Linux/NAS deployment and prepare its target gate

Owner: Agent C  
Security reviewer: Agent B  
Dependencies: CG4, C5.1–C5.3, ADR-C14

Implement the deployment controls and verify the complete protocol on a production-like host/filesystem/account/architecture:

- uniquely identified test image/base digest and native amd64/arm64 smoke as applicable;
- non-root UID/GID 10001, read-only root/app/config, only state writable, umask 077;
- DB/WAL/SHM creation, owner/mode, local supported filesystem; reject SMB/NFS/CIFS;
- dropped capabilities, no-new-privileges, init, tmpfs, CPU/memory/PID limits;
- source-scoped firewall/DOCKER-USER or equivalent public HTTPS/DNS allow and private/loopback/link-local/multicast/unspecified/reserved/metadata deny;
- wherever the kernel permits a controlled reachable TCP alias, exercise representative IPv4/IPv6 loopback, private, link-local/metadata, reserved/mapped/non-global classes and the sequential-rebinding fixture against that sink;
- for multicast, unspecified, and any class the kernel cannot route as a TCP destination, verify normalized ruleset coverage plus synthetic packet/nftables-policy tests and counters rather than requiring an impossible listening sink;
- distinguish application, kernel, route, and installed-firewall denial in evidence; a routable bypass case closes only with installed-rule counters/logs or packet evidence, not merely absent routing. Approved public HTTPS/DNS positive controls must succeed;
- health fault transitions and SIGTERM within 30 seconds leave recoverable lease/chunk state.

Pre-release proof:

- redacted harness report with host/OS/kernel/arch/filesystem/account, rule IDs, commands, times, config hash, digest, results, and reviewer;
- C5.4 closes at CG5 when the hardening implementation and repeatable harness pass against the identified test artifact; F-016/F-023 remain open until C6.6 repeats the protocol with the exact signed candidate.

#### C5.5 — Harden Windows deployment and prepare its target gate

Owner: Agent C  
Security reviewer: Agent B  
Dependencies: CG4, C5.1–C5.3, ADR-C14

Implement the deployment controls and verify the complete protocol on a production-like Windows target:

- dedicated non-admin account rather than current interactive user;
- explicit ACL application/verification for app, config, state, logs, backups, and secrets;
- exact venv interpreter and uniquely identified test-wheel hash;
- S4U/logged-out execution, IgnoreNew, StartWhenAvailable, task retries and execution limit;
- WIB behavior while host timezone changes and two invocations proving one owner/sender;
- durable stdout terminal logs and exit codes;
- program/account-scoped egress rules equivalent to Linux deny policy;
- wherever Windows permits a controlled reachable TCP alias, exercise representative IPv4/IPv6 loopback, private, link-local/metadata, reserved/mapped/non-global and sequential-rebinding cases against that sink;
- for multicast, unspecified, and any class Windows cannot route as a TCP destination, verify normalized rule coverage with synthetic WFP/policy tests plus firewall/ETW evidence rather than requiring an impossible listening sink;
- distinguish application, kernel, route, and installed-firewall denial; a routable bypass case closes only with installed-rule log/ETW/counter or packet evidence, with approved public HTTPS/DNS positive controls;
- two install and two uninstall runs; uninstall preserves env, DB/WAL, logs, and backups by hash.

Pre-release proof:

- redacted Task XML/history, ACL export, transcript, preservation hashes, egress receipts, status/health, and reviewer;
- C5.5 closes at CG5 when the hardening implementation and repeatable harness pass against the identified test artifact; F-016/F-023 remain open until C6.6 repeats the protocol with the exact signed candidate.

#### C5.6 — Make operational documentation honest and executable

Owner: Agent C  
Reviewer: independent operator  
Dependencies: implemented contracts from CG1–CG4 and C5.1–C5.5

Work:

- change production-readiness status from “implemented coverage” to evidence-state reporting;
- label target architecture separately from verified behavior;
- update README, architecture, configuration, monitoring, deployment, release, SECURITY, changelog, and every incident runbook;
- include symptoms/alert, safe diagnostics, decision tree, recovery, verification, rollback, owner, evidence, and forbidden actions;
- document ambiguity reconciliation, terminal retry versus force, migration/restore guard, zero/outage behavior, scheduler reload, egress, and rollback;
- have a second operator execute ambiguity, no-delivery, corruption/restore, disk-full, and rollback drills without author help.

Proof:

- doc consistency check and signed drill records; no unsupported production-ready claim remains.

#### Closure Gate CG5

- metrics/status/log/health/alert fault matrix passes;
- backup/retention and second-operator restore meet RPO/RTO;
- both Linux/NAS and Windows hardening plus repeatable target protocols pass against identified test artifacts;
- runbooks are independently exercised;
- C5.4/C5.5 task closure proves the hardening/harness implementation only; the cross-task findings and target release gate remain open until C6.6 records exact-candidate evidence. CI inspection alone is never target evidence.

---

### Closure Wave 6 — Test depth, CI, packaging, container, security, and release

Production deployment allowed: **no**; CG6 creates one rollout-eligible signed candidate.

#### C6.1 — Build the required test and coverage layers

Owner: domain agents for their code; Coordinator for harness  
Reviewer: outside each domain  
Dependencies: C0.4 for initial safe harness; accepted interfaces as each domain integrates; complete CG1–CG5 contracts for final acceptance. Harness work begins in C0, not after all implementation waves.

Required checked-in layers:

- exhaustive CLI/config/preflight/health truth tables;
- local fake HTTP and Telegram integration;
- migration/intermediate/backup/restore fixtures;
- 50 repeated true-process lease/send races;
- SQL/commit/network/restore kill points and subprocess termination;
- retry/restart/midnight/timezone/config reload;
- hostile Unicode/XML/JSON/URL/DNS/deadline/dedup/message corpus;
- token canaries across log/status/state/backup/context/image;
- PowerShell parser, PSScriptAnalyzer, and Pester;
- installed wheel and container/platform smoke.

Rules:

- tests cannot reach public networks except separately controlled dependency/build jobs;
- overall line and branch coverage ≥90%;
- 100% branch coverage for CLI modes, config decisions, migrations, lease/state transitions, force, outbox classification, retry clock/budget policy, health, secret redaction, Unicode scalar/control policy, URL/DNS/redirect policy, XML/entity/parser limits, MemoryError paths, worker termination/reaping, identity migration, publisher classification, deterministic merge/fuzzy budgets, and Telegram omission/sizing;
- tests leave no unmanaged children, mutable state, or generated repository artifacts.

#### C6.2 — Enforce the full CI matrix

Owner: Agent C  
Reviewers: Agents A and B  
Dependencies: C6.1

CI jobs:

- Ruff format/check, strict typing, first-party static security;
- Linux and Windows Python 3.12/3.13 or exact ADR-C02 matrix;
- unit, property, fake-server integration, fault, concurrency, migration, backup/restore;
- coverage and JUnit artifacts;
- PowerShell parser/PSScriptAnalyzer/Pester;
- secret, dependency, filesystem, image, and code scans;
- Buildx native/multi-architecture container validation;
- installed wheel and production-entrypoint smoke.

Controls:

- actions pinned to commit SHAs;
- least-privilege tokens/permissions;
- required protected checks;
- retained immutable outputs;
- no silent high/critical waiver.

#### C6.3 — Resolve metadata, Python support, dependencies, and locks

Owner: Agent C  
Reviewers: Agents A and B  
Dependencies: ADR-C02, ADR-C03, ADR-C10

Work:

- set actual license/owner without inventing them;
- align pyproject, README, CI, Ruff/mypy targets, Docker base, and release manifest to exact supported Python versions;
- define one canonical version source and verify wheel/sdist/runtime consistency;
- review whether maintained HTTP/config/XML dependencies are adopted;
- produce transitive hash-locked runtime and development inputs for every supported platform strategy;
- document update cadence and vulnerability response.

Proof:

- metadata/version report, hash-lock verification, clean environment install, wheel/sdist inspection, dependency audit.

#### C6.4 — Build the actual-context/layer/runtime verification harness

Owner: Agent C  
Security reviewer: Agent B  
Dependencies: container build available

Work:

- retain the textual .dockerignore check as a fast lint;
- create unique synthetic secret canaries in ignored root/subdirectory/config/env-like locations only within a dedicated disposable context copied from the identified source; never plant them in the working checkout or overwrite existing files;
- implement inspection of actual context transfer/build input, BuildKit records where available, image filesystem, layer tar/history, metadata, and running container;
- assert no canary, VCS secret, evidence secret, env file, state DB, backup, or local config enters any layer;
- verify multi-stage cleanup cannot hide a secret in a lower layer;
- remove only uniquely created canaries after retaining evidence and prove the caller checkout and pre-existing synthetic fixtures remain unchanged;
- treat unavailable daemon/inspection permissions as Blocked, not a text-matcher fallback pass; both actual context and all image layers must be observed to satisfy their checks.

Exit:

- the harness detects deliberately included positive-control canaries and excludes ignored negative-control canaries on a disposable test build;
- C6.4 closes the verifier implementation only. F-024 remains open until C6.5 runs it during the exact build-once candidate creation and binds the report to that digest.

#### C6.5 — Create a signed build-once release candidate

Owner: Agent C and release approver  
Reviewers: security and operations approvers  
Dependencies: CG5, C6.1–C6.4, and C0.1

Work:

- export a clean protected signed source tag into a dedicated disposable build context, verify its tracked-file manifest, place unique ignored synthetic canaries there, capture actual context, and build exactly one candidate image/artifact set;
- run the C6.4 verifier against that exact candidate digest, including context, BuildKit record, every layer/history entry, filesystem, metadata, and runtime; retain the report hash and remove ephemeral canaries only after evidence capture;
- reject and never sign/promote the candidate if any context/layer/runtime proof fails; a fix requires a new reviewed source tag and a new candidate;
- bind source commit/tag, app version, lock/build-input hashes, context-report hash, base/final image digests, wheel/sdist hashes, schema compatibility, migration/backup steps;
- generate SBOM, provenance/attestation, checksums, scan links, and compatibility matrix, then sign that same verified digest without rebuilding;
- prohibit rebuild between shadow, canary, and production; promote the same digest;
- verify signature and manifest before every promotion.

Proof:

- immutable release manifest and verification transcript.

#### C6.6 — Validate the signed candidate on targets and close security independently

Owner: Coordinator  
Reviewers: all domain agents plus named security/release approvers  
Dependencies: C6.5 and access to both ADR-C14 target identities

Work:

- repeat the complete C5.4 and C5.5 production-identity, ACL/mode, storage, egress, scheduler/signal, architecture, health, and recovery protocols using the exact signed candidate digest;
- retain approved target-host reports; a CI/container inspection or earlier test-artifact run is not target evidence;
- replay every original audit reproducer;
- run standard repository security scan plus dependency, secret, filesystem, and image scans;
- read back every F-###, R01–R20 and additional Section 7.2 issue against exact-candidate test, review, scan, target and artifact evidence;
- reject missing, stale, mismatched-digest, or self-approved evidence;
- record every allowed waiver with owner, compensation, expiry, and approval.

Exit:

- zero unwaived high/critical findings; F-001–F-027, R01–R20 and all non-rollout Section 7.2 obligations are closed with accepted exact-candidate evidence. Only F-028 and its explicitly linked rollout/observation evidence remain open.

#### Closure Gate CG6

- complete CI matrix passes on protected source;
- ≥90% overall and 100% required critical branch coverage pass;
- packaging/locks/Python/license are consistent;
- the actual context/layer/history/runtime report names the same digest that is signed and promoted;
- both Linux/NAS and Windows pass the full protocol with the exact signed candidate;
- SBOM, provenance, signature, checksum, compatibility, and scans bind one immutable digest;
- source, operations, security, and release approvers accept the candidate;
- every R01–R20 correction and non-rollout Section 7.2 obligation is independently closed for this candidate before any shadow/canary/cutover authorization; no current finding is deferred until CG7 merely because its historical task mapping exists;
- the same candidate is the only artifact eligible for Closure Wave 7.

---

### Closure Wave 7 — Shadow, canary, rollback rehearsal, cutover, and observation

Only the coordinator/release owner performs external writes. Domain agents review evidence.

#### Rollout Authorization RA-S — Enter shadow

RA-S requires CG6 plus signatures from the named business, operations, security, and release approvers. The retained authorization identifies the exact candidate digest, separate shadow database, recording sink, production-like configuration hash, schedule, evidence location, data/secret handling, stop thresholds, duration, and owners. RA-S permits non-production shadow only.

#### C7.1 — Run 3–7 days of durable shadow

Owner: Coordinator  
Reviewers: Agents A/B/C  
Dependencies: CG6 and RA-S

Work:

- use the exact signed candidate, separate database, production-like schedule/config, and recording/fake delivery sink;
- do not use ordinary dry-run as the shadow substitute;
- record due behavior, selection/freshness exclusions, source outcomes, retries/budgets, resources/duration, payload hashes, logs/health/alerts/backups;
- compare content against approved production expectations.

Exit:

- no freshness violation, source-wide abort, routine budget exhaustion, unacceptable content delta, secret leak, or unresolved incident; daily summaries approved.

#### Rollout Authorization RA-C — Enter canary

RA-C requires approved C7.1 evidence and signatures from business, operations, security, and release approvers. It identifies the exact unchanged digest, separate canary bot/chat/state, scope, cycle count, fake-endpoint fault prerequisites, backup/restore plan, alert owner, stop thresholds, evidence path, and expiry. RA-C permits non-production canary only.

#### C7.2 — Run at least three scheduled canary cycles

Owner: Coordinator  
Reviewers: Agents A/B/C  
Dependencies: approved C7.1 and RA-C

Work:

- use separate bot/chat credentials and state with the same signed digest;
- before live canary, exercise controlled 429, restart, and ambiguous send against the fake endpoint using that artifact;
- run at least three actual scheduled cycles;
- record message IDs, delivery/chunk IDs, payload hashes, retries, alerts, resources, backup, and test restore.

Exit:

- no confirmed duplicate, unexpected ambiguity, secret leak, missed/duplicate schedule, alert failure, restore failure, or resource breach.

#### C7.3 — Rehearse and retain rollback readiness

Owner: operations owner  
Reviewers: release approver and Agent A  
Dependencies: approved C7.2; execute on a disposable production-like target before cutover

Work:

- time compatible-binary rollback and backup-required rollback where applicable;
- restore state identity/history without confirmed replay;
- reconcile ambiguity before resume;
- verify health, one scheduler, prior artifact signature/compatibility, and RTO;
- document exact rollback thresholds and authority.

Exit:

- timed rehearsal passes; rollback remains ready throughout cutover and observation.

#### Rollout Authorization RA-P — Enter controlled production observation

RA-P is the pre-cutover authorization gate. It requires approved C7.1/C7.2/C7.3 evidence and signatures from business, operations, security, and release approvers. The retained record binds:

- exact candidate digest/signature, source tag, schema, and config hash;
- target-host reports and a fresh verified backup/restore receipt;
- proven stopped old schedulers, clear leases/in-flight state, and one-scheduler cutover design;
- tested rollback artifact, thresholds, authority, and RTO;
- approved change window, communication/escalation owners, first-cycle observers, and 72-hour monitoring plan;
- explicit acceptance that the release state becomes CONTROLLED_PRODUCTION_OBSERVATION, not production-ready.

RA-P alone authorizes C7.4 production cutover. An expired, revoked, mismatched, or incomplete authorization prohibits cutover.

#### C7.4 — Perform controlled production cutover

Owner: release approver and operations owner  
Reviewers: security and domain owners  
Dependencies: approved C7.2, C7.3, and RA-P

Checklist:

- set and report release state CONTROLLED_PRODUCTION_OBSERVATION; do not publish a production-ready claim;
- signed change approval identifies old/new digest and schema;
- verified backup ID and restore receipt exist;
- all old schedulers/processes stop and shared guards, leases, and in-flight chunks are clear;
- candidate signature/digest/config hash verify;
- before maintenance, normal offline preflight must return either 0 for the exact current compatible schema or the exact expected migration_required code for an approved prior schema; any other result aborts;
- acquire exclusive maintenance; run explicit migration once only when the approved migration_required result was observed; run maintenance_verify before releasing the guard;
- with scheduling still disabled, release maintenance and require normal offline preflight exit 0 plus post-migration status/health pass;
- exactly one scheduler is enabled;
- first production cycle is observed end to end;
- heightened monitoring owner and 72-hour window begin.

Exit:

- signed cutover evidence records every command/result, identifier, time, and approver; production rollback is invoked when a C7.3 threshold requires it, not artificially.

#### C7.5 — Observe for 72 hours and feed incidents back

Owner: operations owner and Coordinator  
Approvers: business, operations, security, release  
Dependencies: C7.4

Work:

- retain CONTROLLED_PRODUCTION_OBSERVATION throughout the full window;
- monitor schedule/delivery SLOs, duplicates/ambiguity, retries, source coverage, resources, disk, DB/lease, alerts, backups, and operator interventions;
- turn every breach or unacceptable content delta into a new F-### issue;
- roll back on approved stop threshold;
- rerun affected loops and rollout phases after any fix;
- finalize stable-release evidence only after a clean full 72 hours.

#### Closure Gate CG7

- C7.1 shadow, C7.2 canary, C7.3 rollback rehearsal, C7.4 cutover, and C7.5 observation are approved for the exact CG6 digest;
- every rollout incident is closed and affected gates rerun;
- F-028 closes;
- F-001–F-028, R01–R20 and every additional mandatory issue in Section 7.2 are Closed with source-matching evidence;
- named business, operations, security, and release approvers sign the evidence index.

Only CG7 permits the production-ready status.

---

## 11. Parallel execution schedule

The coordinator dispatches only dependency-ready work and uses file allocations from Section 8. Waves identify domains; the dependency sequence below controls implementation. Early foundation/harness work does not claim a later release gate is closed. Reuse verified existing implementation; do not redo a task merely because its historical wave is open.

| Phase | Agent A | Agent B | Agent C | Coordinator serialization |
|---|---|---|---|---|
| Safety/baseline | C0.3 state/schema red fixtures and transition table | Review C0.4; prepare transport/content fixtures | C0.4 disposable context repair; bootstrap C6.1 safe harness | Record source/dirty-state identity, current R/F ledger, relevant decisions; preserve user work |
| Foundations/control | C2.0 live reads/FULL/OS guard/backup; C2.1 catalog | C3.1 envelope proof and disjoint parser/dedup fixtures | C1.1/C1.4; integrate C1.2/C1.3 after live inspector freezes | Own app.py; review interfaces; CG0 then integrated CG1 after foundations |
| Authority/recovery | C2.3 authority core → C2.2 migration → C2.3 recovery → C2.4 → C2.5 | Non-author state counterexamples and disjoint transport/content work | Review guards/restore/control matrices; no concurrent backup.py editing | Confirm CLI migration is enabled only after its safety proof; replay CG1 then close CG2 |
| C3 | C3.3 and persisted portion of C3.2 | C3.1 and transport-policy portion of C3.2 | C3.4 and C3.5 observability/docs | Integrate app.py/config; resolve interfaces |
| C4 | Identity/history migration support and state review | C4.1 → C4.6 serial by overlapping files; C4.7 | Prepare platform egress protocols, no claim of target pass | Freeze model/persistence boundary |
| C5 | Restore/backup drill and state review | Egress/security review and corpus replay | C5.1–C5.6, hardening and target harnesses | Coordinate production-like hosts/approvals |
| C6 | Concurrency/migration/recovery CI and evidence review | Security/property/fake-server CI and rescan | Finish early C6.1/C6.2 harness work; package/context/release pipeline | Run full matrix and issue readback; exact-candidate target replay |
| C7 | Evidence review only | Evidence review only | Operations evidence support | Sole rollout integration/external-write authority |

Allowed parallelism never overrides:

- one editor for migrations/storage at a time;
- one editor for Telegram transport/render state at a time;
- coordinator-only final app.py wiring;
- no C3 automatic retry integration before CG2;
- no platform/release gate before core correctness/security gates;
- no shadow before CG6 and RA-S;
- no canary before RA-C and no production cutover before RA-P;
- no canary/cutover using a rebuilt or mismatched artifact.

### 11.1 First executable packets and dependency exits

| Packet | Goal / findings | Implementer → reviewer | Dependency / concrete handoff |
|---|---|---|---|
| P00 Safe test runner | G6 / R10 | C → B | C0.4; safe fixture preservation proof, explicit blocked-Docker behavior, full-suite safety go/no-go |
| P01 Snapshot and transition inventory | G0/G2 / R01–R07, R12–R13 | A → C | Read-only now; versioned states/mutators/authority/backup map plus reduced red probes; no application edits until file allocation |
| P02 Live read and durable guard foundation | G1/G2 / R02/R03/R05 | A → C | P00 + accepted interfaces; C2.0 live-WAL/FULL/OS-lock tests and actual StateStore wiring |
| P03 Common backup and catalog | G2/G5 / R13, historical F-006 | A → C | P02; verified snapshot/manifest publication and exact catalog consumed by migration/restore |
| P04 Ownership and uncertainty recovery | G2/G3 / R01/R07 | A plus coordinator app integration → C/B respectively | P03; every mutator guarded, missing-lease uncertainty retained, fake-endpoint one-request proof |
| P05 Migration and restore | G2/G5 / R03/R04 | A → C | Accepted authority core; old-writer fence + commit checks, CLI migration enablement, restore crash/reconciliation matrix |
| P06 Generations/retry/destination | G3 / R06/R08/R12 | A persistence, B transport, coordinator app integration → non-author per subpacket | CG2 before automatic-send integration; envelope unit proof may start early; no ordinary budget reset, frozen resume, audited target resolution |
| P07 Truthful operator interface | G1/G5 / R09/R11/R17/R18 | C plus coordinator app integration → B/A respectively | P02 for final live tests; safe grammar/logging fixtures earlier; actual subprocess JSON/state/exit matrix |
| P08 Source/content boundaries | G4 / R14/R15/R16/R19 | B → A | Disjoint fixtures early; state/history interface frozen before integration; source-shape, dedup, final-render and bounded-process evidence |
| P09 Scheduler integration | G3 / R20 | C plus coordinator app integration → A | P06/P07; coordinate P08 supervisor interface; typed-outcome, immediate reload, per-chunk ownership and stop/restart proof |
| P10 Operational and release proof | G5/G6/G7 / Section 7.2 and remaining F rows | C/Coordinator → domain reviewer plus actual human approvers | Core gates; clean unrelated-directory artifact tests, full CI/coverage, both target protocols, alert/restore, signed promotion and rollout |

Split mixed-author rows into independently reviewed subpackets with disjoint files; this table never grants two simultaneous editors of app.py/storage.py/telegram.py. P04 can establish the uncertainty-preserving state path before CG2; its full send integration and final proof are repeated in P06/C3.3 after CG2. C3.5 initially uses typed source-result fixtures; R14 integration reruns it after C4.3. C6.1 starts with P00 and matures throughout, so test infrastructure never waits for the code it is needed to verify.

---

## 12. Verification command contract

These are intended commands/protocols, not results from editing this plan. Use the project's provisioned interpreter (`.venv/Scripts/python.exe` on this checkout) and record its version/path. A command is not evidence until its exit code, tool versions, source fingerprint, environment, output artifact and hash are retained. Future helpers named here must be created/reviewed by their task; their absence is not a pass. C0.4 is mandatory before invoking any full suite that contains the current unsafe context test.

### 12.1 Fast local loop

- python -m pytest -o addopts='' -q path/to/reviewed_focused_test.py (replace the placeholder with the packet's actual safe test path)
- python -m ruff format --check .
- python -m ruff check .
- python -m mypy meco_news

After C0.4, integrated batches run `python -m pytest -o addopts='' -q` with reviewed temporary fixtures. Before C0.4, only explicitly inspected safe focused tests or the documented audit command with `--deselect=tests/test_c06_context.py::TestContext::test_verify_with_context` may run; retain the deselection and original failure. Do not claim a complete pass with that omission.

### 12.2 Coverage gate

- choose a new evidence directory for this source/iteration; retain the old coverage result instead of erasing it;
- configure coverage for spawned children before running the suite; validate collection with a behavior executed only inside a child, then combine only matching-source/OS/runtime data under the declared coverage policy;
- run `python -m coverage run --branch -m pytest -o addopts='' -q` after C0.4, with its data-file path explicitly set to the new evidence directory;
- retain report and JSON output, enforce the combined `--fail-under=90` check, and additionally check statement and branch percentages separately at ≥90%; a combined percentage alone cannot certify both;
- a reviewed critical-branch register/checker maps the C6.1 decision sets to source locations and behavioral cases, enforcing 100% of their feasible required branches with justified exclusions individually reviewed;
- fail on missing expected child coverage, unknown exclusions, mandatory skipped/xfail cases or source-manifest mismatch; publish denominators, misses and exclusions as well as percentages.

### 12.3 Build/package gate

- python -m build
- build in a provisioned clean environment; install wheel and sdist separately into clean environments, change to unrelated directories with no checkout on import paths, verify installed-module paths, and run version/CLI/preflight/dry-run/status with explicitly provisioned synthetic config/input/state
- verify transitive hash-locked runtime/development installs

### 12.4 Platform/container gate

- docker compose config
- existing textual context lint
- new actual-context/layer/history/runtime canary verification
- docker buildx multi-architecture build/smoke under the approved matrix
- PowerShell parser, PSScriptAnalyzer, Pester, task XML, ACL, and logged-out target tests

### 12.5 Security/release gate

- first-party static scan
- secret scan
- dependency audit
- filesystem and image scan
- SBOM/provenance/signature/checksum generation and verification
- independent original-probe replay

Command rules:

- offline tests have public network denied;
- local loopback fake servers are allowed;
- ephemeral raw artifacts and canaries are cleaned only after required redacted evidence, immutable references, and hashes are retained;
- the evidence index and required retained artifacts are never cleaned by ordinary test cleanup and follow the approved retention policy;
- logs/output are redacted before repository storage;
- external target/CI URLs are referenced by immutable ID and hash;
- a flaky or retried failure remains visible in evidence.

---

## 13. Evidence and issue format

Suggested release evidence index:

- docs/evidence/production-readiness/{release-id}/index.json
- source/provenance.json
- decisions/adr-index.json
- findings/ledger.json
- ci/test-coverage.json
- ci/fault-concurrency-migration.json
- security/application-corpus.json
- security/scans-waivers.json
- observability/log-health-alert-matrix.json
- backup/backup-retention-restore.json
- platform/linux-target.json
- platform/windows-target.json
- release/manifest.json
- rollout/authorization-shadow.json
- rollout/shadow-summary.md
- rollout/authorization-canary.json
- rollout/canary-summary.md
- rollout/rollback-drill.md
- rollout/authorization-production-observation.json
- rollout/cutover-checklist.md
- rollout/observation-72h.md
- approvals.json

The exact location may change by ADR, but the index must contain:

- release/source/task/finding IDs;
- UTC timestamp and environment identity;
- exact command/tool versions and exit code;
- source commit/tag and working-tree cleanliness;
- artifact URI and SHA-256;
- expected and actual result;
- sanitized failure/retry history;
- implementer and independent reviewer;
- applicable decision/waiver/expiry;
- rollback note;
- approval identity and time.

Sensitive raw evidence remains in an approved restricted store. The repository contains only redacted summaries, immutable references, hashes, timestamps, and approver identities. Secrets, raw tokens, raw hostile payloads, state databases, and private backups never enter Git.

Issue template:

- Finding: Rxx and/or F-### (preserve both cross-links when applicable)
- Closure task: C#.#
- Contract/invariant:
- Current lifecycle state:
- Red test/protocol:
- Dependencies:
- Implementer:
- Reviewer:
- Focused commands/artifacts:
- Cumulative gate:
- Counterexample result:
- Rollback:
- Residual risk/waiver:
- Evidence manifest link:
- Closure decision/date:

---

## 14. Gate summary and approval matrix

| Gate | Required result | Minimum independent approval | Unlocks |
|---|---|---|---|
| CG0 | Current source/decisions/reproducers/evidence schema reconciled; C0.4 verification safety accepted | Owner plus all domain reviewers | Dependent integrated implementation; isolated safety/fixture work may start earlier |
| CG1 | CLI/preflight/health/logging truthful, using accepted C2.0 live-read foundations | Non-author domain reviewers | Control-plane acceptance; C2 foundation work does not wait for this gate |
| CG2 | Migration/lease/generation/restore state invariants pass | Agent C plus Coordinator | Automatic retry/outbox integration |
| CG3 | Ambiguity/retry/outbox/scheduler/outcomes pass | Agents B/C and Coordinator | Content/security cumulative integration |
| CG4 | Hostile-input/determinism/message application security passes | Agent outside each implementation packet | Operations/platform validation |
| CG5 | Metrics/alerts/backup/platform hardening/runbooks and target harnesses pass | Operations, security, second operator | Protected release candidate build |
| CG6 | Full CI/security, exact-candidate target gates, and signed build-once candidate pass | Security, operations, release approvers | RA-S review |
| RA-S | Exact-digest shadow plan, separate state/sink, thresholds, owners, and evidence path approved | Business, operations, security, release approvers | Non-production shadow |
| RA-C | Shadow accepted; exact-digest canary scope, credentials/state, faults, thresholds, and expiry approved | Business, operations, security, release approvers | Non-production canary |
| RA-P | Canary and rollback accepted; exact digest, backup/restore, window, thresholds, one-scheduler plan, and observation owners approved | Business, operations, security, release approvers | CONTROLLED_PRODUCTION_OBSERVATION cutover |
| CG7 | Shadow/canary/rollback-rehearsal/cutover/72-hour evidence passes | Business, operations, security, release approvers | Production-ready declaration |

A failed later gate reopens every upstream task whose invariant could have caused it. The coordinator records the dependency and repeats the relevant loops; no gate is waived by schedule pressure.

---

## 15. Program Definition of Done

### Finding closure

- [ ] F-001 through F-028 each have red proof, implementation, cumulative verification, independent review, evidence, and closure decision.
- [ ] R01 through R20 and Section 7.2 obligations each have an explicit evidence/closure record; historical receipts are linked or superseded without being misrepresented as current.
- [ ] No mandatory issue is hidden as a duplicate, documentation note, TODO, or untracked residual risk.
- [ ] Every original audit probe and every implementation-audit probe passes against the release candidate.

### Correctness and delivery

- [ ] Control-plane commands cannot cause unintended side effects.
- [ ] Preflight and health cannot be falsely green.
- [ ] Every state mutation has transaction-local authority.
- [ ] Migration, force, outbox, scheduler, and restore state machines pass their full tables and crash matrices.
- [ ] Confirmed chunks never auto-replay; ambiguity never auto-retries.
- [ ] A failed acknowledgment commit and an absent lease cannot hide uncertainty or create a fresh automatic generation.
- [ ] Live WAL state is visible or explicitly unavailable; every authoritative writer verifies FULL; actual writable connections hold the required OS guard.
- [ ] Restore preserves/reconciles unresolved and post-backup send history; missing sources and partial backup publication cannot appear successful.
- [ ] Retry delay, attempts, and elapsed time are durably bounded.
- [ ] Zero, outage, degraded, dry, retry, attention, and terminal outcomes remain distinct.

### Security and content

- [ ] Unicode/XML/JSON/URL/DNS/redirect/worker/message corpus passes.
- [ ] Deployment egress blocks forbidden destinations when application checks are bypassed.
- [ ] Identity is source-independent and content output is deterministic across input order and hash seed.
- [ ] Fuzzy work is fully counted and bounded.
- [ ] One bad source/item cannot abort healthy work.
- [ ] No secret or raw hostile payload appears in source, context, image, logs, status, state, backup manifest, or evidence.
- [ ] No unwaived high/critical security finding remains.

### Operations and platforms

- [ ] Normal stdout JSONL logs and single-document JSON reports follow the stream contract; message/field/worker redaction, exactly-one terminal events, status, health, metrics and independent alerts pass injected failures.
- [ ] Backup scheduling/retention/off-host receipts and restore drills meet approved RPO/RTO.
- [ ] Both Linux/NAS and Windows pass identity, storage, ACL/mode, egress, scheduler/signal, architecture, and recovery validation.
- [ ] A second operator successfully executes critical runbooks.

### Test and release

- [ ] Overall line and branch coverage is at least 90%; listed critical branches are 100%.
- [ ] Required Linux/Windows/Python/fault/concurrency/migration/PowerShell/container/multi-architecture CI passes.
- [ ] License, ownership, Python support, version, metadata, dependencies, and transitive hash locks are consistent and approved.
- [ ] Protected source/tag and reviewed change history exist.
- [ ] One immutable candidate has SBOM, scans, provenance, signatures, checksums, and compatibility manifest.
- [ ] Context/canary tests cannot overwrite caller data; actual context and all layers are inspected, positive controls fail, and installed wheel/sdist smoke runs outside the checkout.

### Rollout

- [ ] 3–7 day shadow is approved.
- [ ] At least three scheduled canary cycles are approved.
- [ ] RA-S, RA-C, and RA-P are retained for the exact candidate and required approval quorum.
- [ ] Cutover and first production cycle evidence are signed.
- [ ] Rollback is rehearsed and remains ready.
- [ ] A clean 72-hour observation window is approved.
- [ ] The exact CG6 digest, not a rebuild, is the production digest.
- [ ] Status remains CONTROLLED_PRODUCTION_OBSERVATION from RA-P/cutover until CG7, then and only then becomes production-ready.

The coordinator may change the release status to production-ready only after every checkbox and CG0–CG7 is supported by the evidence index and approvals.

Until an RA-P-authorized C7.4 cutover begins, the supported posture is **NO-GO / supervised non-production pilot**. From cutover through CG7, the only permitted production posture is **CONTROLLED_PRODUCTION_OBSERVATION** under RA-P stop/rollback thresholds. Only after CG7 is the posture production-ready.

---

## 16. Stop conditions

Stop the current packet or rollout immediately and reopen its finding when:

- a schema/database provenance or compatibility assumption is uncertain;
- a pre-migration/pre-restore backup or manifest cannot be verified;
- a lease owner, state transition, chunk acknowledgment, or external acceptance is uncertain;
- an ambiguous chunk would need automatic replay;
- a secret or raw sensitive payload appears in any artifact;
- an unwaived high/critical finding appears;
- a worker survives its deadline;
- coverage or a cumulative gate regresses;
- a target platform, ACL/mode, filesystem, architecture, egress, alert, backup, or restore check fails;
- a release digest/signature/evidence hash does not match;
- a shadow/canary content or safety threshold fails;
- the user/owner must make a decision that would materially change scope or safety.

Do not work around a stop condition by deleting evidence, weakening a test, increasing a hard limit without review, editing the database manually, rebuilding a candidate, or narrowing support silently.

---

## 17. Initial dispatch order

When implementation begins, use the reviewed working tree rather than resetting to the historical snapshot:

1. Coordinator fingerprints the existing commit and user changes, reconciles the current R/F ledger and historical receipts, and records implementation assumptions separately from unresolved owner decisions.
2. Dispatch P00 to Agent C with Agent B as reviewer; do not run the unsafe full suite first. Agent A prepares P01 read-only state/mutator/transition inventory concurrently.
3. Bootstrap the safe C6.1 harness, replay/reduce the September probes, and freeze the interfaces/ADRs required by the next packets. Existing good tests/code remain in place; behavioral evidence determines what needs repair.
4. Start A's P02/P03 foundations; C works on disjoint grammar/logging/input contracts; B prepares/reviews transport and hostile-content cases. Coordinator owns shared app integration and closes CG0 from actual evidence.
5. Integrate C1 with the accepted live inspector; finish the serial authority/migration/recovery/restore chain in Section 11. Re-run CG1 against final state semantics and close CG2 only when all ownership/durability/restore proofs pass.
6. Integrate P06 outbox/retry/target semantics, P07 operator truthfulness and P09 scheduler behavior under the frozen interfaces; B completes P08 in serialized content/transport subpackets. Re-run affected earlier gates after each structural change.
7. Complete C5 operations and both supported target protocols, then the full C6 CI/coverage/artifact/exact-candidate verification. Missing local Docker or target access remains explicitly blocked until a suitable environment supplies evidence.
8. Seek only the human operational/release decisions actually required by the existing RA-S/RA-C/RA-P contracts, with concrete digest-bound evidence ready for review. The plan edit itself does not authorize implementation on live data or rollout.
9. Continue bounded CL0–CL9 iterations and C7 observation until every mandatory current/historical obligation closes. Stop dependent actions on failed safety invariants; continue useful unrelated work.

No agent self-closes a task. The coordinator closes only from retained evidence and an independent review.
