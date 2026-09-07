**Production-readiness review — 7 September 2026**

**Verdict: NO-GO for unattended production.** The current working tree has useful safeguards and a passing test suite, but independently constructed failure scenarios still violate its delivery and recovery guarantees. The most serious problems can duplicate acknowledged messages or replace the active delivery database with an empty one after interrupted restoration. Fix these before relying on the service for production delivery.

This review covers the working tree based on commit `1e820fc968c3dbb3dfa35904ee01a3131f7dfc70`, including existing modified and untracked application files. It is not a review of that commit alone. Application version is 2.0.0 and schema version is 5. Existing application, configuration, tests, deployment files, and prior review documents were left unchanged. New files are review evidence only.

**Measured results**

| Check | Fresh result | What it establishes |
|---|---|---|
| Existing pytest suite | 509 passed in 143.08 seconds | Existing assertions pass on this Windows/Python environment |
| Statement coverage | 5,750 / 6,147 = 93.542% | Measured executed statements; not proof of correct behavior |
| Branch coverage | 1,829 / 2,032 = 90.010% | Measured branch coverage; barely above the configured floor |
| Critical-branch script | Reports 8 passed | Unreliable as a release gate; see F09 |
| Ruff | Passed for application, tests, and scripts | Configured static lint checks pass |
| Strict mypy | Passed for 22 application modules | Configured static type checks pass |
| Wheel and sdist | Both built from a disposable source copy | Packaging succeeds without relying on the existing build directory |
| Installed package smoke | Both passed in fresh environments outside the checkout | Package imports and supplied CLI smoke checks succeed |
| Compose syntax | Passed with `--no-interpolate --quiet` | Compose configuration parses |
| Pinned base image | Registry manifest resolves; includes Linux amd64 and arm64 | Digest exists; this is not a container runtime test |
| Actual Docker context/image verification | BLOCKED; required verifier exited 1 | Docker Desktop Linux daemon unavailable |
| New review probes | 12 observations reproduced | Concrete failures described below |

Local runtime: CPython 3.14.6, Windows build 19045, SQLite 3.50.4, OpenSSL 3.5.7. The default `python` lacks the development tools, so checks used the existing `.venv/Scripts/python.exe`. The registry identifies the pinned Python image as 3.14.7-slim-bookworm; its runtime libraries were not executed or scanned here.

The probes use disposable SQLite databases, synthetic credentials, mocked Telegram/collection calls, controlled clock advancement, and two real spawned-process tests. They make no Telegram requests. A reported duplicate send means a duplicate invocation of the mocked transport with an already acknowledged frozen payload, not an actual duplicate delivered to a real chat.

**Confirmed findings, ordered by production impact**

**F01 — P1: Restore reactivates already acknowledged frozen messages.**

Location: [backup.py:438](<D:/Desktop/test/meco news scraper/meco_news/backup.py:438>), [app.py:832](<D:/Desktop/test/meco news scraper/meco_news/app.py:832>).

Take an online backup after preparation but before sending, then finish sending the live delivery and restore the backup. Restore verifies the target has completed, copies the older pending outbox, and merges article history. It never reconciles the older chunk states with the target's acknowledgments. `run_once()` resumes that frozen outbox directly, bypassing selection/history filtering.

Observed: the restored delivery was `prepared`, the acknowledged article remained in history, and the next normal invocation called `send_html()` once with the original payload and completed it again. The added history merge therefore does not prevent replay. Restoring into a new path has even less target evidence available.

Required correction: reconcile delivery/chunk identity, payload hashes, acknowledgments, and completion records before activation. Require explicit operator reconciliation where evidence is insufficient, including unresolved work inside the backup. Acceptance must include backups captured in prepared, sending, retry, ambiguous, and completed states, followed by post-backup sends. An acknowledged chunk must never become automatically sendable again.

**F02 — P1: Process death during restore can cause silent creation of a fresh database.**

Location: [backup.py:446](<D:/Desktop/test/meco news scraper/meco_news/backup.py:446>), [storage.py:527](<D:/Desktop/test/meco news scraper/meco_news/storage.py:527>).

