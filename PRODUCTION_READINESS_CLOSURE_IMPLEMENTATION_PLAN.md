# MECO News Scraper — Production Readiness Closure Implementation Plan

Status: **Active remediation plan — all closure gates open**  
Created from implementation audit: **2026-08-24**  
Predecessor contract: [PRODUCTION_READINESS_IMPLEMENTATION_PLAN.md](PRODUCTION_READINESS_IMPLEMENTATION_PLAN.md)  
Audited workspace: imported, unversioned implementation snapshot  
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

## 2. Verified rebaseline

The 2026-08-24 audit established the following local facts:

| Area | Verified result | Readiness meaning |
|---|---|---|
| Existing tests | 23/23 collected tests passed | Useful smoke evidence; insufficient breadth and depth |
| Coverage | 59% combined line and branch coverage | Fails the mandatory 90% repository gate and critical-branch requirement |
| Static quality | Ruff 0.9.10 check/format and mypy 1.15 strict passed | Positive local evidence only |
| Packaging | Wheel and sdist built | Artifact existence is not metadata, reproducibility, provenance, or release proof |
| Compose/scripts | Compose syntax, PowerShell parsing, and shallow build-context text sentinel passed | Runtime, target-host, ACL, egress, layer, and multi-architecture proof absent |
| Docker runtime | Docker daemon unavailable during audit | Image runtime and platform gates remain open |
| Provenance | No usable Git repository/history in the snapshot | Source identity, review, branch protection, tags, and release traceability absent |
| Rollout | No shadow, canary, cutover, rollback-drill, or 72-hour evidence | Production rollout is prohibited |

The strongest release blockers are not cosmetic:

- preflight and health can report success while mandatory safety conditions fail;
- forced generation zero can be treated as absent and recreated;
- non-owners can perform delivery-state transitions;
- Telegram failures can be retried when acceptance is unknown;
- retry timing is not fully bounded;
- hostile Unicode, XML, URL, and deadline cases can bypass defenses or abort healthy work;
- historical migration compatibility and restore safety are not established;
- identity and deduplication are not fully source-independent, deterministic, or bounded;
- logs, metrics, alerts, backup retention, platform validation, CI, and release evidence do not satisfy their contracts.

All original Waves 0–8 and every predecessor Definition of Done checkbox therefore remain open.

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

### R0 — Attributable source and decisions

Every change is traceable to an authoritative baseline, approved contract, reviewed change, immutable artifact, and named approver.

Success measures:

- an authoritative remote/history is restored, or the owner signs an imported-snapshot declaration with pre-import provenance explicitly unknown;
- D1–D12 and the additional closure decisions in Section 6 are recorded;
- every finding, task, commit, review, test artifact, and release artifact is cross-linked;
- protected source and signed release tags exist before rollout.

### R1 — Truthful and side-effect-safe control plane

CLI, preflight, status, and health never report success when a mandatory condition fails and never cause an unintended side effect.

Success measures:

- invalid command combinations exit 2 before file logging, state, migration, backup, collection, scheduling, or Telegram initialization;
- dry-run is offline: it makes no remote network call and leaves state, WAL/SHM, logs, status, timestamps, leases, and schedulers unchanged; candidate evaluation uses explicitly supplied frozen local input;
- schema compatibility is exact and fails closed;
- status distinguishes latest terminal delivery from active delivery;
- every mandatory failed check produces ready=false or healthy=false and a nonzero exit.

### R2 — Authorized, immutable, recoverable state

Every runtime mutation is authorized inside its transaction, completed evidence remains immutable, and migration/restore never exposes a mixed state.

Success measures:

- all runtime mutators require a live lease capability and current maintenance fence checked in the same write transaction;
- each runtime process holds the shared execution guard for its lifetime; maintenance requires the exclusive guard and a distinct audited capability;
- force requires a prior completion and atomically creates N+1;
- historical migration checksums do not change when a future migration is added;
- legacy writers are fenced from migrated databases;
- migration and restore fault matrices yield either the verified old state or the verified new state, never a partial hybrid.

### R3 — Ambiguity-safe delivery and scheduler recovery

Telegram side effects are classified conservatively, immutable chunks resume safely, and retries are durable and bounded.

Success measures:

- confirmed chunks never auto-replay;
- acceptance-unknown outcomes become ambiguous and block later work;
- retry delay, attempts, and total elapsed time have independent hard caps;
- retry decisions and deadlines survive restart without recomputation drift; a backward wall-clock jump blocks automatic retry rather than extending its budget;
- daemon scheduling consumes typed outcomes, reloads the resolved config path safely, and never loses terminal/attention state.

### R4 — Hostile-input isolation and deterministic content

Untrusted feeds cannot escape resource, URL, parser, Unicode, or output boundaries, and equivalent frozen inputs always produce byte-identical selected content.

Success measures:

- a bad source or item cannot abort healthy-source processing;
- only valid Unicode scalar values cross validation boundaries;
- DTD/entity rejection works across supported encodings before expansion;
- multicast and every other forbidden destination class fail closed at URL, DNS, redirect, and deployment layers;
- source deadlines terminate and reap workers;
- title identity is source-independent, direct publishers beat aggregators, merge ordering is total, and fuzzy work is completely budgeted.

### R5 — Diagnosable, recoverable operations

Operators can see, alert on, back up, restore, and resolve every material state without manual SQL.

Success measures:

- JSON logs go to stdout, redact by key and value, remove prohibited controls/bidi, and emit exactly one terminal event per attempt kind;
- status/metrics expose stable outcome, source, retry, ambiguity, DB, lease, and dedup signals;
- alerts are tested through an independent channel;
- disk health fails below 1 GiB or 10% free;
- backup retention, restore drills, Linux/NAS, and Windows target gates meet approved RPO/RTO and security requirements.

### R6 — Reproducible, defended release

The same reviewed artifact passes test, security, platform, shadow, canary, and production promotion.

Success measures:

- line and branch coverage is at least 90% overall and 100% for listed critical decision branches;
- Linux and Windows Python matrices, fault/concurrency/migration tests, PowerShell tests, and multi-architecture image tests pass;
- metadata, Python support, license, dependencies, and transitive hash locks are consistent;
- one candidate is built from the protected tag, its exact context/layers/history/runtime are inspected, and only that same digest is signed;
- the protected tag/candidate produces SBOM, checksums, provenance, signature, compatibility manifest, and one immutable candidate digest.

### R7 — Evidence-led rollout and feedback

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
12. Lease expiry does not prove that an in-flight remote request failed; reclaim converts unresolved in-flight work to ambiguous atomically.
13. Retry delay, retry count, and total elapsed retry time are independently bounded and persisted; backward wall-clock movement cannot extend a budget and blocks automatic retry when elapsed time cannot be trusted.
14. Forced delivery requires an already completed generation, operator, reason, predecessor, and atomic N+1 allocation.
15. Old binaries and legacy tables cannot write a migrated database.
16. Restore requires the exclusive process-lifetime maintenance guard, proved-stopped schedulers/processes, no active lease or in-flight chunk, verified compatibility, and a preserved pre-restore artifact.
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
27. Logs go to stdout, use stable schemas/reason codes, recursively redact sensitive keys and values, and emit exactly one terminal event for every attempt kind.
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
| ADR-C07 | Force grammar plus terminal retry: same frozen failed generation only, allowlisted reason, no ambiguous/in-flight chunk, audited one-shot authorization, no automatic-budget reset | Generations and CLI |
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
| meco_news/storage.py and migrations | Agent A | C2.1 → C2.2 → C2.3 → C2.4 merges serially |
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

---

## 9. Recurring closure loops

These loops are mandatory work. They repeat until the associated findings stay closed through all later gates.

### CL0 — Finding-to-red-test loop

1. Select the highest-priority dependency-ready F-### row.
2. Reduce the audit probe to a deterministic checked-in reproducer.
3. Record the current failing output and intended contract.
4. Confirm the test fails for the correct reason.
5. Have a reviewer check that the reproducer does not merely encode the proposed implementation.
6. Move the finding to Red reproduced.

Exit: one stable red test or approved external protocol is linked to the finding.

### CL1 — Red/green/review loop

1. Implement the smallest coherent change that satisfies the invariant.
2. Run the focused tests.
3. Run the full offline suite, Ruff, strict typing, and branch coverage.
4. Run all applicable hostile/fault/concurrency tests.
5. Have another agent replay the audit and attempt a counterexample.
6. Address review findings through a new explicit packet.
7. Record commands, outputs, hashes, reviewer, and rollback.

Exit: focused and cumulative checks pass and independent review accepts the invariant.

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

- search for and restore the authoritative remote/history before initializing replacement history;
- if found, identify the source commit and import this snapshot as a reviewable diff;
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

- resolve predecessor D1–D12 and ADR-C01–ADR-C17;
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

#### Closure Gate CG0

- authoritative source status is recorded;
- decisions required by the next wave are approved;
- all 28 findings have issue, owner, red test/protocol, reviewer, and evidence path;
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