Restore first renames the existing target to its rollback filename, then later renames the staged replacement into place. Exception rollback helps ordinary Python failures, but cannot run after abrupt process termination. There is no persistent restore transaction that startup resumes or rejects.

Observed with a real child process terminated by `os._exit(73)` immediately after the first rename: the target disappeared and the rollback file remained. After advancing only the maintenance-marker clock beyond its grace period, ordinary `StateStore(target)` created a valid empty database with zero delivery records. Recoverable old bytes still exist, but the application has silently switched to a new authority and can lose duplicate protection.

Required correction: implement a recoverable restore publication protocol and make startup fail closed when recovery artifacts indicate interrupted activation. Preserve the previous file without creating an untracked missing-target window. Test actual process death at every publication boundary, not only exceptions that execute `finally` blocks.

**F03 — P1: A destination mismatch has no usable operator recovery path.**

Location: [app.py:583](<D:/Desktop/test/meco news scraper/meco_news/app.py:583>), [storage.py:1193](<D:/Desktop/test/meco news scraper/meco_news/storage.py:1193>), [storage.py:1877](<D:/Desktop/test/meco news scraper/meco_news/storage.py:1877>).

A changed chat ID correctly stops the frozen delivery and marks it `needs_attention`, but leaves its chunk `pending`. Subsequent runs return early for `needs_attention`, before comparing the destination again. The documented resolution command only accepts ambiguous or certain terminal chunks.

Observed: changing the destination blocked sending; restoring the exact original destination still returned `needs_attention`; an audited `resolve_chunk(..., 'retry')` failed with `InvalidTransition`. This active delivery also prevents normal progress on later dates. `--force` cannot repair it.

Required correction: add an audited delivery-level reconciliation command. Safely support resumption to the original frozen destination and explicitly define any permitted destination change for unsent versus partly sent deliveries. Preserve frozen content and acknowledged chunks. Demonstrate recovery entirely through supported commands.

**F04 — P1: Retry expiration is checked after the external attempt.**

Location: [app.py:589](<D:/Desktop/test/meco news scraper/meco_news/app.py:589>), [app.py:832](<D:/Desktop/test/meco news scraper/meco_news/app.py:832>), [storage.py:1370](<D:/Desktop/test/meco news scraper/meco_news/storage.py:1370>).

Resume checks `next_attempt_at`, then starts collection or sends a pending chunk. The persisted retry budget is evaluated only after collection fails or Telegram returns a retryable error. A successful response bypasses expiration entirely.

Observed: a chunk with a frozen 60-second budget and a previous attempt two minutes old reported `retry_budget_exhausted=True` before restart, yet `run_once()` sent it and returned `completed`.

Required correction: enforce the frozen attempt/elapsed budget before authorizing each automatic retry and before external I/O, with a durable terminal transition when exhausted. Keep explicit operator retry authorization separate. Also consistently use the frozen enabled/delay policy: current error handling still reads parts of the reloaded configuration.

**F05 — P1: An abandoned first delivery can remain healthy indefinitely.**

Location: [preflight.py:334](<D:/Desktop/test/meco news scraper/meco_news/preflight.py:334>), [preflight.py:346](<D:/Desktop/test/meco news scraper/meco_news/preflight.py:346>).

The overdue calculation uses the last success, or checks a first due window only when both active and latest deliveries are absent. An existing `collecting` delivery with no successful history skips both paths. Missing leases are also not treated as evidence of abandoned active work.

Observed: a delivery dated 1 January 2026, still `collecting`, with no lease and no successful delivery, returned `healthy=True` and no reasons in September. Disk sufficiency was held true in this probe to isolate the delivery-health logic. The alert evaluator receives no reason to notify an operator.

Required correction: evaluate the scheduled obligation and active-work age independently of whether a delivery row exists. Detect abandoned collecting/prepared/sending work and missing or expired ownership. Add before-first-success cases for process death and scheduler loss, and prove they produce an external alert.

**F06 — P2: Valid configuration can expire the sender lease during collection.**

Location: [config.py:487](<D:/Desktop/test/meco news scraper/meco_news/config.py:487>), [app.py:674](<D:/Desktop/test/meco news scraper/meco_news/app.py:674>), [app.py:817](<D:/Desktop/test/meco news scraper/meco_news/app.py:817>).