#### C1.2 — Make preflight exact, read-only, and fail-closed

Owner: Agent C  
Schema API owner/reviewer: Agent A  
Dependencies: ADR-C04 read-only inspection contract

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
- preflight creates no DB/WAL/SHM and changes no timestamp/bytes;
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
- probe calls leave state bytes and timestamps unchanged.

Exit:

- health truth table has 100% decision-branch coverage.

#### C1.4 — Establish safe structured logging and lifecycle identity

Owner: Agent C  
Reviewers: Agent B for hostile text/redaction; Agent A for attempt semantics  
Dependencies: ADR-C11; stable attempt kinds

Work:

- send JSON logs to stdout and reserve stderr for intentional human diagnostics if documented;
- define command, collection, delivery, and chunk attempt schemas with run/attempt/delivery/generation/chunk IDs;
- emit exactly one terminal event per attempt kind through one lifecycle finalizer;
- recursively redact sensitive keys and values in mappings, sequences, exceptions, and stack text;
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
- this gate accepts the fail-closed inspection contract against the audited baseline only; any later schema/signature change must rerun C1.2/C1.3 and CG1 evidence before CG2.

---

### Closure Wave 2 — Migration, state authority, generations, and restore

Production deployment allowed: **no**.

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
Dependencies: C2.1

Work:

- create and verify a pre-migration artifact before BEGIN with backup ID, SHA-256, UTC time, integrity, schema/app/source versions, and redacted config hash;
- abort before schema change if artifact creation/verification fails;
- implement the process-lifetime shared runtime/exclusive maintenance execution guard and a transaction-visible maintenance epoch/fence before migration can run;
- require a MaintenanceContext carrying the acquired exclusive guard/fence for migration and expose the fence-check API used by C2.3;
- provision every currently known downstream durable fact in the approved target schema, including force audit/predecessor, retry first/last/high-water/deadline/elapsed/manual-authorization fields, title-v2 identity, destination/send-option fingerprint, transition audit, and maintenance fencing;
- install tested BEFORE INSERT/UPDATE/DELETE fences on legacy runs/sent_articles tables or an equivalently proven old-writer barrier;
- migrate only under the exclusive maintenance guard;
- preserve the exact pre-migration database/application pair for rollback.

Red/fault tests:

- old v1 INSERT, UPDATE, DELETE, and INSERT OR REPLACE all fail after migration;
- a runtime holding the shared process guard blocks maintenance; exclusive maintenance blocks new runtime startup; stale-guard recovery follows ADR-C05 and cannot bypass a live process;
- inject failure after each SQL statement and before commit;
- backup/manifest corruption or failure leaves source hash/schema unchanged;
- restore the manifest and prove logical equivalence to the starting fixture;
- repeated public migrate under the verified exclusive guard is an audited no-op;
- run the matrix on Linux and Windows.

Exit:

- every crash yields fully verified old or new schema; no legacy write succeeds.

#### C2.3 — Require an authorized state capability for every mutation

Owner: Agent A  
Reviewer: Agent C; Agent B reviews untrusted stored fields  
Dependencies: C2.2 and ADR-C06

Work:

- introduce mandatory runtime LeaseContext or equivalent and consume the C2.2 MaintenanceContext;
- hold the shared execution guard for the complete runtime-process lifetime;
- check scope, owner, expiry, lease fence, and the current maintenance epoch/fence in the same BEGIN IMMEDIATE transaction as every mutation and heartbeat;
- cover delivery create/start, source results, prepare, retry, chunk begin/finish, failure, completion, reopen, and reconciliation;
- remove/private owner-optional and compatibility mutators such as direct complete/fail paths;
- make expired-owner reclaim and in_flight-to-ambiguous atomic;
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

#### C2.5 — Make restore exclusive, compatible, and automatically recoverable

Owner: Agent A for state safety; Agent C for operational wiring  
Reviewers: Agent B and Agent C  
Dependencies: C2.2, C2.3, ADR-C13

Work:

- acquire the C2.2 exclusive process-lifetime maintenance guard, advance the maintenance fence, and prove every existing runtime process has drained;
- refuse a live process/shared guard, active scheduler/delivery lease, or in-flight chunk; “no lease” alone is never treated as proof that the scheduler stopped;
- verify manifest, checksum, schema/application/source compatibility, and integrity;
- restore to a temporary path, handle WAL/SHM consistently, apply only supported migrations there, and run maintenance_verify with the live MaintenanceContext;
- preserve current target as a separately manifested artifact;
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
- every pre-swap failure preserves target hash; post-swap failure returns exactly to original hash;
- portable/local owner/mode/ACL preservation tests; actual target-account/filesystem assertions are retained for C5.3–C5.5 and C6.6.

Exit:

- the disposable automated restore-safety/fault matrix proves maintenance verification, automatic rollback, guard release, and a final normal read-only preflight exit 0; timed scheduling/retention, target owner/mode/ACL, second-operator execution, and RPO/RTO closure belong to C5.3/CG5.

#### Closure Gate CG2

- C2.1–C2.5 independently reviewed;
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

#### C3.2 — Bound and persist retry decisions

Owner: Agent B for policy; Agent A for persisted fields  
Reviewer: Agent C  
Dependencies: C3.1, CG2, and the C2.2 persisted retry/clock fields

Work:

- clamp retry_after to configured and non-disableable hard maximum;
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

#### C3.3 — Freeze outbox identity and audited reconciliation

Owner: Agent A  
Telegram reviewer: Agent B  
Dependencies: C2.4, C3.1, C3.2

Work:

- freeze item order, final HTML, raw payload hash, delivery/chunk ID, item-to-chunk mapping, and a non-secret DeliveryTargetSnapshot before send;
- bind the target snapshot to the bot's public identity, a versioned HMAC fingerprint of chat/thread destination, approved API endpoint class, parse mode, link-preview/send options, and delivery-policy/config hash; never persist the token, HMAC key, or raw secret destination;
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

#### C3.4 — Make scheduler outcomes, reload, and time deterministic

Owner: Agent C  
State reviewer: Agent A  
Dependencies: C3.2, C3.3, C1.3, ADR-C09

Work:

- return and consume typed success/skip/retry_wait/attention/terminal outcomes;
- resolve the effective config path once and reload that path each cycle, including default/MECO_CONFIG cases;
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
Dependencies: stable contracts from CG1–CG5

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
- create unique secret canaries in ignored root/subdirectory/config/env-like locations;
- implement inspection of actual context transfer/build input, BuildKit records where available, image filesystem, layer tar/history, metadata, and running container;
- assert no canary, VCS secret, evidence secret, env file, state DB, backup, or local config enters any layer;
- verify multi-stage cleanup cannot hide a secret in a lower layer;
- remove canaries after the test and prove repository cleanliness.

Exit:

- the harness detects deliberately included positive-control canaries and excludes ignored negative-control canaries on a disposable test build;
- C6.4 closes the verifier implementation only. F-024 remains open until C6.5 runs it during the exact build-once candidate creation and binds the report to that digest.

#### C6.5 — Create a signed build-once release candidate

Owner: Agent C and release approver  
Reviewers: security and operations approvers  
Dependencies: CG5, C6.1–C6.4, and C0.1

Work:

- from a clean protected signed source tag, place unique ignored canaries, capture the actual context, and build exactly one candidate image/artifact set;
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
- read back every F-### issue against test, review, scan, and artifact evidence;
- reject missing, stale, mismatched-digest, or self-approved evidence;
- record every allowed waiver with owner, compensation, expiry, and approval.

Exit:

- zero unwaived high/critical findings; all F-001–F-027 are closed or F-028 alone remains rollout-evidence-open.

#### Closure Gate CG6

- complete CI matrix passes on protected source;
- ≥90% overall and 100% required critical branch coverage pass;
- packaging/locks/Python/license are consistent;
- the actual context/layer/history/runtime report names the same digest that is signed and promoted;
- both Linux/NAS and Windows pass the full protocol with the exact signed candidate;
- SBOM, provenance, signature, checksum, compatibility, and scans bind one immutable digest;
- source, operations, security, and release approvers accept the candidate;
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
- all 28 finding rows are Closed;
- named business, operations, security, and release approvers sign the evidence index.

Only CG7 permits the production-ready status.

---

## 11. Parallel execution schedule

The coordinator dispatches only dependency-ready work and uses file locks from Section 8.