Validation compares the lease TTL with one source deadline, while collection can occupy the whole cycle deadline. The delivery lease is renewed before collection and once before each batch of due chunks, not continuously during collection or before every network call.

Observed: configuration accepted a 65-second lease and 120-second collection cycle. A simulated 70-second successful collection then failed with no send; the durable delivery remained `collecting` because the expired owner also could not record terminal failure. The default 180/120 settings avoid this particular timing, but the supported configuration range does not.

Required correction: maintain the delivery lease while bounded work is running, or validate a sufficient complete-cycle budget with margin. Renew/check before each chunk and define behavior when work overruns its authority. Test supported nondefault settings and multiple slow chunks.

**F07 — P2: Stale backup-lock recovery can admit two owners.**

Location: [operations.py:104](<D:/Desktop/test/meco news scraper/meco_news/operations.py:104>), [operations.py:117](<D:/Desktop/test/meco news scraper/meco_news/operations.py:117>).

`BackupJobLock` reads a stale marker, then unlinks whatever currently occupies the path. Another contender can replace the stale marker with a live lock between those steps; the first contender deletes that live lock and creates its own.

Observed using a deterministic interleaving at the liveness-check boundary: two lock objects both returned from acquisition with `_held=True`, while the path named only one owner. Catching `FileExistsError` does not cover this interleaving. The independent runtime/maintenance OS guard does not protect this separate backup-job lock.

Required correction: use an OS lock held for the entire backup job, with metadata informational rather than authoritative. Cover simultaneous stale recovery, partial marker publication, process death, and inaccessible process identity.

**F08 — P2: Spawned source workers bypass log redaction.**

Location: [collectors.py:55](<D:/Desktop/test/meco news scraper/meco_news/collectors.py:55>), [collectors.py:650](<D:/Desktop/test/meco news scraper/meco_news/collectors.py:650>).