| Phase | Agent A | Agent B | Agent C | Coordinator serialization |
|---|---|---|---|---|
| C0 | State/schema audit reproducers and transition tables | Hostile corpus builders and expected reasons | Control/ops/release evidence protocols | Source/decisions/status; no production implementation |
| C1 | Read-only schema/status query interface and health state fixtures | Unicode/URL/XML corpus only, no production edits | C1.1, C1.2, C1.4; then integrate C1.3 | Own app.py and preflight integration |
| C2 | C2.1 → C2.2 → C2.3 → C2.4 serially | Review stored untrusted fields; prepare identity migration design | Restore operational requirements; C2.5 after state API freezes | Approve schema/state machine before retry work |
| C3 | C3.3 and persisted portion of C3.2 | C3.1 and transport-policy portion of C3.2 | C3.4 and C3.5 observability/docs | Integrate app.py/config; resolve interfaces |
| C4 | Identity/history migration support and state review | C4.1 → C4.6 serial by overlapping files; C4.7 | Prepare platform egress protocols, no claim of target pass | Freeze model/persistence boundary |
| C5 | Restore/backup drill and state review | Egress/security review and corpus replay | C5.1–C5.6, hardening and target harnesses | Coordinate production-like hosts/approvals |
| C6 | Concurrency/migration/recovery CI and evidence review | Security/property/fake-server CI and rescan | Package/container/CI/release pipeline | Run full matrix and issue readback |
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

---

## 12. Verification command contract

These are intended final commands. A command is not evidence until its exit code, tool versions, source commit, environment, output artifact, and hash are retained. Future scripts named here must be created and reviewed by their task; their current absence is not a pass.

### 12.1 Fast local loop

- python -m pytest -q followed by the focused test path
- python -m ruff format --check .
- python -m ruff check .
- python -m mypy meco_news

### 12.2 Coverage gate

- python -m coverage erase
- python -m coverage run --branch -m pytest
- python -m coverage report --show-missing --fail-under=90
- a reviewed critical-branch checker must enforce 100% on the decision sets listed in C6.1

### 12.3 Build/package gate

- python -m build
- install wheel into a clean environment and run version, CLI, preflight, dry-run, and status smoke
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

- Finding: F-###
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
| CG0 | Provenance/decisions/reproducers/evidence schema complete | Owner plus all domain reviewers | Control-plane implementation |
| CG1 | CLI/preflight/health/logging truthful and side-effect-safe | Agents A and B | State integration |
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
- [ ] No mandatory issue is hidden as a duplicate, documentation note, TODO, or untracked residual risk.
- [ ] Every original audit probe and every implementation-audit probe passes against the release candidate.

### Correctness and delivery

- [ ] Control-plane commands cannot cause unintended side effects.
- [ ] Preflight and health cannot be falsely green.
- [ ] Every state mutation has transaction-local authority.
- [ ] Migration, force, outbox, scheduler, and restore state machines pass their full tables and crash matrices.
- [ ] Confirmed chunks never auto-replay; ambiguity never auto-retries.
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

- [ ] Structured stdout logs, exactly-one terminal events, status, health, metrics, and independent alerts pass injected failures.
- [ ] Backup scheduling/retention/off-host receipts and restore drills meet approved RPO/RTO.
- [ ] Both Linux/NAS and Windows pass identity, storage, ACL/mode, egress, scheduler/signal, architecture, and recovery validation.
- [ ] A second operator successfully executes critical runbooks.

### Test and release

- [ ] Overall line and branch coverage is at least 90%; listed critical branches are 100%.
- [ ] Required Linux/Windows/Python/fault/concurrency/migration/PowerShell/container/multi-architecture CI passes.
- [ ] License, ownership, Python support, version, metadata, dependencies, and transitive hash locks are consistent and approved.
- [ ] Protected source/tag and reviewed change history exist.
- [ ] One immutable candidate has SBOM, scans, provenance, signatures, checksums, and compatibility manifest.

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

After this plan is accepted:

1. Coordinator executes C0.1/C0.2 and creates the F-### ledger.
2. Agent A receives a C0.3 packet for schema/state/force/lease/restore red fixtures only.
3. Agent B receives a C0.3 packet for Unicode/XML/URL/deadline/dedup/Telegram hostile fixtures only.
4. Agent C receives a C0.3 packet for CLI/preflight/health/logging/platform/release evidence protocols only.
5. Coordinator reviews all reproducers, freezes required ADRs, and closes CG0.
6. Agent C starts C1.1/C1.2/C1.4; Agent A supplies the read-only schema/status interface for C1.3; Agent B remains on disjoint hostile-corpus preparation.
7. After CG1, Agent A owns the serialized C2.1→C2.4 state chain; restore C2.5 begins only after the maintenance/state API freezes.
8. Only after CG2 do Agent A and Agent B integrate outbox/retry work, with Agent C implementing scheduler/health integration and the coordinator owning app.py.
9. Continue Closure Waves 3–7 and loops CL0–CL9 until zero mandatory findings remain.

No agent self-closes a task. The coordinator closes only from retained evidence and an independent review.