The parent configures `JsonEventFormatter`, but the spawned worker entry point does not configure logging. An unexpected source error uses `LOGGER.exception()` inside that fresh process. Its traceback can go directly to stderr before the sanitized result reaches the parent. Python documents this unconfigured-logger fallback to stderr. [Python logging documentation](https://docs.python.org/3/library/logging.html#logging.lastResort).

Observed in a real spawned child: a synthetic `password=review-canary-92817` exception appeared intact in stderr, with a traceback. This demonstrates the missing redaction boundary; it is not evidence that an actual credential was exposed.

Required correction: initialize the redacting logger in every worker before source execution, or route sanitized structured diagnostics to the parent without raw worker traceback output. Verify real spawned-process stderr as well as parent log files and persisted source results.

**F09 — P2: The critical-branch gate accepts nonexistent evidence locations.**

Location: [coverage_gate.py:51](<D:/Desktop/test/meco news scraper/scripts/coverage_gate.py:51>), [critical-branches.json:5](<D:/Desktop/test/meco news scraper/scripts/critical-branches.json:5>).

The checker only asks whether a line or arc is listed as missing. It never requires the registered location to exist in measured executable lines/arcs. An empty register also passes this part of the gate.

Observed: invented line 999999 and arc `[999999, 1000000]` passed with no unknown locations. The real register is already stale: its scheduler entry points at the `ttl_seconds: int` function parameter, and its collector entry points at a blank line. Other descriptions point at unrelated statements. Consequently, the fresh “8 passed” result does not establish coverage of the claimed decisions.

Required correction: reject nonexistent/nonbranch locations, require every claimed arc to occur in the measured arc set, and require a nonempty validated register. Bind the register to source identities and actual behavioral tests. Refresh the current entries. Keep the independently measured aggregate coverage numbers, which are not invalidated by this defect.

**F10 — P2: `--require-signature` does not verify a signature.**

Location: [release-provenance.py:99](<D:/Desktop/test/meco news scraper/scripts/release-provenance.py:99>), [release-provenance.py:110](<D:/Desktop/test/meco news scraper/scripts/release-provenance.py:110>).

Verification trusts the JSON string `signature.state == 'signed'`. It has no cryptographic verification, trusted identity, or artifact-to-signature binding. It also permits an empty artifact list and does not verify the stored source/context relationship.

Observed: a document containing only schema version 1, an empty artifacts list, and `signature: {state: 'signed'}` passed `verify_provenance(..., require_signature=True)`.

Required correction: either implement verification of a supported signed attestation and trusted identity, or explicitly fail this gate until an external verifier supplies verified results. Require actual artifacts and verify their binding to the candidate and build-context evidence. A metadata state must not stand in for authentication.

**F11 — P2: Status reports a corrupt existing database as missing, with success.**

Location: [app.py:204](<D:/Desktop/test/meco news scraper/meco_news/app.py:204>), [app.py:1321](<D:/Desktop/test/meco news scraper/meco_news/app.py:1321>).

`_history_reader()` collapses open, schema, and SQLite failures into `None`; `_state_status()` interprets every such result as a missing database. The status CLI then exits zero.

Observed through the actual CLI subprocess: an existing file containing invalid SQLite bytes produced `state='missing'` and exit code 0. Operators cannot distinguish initial setup from damaged state using this command. Preflight and health have more careful classification, but status does not preserve it.

Required correction: reuse explicit inspection classifications, distinguish absence from unreadable/corrupt/incompatible state, and document meaningful exit codes. Test real subprocess JSON against missing, corrupt, incompatible, maintenance, and WAL-backed fixtures.

**F12 — P2: Malformed frozen input escapes the documented CLI error contract.**

Location: [app.py:375](<D:/Desktop/test/meco news scraper/meco_news/app.py:375>), [app.py:1572](<D:/Desktop/test/meco news scraper/meco_news/app.py:1572>).

Frozen-input validation raises `ConfigurationError` inside `run_once()`. `main()` catches that error for the configuration file but not for the later frozen-input load; the final run exception handler omits it.

Observed through a subprocess with `--dry-run --json`: a valid JSON file missing required frozen-input fields exited 1, printed a traceback, and produced empty stdout. README promises invalid input exits 2, and JSON mode should provide a parseable report. The probe did not create the requested log file or a state database; the failure is the error interface, not a demonstrated dry-run state write.

Required correction: validate frozen input at the CLI boundary and return the documented code and one structured error report. Test missing fields, invalid schema versions, unreadable files, invalid dates, and malformed JSON through the command entry point.

**Dependency and platform risks requiring explicit disposition**

The build backend is pinned to setuptools 75.8.0 in metadata and lockfiles. Upstream identifies versions below 78.1.1 as affected by CVE-2025-47273, a path traversal in the deprecated PackageIndex downloader. The reviewed build uses pinned packages and does not demonstrate reachability of that downloader, so this is a build-tool risk to upgrade or document, not a proven runtime exploit. Hash pinning does not fix known defects. [Setuptools advisory](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf).

The local interpreter reports SQLite 3.50.4. SQLite documents a rare WAL-reset corruption race affecting older versions when concurrent connections write/checkpoint; listed fixes include 3.50.7 and 3.51.3. This application uses WAL with separate scheduler and delivery connections. Verify a patched SQLite build on the selected target and record its source/version, or document the vendor backport. No occurrence of this upstream corruption bug was reproduced here. [SQLite WAL-reset documentation](https://www.sqlite.org/wal.html#walresetbug).

**Release evidence that remains absent or incomplete**

| Area | Required evidence before production |
|---|---|
| Candidate identity | Select a clean source commit containing the intended changes and bind tests, configuration, schema, wheel/image, and provenance to it. Existing untracked modules/scripts must be included deliberately. |
| Supported platforms | Successful remote CI for the declared Windows/Linux × Python 3.12/3.13/3.14 matrix. Local 3.14.6 success does not certify the other environments. |
| Container | Actual application build, canary context/layer inspection, read-only/non-root startup, volume permissions, health, memory/CPU behavior, and stop/restart using the candidate digest. CI currently builds an image but does not run the application container. |
| Network | Controlled target tests for DNS/redirect/private-address policy and host egress restrictions. Address pinning and disabled ambient proxies are present; target firewall behavior was not tested. |
| Delivery | Non-production Telegram canary, error-envelope/timeout recovery, multi-chunk interruption, and operator resolution against the real destination contract. No live messages were sent during review. |
| Scheduling | Real task/container registration, reboot, missed window, clock/date transition, shutdown during collection/send, and configuration-change behavior under the selected account. No scheduler was installed or changed. |
| Alerts | A scheduled health checker and independently delivered operator notification with failure and recovery receipts. The implemented JSONL sink writes locally; it is not an external alert route. |
| Recovery | Scheduled backups, verified off-host copies, retention, and a second-operator restore drill measuring RPO/RTO after the restore defects are corrected. A replication receipt does not itself upload a backup. |
| Observation | The repository's specified supervised rollout/72-hour record on the actual candidate, including actionable failure drills and review of digest relevance. |

Additional source-review limits should be resolved during those checks: the daemon retains old configuration after reload failure; sleeping schedules are not replanned on each 60-second wait; collection has no stop-event input to cancel promptly; and several exported metrics are placeholders (`dedup_pairs_total` and `dedup_similarity_total` are constant zero). These observations are not additional reproduced findings or proof of a target outage. Do not interpret those counters as measured deduplication work.

**What is already sound and worth retaining**

The code separates ingestion, ranking, rendering, state, and operations reasonably well. It now uses SQLite `synchronous=FULL`, WAL-aware live reads, explicit catalog migrations, OS runtime/maintenance guards, persisted send intent, conservative ambiguous-send handling, payload hashes, strict configuration, bounded source processes, address-pinned source connections, escaped HTML, and disabled ambient proxies. Non-root/read-only container configuration and digest pinning are present. The package tests genuinely run outside the checkout. These improvements are valuable; the result does not require replacing SQLite or introducing a broker.

The remaining weakness is behavioral coverage across boundaries. Some older tests inspect source text, while comments/dummy expressions can satisfy them without establishing behavior. The 12 successful fault probes against a fully passing suite are direct evidence that coverage percentages and test count alone do not establish readiness.

**Recommended completion order**

1. Repair restore replay and interrupted activation, with real restart tests and reconciliation of post-backup acknowledgment/completion evidence.
2. Repair destination reconciliation, pre-attempt retry enforcement, abandoned-delivery health, and lease lifetime behavior.
3. Fix backup locking, spawned-worker redaction, machine-readable errors, and the coverage/provenance verifiers. Turn these probes into assertions of the corrected outcomes.
4. Review runtime/build advisories, produce one candidate, pass remote CI and real container checks, then demonstrate external alerts, off-host recovery, and supervised delivery.

Production sign-off requires the five P1 findings closed, the applicable P2 corrections resolved, and demonstrated operation/recovery on the selected target. The existing readiness document's NO-GO verdict is appropriate, but its claims that the corresponding local behaviors are fully implemented should be revised in light of these reproductions.

**Evidence and reproducibility**

The [probe script](<D:/Desktop/test/meco news scraper/docs/reviews/2026-09-07-independent/reproduce.py>) and [observed results](<D:/Desktop/test/meco news scraper/docs/reviews/2026-09-07-independent/reproductions.json>) record all 12 findings. [Package build/smoke output](<D:/Desktop/test/meco news scraper/docs/reviews/2026-09-07-independent/artifacts.json>) records disposable artifact checksums and subprocess results; these artifacts were test builds, not promoted releases. [Coverage data](<D:/Desktop/test/meco news scraper/docs/reviews/2026-09-07-independent/coverage.json>), [pytest XML](<D:/Desktop/test/meco news scraper/docs/reviews/review-2026-09-07-pytest.xml>), [verification metadata](<D:/Desktop/test/meco news scraper/docs/reviews/2026-09-07-independent/verification.json>), and [source hashes](<D:/Desktop/test/meco news scraper/docs/reviews/2026-09-07-independent/source-manifest.sha256>) identify the measured environment and reviewed inputs.

Re-run the evidence probes from the repository root with `.venv/Scripts/python.exe docs/reviews/2026-09-07-independent/reproduce.py`. They intentionally report the observed behavior rather than counting defects as successful acceptance tests. The build helper is `verify_artifacts.py` in the same directory. No conclusion here guarantees the absence of other bugs, certifies an untested production host, or claims a real secret leak or real SQLite corruption incident.
