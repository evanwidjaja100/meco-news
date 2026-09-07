**Production-readiness review — 6 September 2026**

**Recommendation: keep this version out of unattended production.** Preserve the small Python/SQLite architecture, but repair its delivery and recovery invariants before expanding functionality. The principal problems are correctness failures, not a lack of infrastructure or frameworks.

This review covers the current working tree based on commit `1e820fc968c3dbb3dfa35904ee01a3131f7dfc70`, including the uncommitted schema-v4 changes and tests present at review start. Application modules, configuration, tests, packaging, CI, deployment scripts, operational documentation, and existing readiness evidence were inspected. The two historical implementation plans were used as requirements/context, not as proof of completion. Existing scratch editing scripts and historical test logs are not release artifacts and were not executed. No application or existing test code was changed. Review artifacts are under this directory.

Evidence labels below distinguish **reproduced behavior**, **source-verified defects**, and **deployment validation still required**. Reproductions use disposable databases, synthetic stories, and mocked responses; they do not send Telegram messages or fetch public feeds. This review cannot certify the absence of every defect or substitute for target-host rollout evidence.

**What was verified**

| Check | Result |
|---|---|
| Runtime | Windows, Python 3.14.6 |
| Initial pytest suite | 379 passed; one failure: `tests/test_c06_context.py::TestContext::test_verify_with_context` |
| Safe repeat under coverage | 379 passed, one deliberately deselected unsafe context test, 62.31 seconds |
| Ruff | Passed for `meco_news` and `tests` |
| Strict mypy | Passed, 19 source files |
| Statement/branch combined coverage | **79%, fails the configured 90% floor**; see `coverage-summary.txt` |
| Critical module coverage | `app.py` 66%; `collectors.py` 55%; `network.py` 66%; `telegram.py` 71%; `storage.py` 86% |
| Docker sentinel, text mode | Passed, explicitly skipped actual context inspection |
| Docker build/runtime | Unverified: Docker CLI exists, but no running daemon was accessible; Docker config access was also restricted |
| Wheel/sdist build | Blocked locally: `.venv` lacks `setuptools`/`wheel`; `build --no-isolation` cannot import `setuptools.build_meta` |
| Live feeds, Telegram, Task Scheduler, Linux, power-loss drills | Not exercised |

Coverage is the measurement collected by the repository's current configuration. Spawned workers/subprocesses are not configured for combined coverage, so it is not a claim that every unmeasured line is never exercised. It does establish that the current measurement fails the declared floor; the historical 90–92% reports do not establish coverage for this working tree.

The initial context test reports a nonzero subprocess exit; its assertion does not include the subprocess stderr. Script inspection shows it attempts to create `.git/canary` in a directory protected by this session, consistent with an environmental failure. Inspection also exposed a more serious implementation problem: the script first writes to the checkout's real `.env` and later deletes it. There was no `.env` at the beginning of this review. A subsequent disposable-checkout reproduction confirmed that an existing `.env` would be lost. Do not solve this test failure merely by granting broader permissions.

**Release-blocking findings — P1**

**R01. An acknowledgment write failure can resend a message Telegram already accepted.**

Location: [app.py:556](<D:/Desktop/test/meco news scraper/meco_news/app.py:556>), [app.py:559](<D:/Desktop/test/meco news scraper/meco_news/app.py:559>), [storage.py:836](<D:/Desktop/test/meco news scraper/meco_news/storage.py:836>).

**Reproduced:** inject one `sqlite3.OperationalError` when persisting an accepted chunk. The first run returns `failed_terminal`, leaves the chunk `in_flight`, and releases the delivery lease. The next invocation creates a new generation and sends the same story again because no acknowledged history exists. The probe records two sends. Recovery currently requires an expired lease row, so a released/missing lease does not recover this orphaned `in_flight` chunk.

Recommendation: after any possible send, failures to save its result must preserve acceptance uncertainty. Never turn that work into an ordinary terminal generation that can be replaced automatically. Recover orphaned `in_flight` chunks whether the lease is expired or absent, keep later chunks blocked, and require reconciliation. Test every boundary before and after send, commit, rollback, and lease release, including transient database errors and process death.

**R02. Live read-only connections ignore committed WAL state.**

Location: [storage.py:423](<D:/Desktop/test/meco news scraper/meco_news/storage.py:423>), [inspection.py:98](<D:/Desktop/test/meco news scraper/meco_news/inspection.py:98>), [migrate.py:84](<D:/Desktop/test/meco news scraper/meco_news/migrate.py:84>).

**Reproduced:** checkpoint an empty current schema, keep a writer open, then commit a scheduler lease and delivery. The writer sees both; `StateStore(readonly=True)` sees neither. Preflight returns `ready=true`, exit 0, despite the live scheduler lease. This also undermines status, health, scheduler recovery/retry discovery, restore lease checks, and migration backup completeness after a crash leaves WAL content.

The cause is `mode=ro&immutable=1` on a database that is actively mutable. SQLite explicitly states that immutable mode disables locking/change detection and must not be used when the underlying file can change. [SQLite URI documentation](https://www.sqlite.org/uri.html#uriimmutable).

Recommendation: define separate APIs for a live SQLite snapshot and a truly immutable offline artifact. Use a WAL-aware read transaction for live state; if the operator requires absolutely no sidecar creation, report the read as unavailable when that guarantee cannot be met, or read a safely produced snapshot. Do not obtain apparent read-only safety by silently omitting committed state. Add tests with an open writer, committed uncheckpointed updates, crash leftovers, and concurrent checkpoints.

**R03. The maintenance guard is neither integrated with real runtime lifetimes nor atomically exclusive.**

Location: [maintenance.py:109](<D:/Desktop/test/meco news scraper/meco_news/maintenance.py:109>), [maintenance.py:289](<D:/Desktop/test/meco news scraper/meco_news/maintenance.py:289>), [storage.py:406](<D:/Desktop/test/meco news scraper/meco_news/storage.py:406>), [migrate.py:320](<D:/Desktop/test/meco news scraper/meco_news/migrate.py:320>).

**Reproduced:** maintenance acquires while a real `StateStore` remains open, and that store can still write. `RuntimeContext` is defined and tested but never acquired by `StateStore` or the application. A barrier-controlled race also lets two exclusive acquisitions both return successfully; the second marker replaces the first. Shared-runtime acquisition has the corresponding check/publish race. The migration runner checks the fence only at entry, not again at commit.

Recommendation: implement a shared/exclusive OS-backed lock with well-defined Windows and POSIX behavior, bind runtime holds to every writable connection's lifetime, and make all maintenance operations use that lock. Verify fencing inside transactional mutation paths and before migration commit. Handle stale holders and process identity without permitting a live holder's TTL to expire silently. Test separate real processes, simultaneous acquisition, abandoned locks, and long-lived daemon sessions.

The supported `--migrate --to-version` CLI still returns `maintenance_unavailable` unconditionally ([app.py:945](<D:/Desktop/test/meco news scraper/meco_news/app.py:945>)). This is a useful fail-closed restriction while the guard is unfinished, but it means the release has no working supported upgrade command. Wire it only after the exclusivity and recovery tests pass.

**R04. Restore can discard unresolved work and does not provide a complete atomic recovery protocol.**

Location: [backup.py:69](<D:/Desktop/test/meco news scraper/meco_news/backup.py:69>).

**Reproduced:** restore an empty backup over a target containing prepared, unsent work with no active lease. Restore succeeds and the target's delivery count becomes zero. The old file is retained, but the active application state has forgotten that work.

**Source-verified:** restore checks only lease timestamps, using the stale reader from R02; it does not acquire exclusive maintenance, reject unresolved target/source work, or verify exact schema compatibility. It accepts schema versions >=2 instead of a compatible catalog/signature. It renames the target away before replacing it, leaving a failure window with no target, and has no WAL/SHM recovery or rollback protocol around that swap. Migration manifests use a different checksum field from the regular restore API, so that recovery path also needs an explicit compatibility design.

Recommendation: share one exclusive backup/migration/restore protocol. Validate artifact identity, manifest shape, supported schema/checksums, and unresolved deliveries before activation. Stage and verify a WAL-aware replacement, retain recoverable evidence, publish atomically, and restore the previous target automatically if activation fails. Reconcile work sent after the backup's recovery point; restoring an older database alone cannot preserve Telegram delivery history.

**R05. Committed delivery intent and acknowledgments are not durable across host power loss.**

Location: [storage.py:440](<D:/Desktop/test/meco news scraper/meco_news/storage.py:440>).

**Source-verified, power loss not simulated:** all runtime connections use WAL plus `synchronous=NORMAL`. SQLite documents that committed transactions can roll back after power loss/system crash in this mode. If an `in_flight` marker disappears after Telegram received the request, the application can lose the evidence that should block a resend. [SQLite synchronous documentation](https://www.sqlite.org/pragma.html#pragma_synchronous).

Recommendation: use `synchronous=FULL` for the delivery state authority, verify the chosen setting and filesystem on the deployed host, and test recovery around both intent and acknowledgment commits. This service's daily message volume does not justify sacrificing the durability its delivery model needs. FULL still requires correct storage hardware and does not make Telegram exactly-once.

**R06. The retry budget restarts after terminal exhaustion.**

Location: [app.py:244](<D:/Desktop/test/meco news scraper/meco_news/app.py:244>), [app.py:280](<D:/Desktop/test/meco news scraper/meco_news/app.py:280>), [storage.py:624](<D:/Desktop/test/meco news scraper/meco_news/storage.py:624>).

**Reproduced:** four source failures exhaust generation 0; the next ordinary invocation creates generation 1 and starts at attempt 1 without an operator command. Windows invokes the command repeatedly, so the configured maximum is not a durable daily limit. The same terminal-generation selection logic applies to terminal Telegram failures.

Recommendation: make the date/generation terminal decision durable and require an explicit audited operator action to reopen it. Enforce attempt and elapsed-time budgets across restarts, plus a defined midnight policy. Keep scheduler/task retries from silently resetting application budgets.

**R07. State mutations remain callable without ownership, and outbox invariants are not enforced at the state boundary.**

Location: [storage.py:671](<D:/Desktop/test/meco news scraper/meco_news/storage.py:671>), [storage.py:711](<D:/Desktop/test/meco news scraper/meco_news/storage.py:711>), [storage.py:753](<D:/Desktop/test/meco news scraper/meco_news/storage.py:753>), [storage.py:939](<D:/Desktop/test/meco news scraper/meco_news/storage.py:939>), [storage.py:1175](<D:/Desktop/test/meco news scraper/meco_news/storage.py:1175>).

**Reproduced:** a second store calls `complete_run()` and completes another owner's delivery while the original owner still holds the lease. Several other mutators likewise accept no capability. The lease checks added to prepare/begin/finish/resolve are useful but incomplete.

**Source-verified:** `recover_expired_lease()` reads lease state before acquiring the write transaction and does not recheck it before changing all in-flight chunks. `begin_chunk_attempt()` does not enforce that earlier chunks are acknowledged, verify the payload against its stored hash, or bind acknowledgment to the recorded attempt's run ID. Public raw connections and compatibility wrappers make the state contract easy to bypass accidentally. This is an internal correctness boundary, not protection against an attacker with arbitrary database write access.

Recommendation: require a current owner/fence for every runtime mutation, recheck inside the transaction, bind attempts to their owners, enforce chunk ordering and immutable payload/item mapping, and remove or isolate legacy bypass APIs. Add multi-connection race tests rather than only passing the wrong owner string to already-guarded methods.

**R08. Telegram response validation can falsely acknowledge malformed responses.**

Location: [telegram.py:117](<D:/Desktop/test/meco news scraper/meco_news/telegram.py:117>), [telegram.py:163](<D:/Desktop/test/meco news scraper/meco_news/telegram.py:163>).

**Reproduced:** `{"ok":true,"result":{"message_id":null}}` is accepted as message ID `"None"`; `ok:"false"` is also accepted because it is a nonempty string. An empty JSON object is classified as a terminal rejection rather than an unknown response. A fabricated success can permanently suppress a story; fabricated certainty of rejection can enable replacement generations.

Recommendation: require an exact boolean success envelope, a valid positive integer message ID (excluding booleans), and the expected destination identity. Treat missing/invalid response fields after possible transmission as ambiguous. Contract-test the complete envelope/type matrix and acceptance uncertainty, not just HTTP status codes.

**R09. Control-plane checks can report healthy/ready when they are not.**

Location: [preflight.py:97](<D:/Desktop/test/meco news scraper/meco_news/preflight.py:97>), [preflight.py:332](<D:/Desktop/test/meco news scraper/meco_news/preflight.py:332>), [app.py:819](<D:/Desktop/test/meco news scraper/meco_news/app.py:819>).

**Reproduced independently of R02:** a failed WAL capability probe still returns `ready=true`, exit 0. A first delivery left collecting since 2000, with no lease or successful delivery, reports healthy. A corrupt SQLite file is rendered by status as `state:"missing"`. The `--status` branch returns exit 0 regardless.

**Source-verified:** the no-first-success due check only applies when there is no active/latest delivery. The `all_sources_failed_retry_exhausted` reason is assigned to every matching collection retry, including attempts with budget remaining. Operational consumers therefore receive both false green and false exhaustion signals.

Recommendation: calculate readiness from all mandatory checks, preserve missing/corrupt/incompatible/maintenance distinctions, and evaluate missed deadlines and stuck work even before the first success. Distinguish ordinary backoff from exhaustion. Test status and health against real state fixtures and a controlled clock, with multiple simultaneous failures.

**R10. The build-context verification script can destroy secrets and does not verify actual context/layers.**

Location: [verify-build-context.py:27](<D:/Desktop/test/meco news scraper/scripts/verify-build-context.py:27>), [verify-build-context.py:40](<D:/Desktop/test/meco news scraper/scripts/verify-build-context.py:40>), [verify-build-context.py:73](<D:/Desktop/test/meco news scraper/scripts/verify-build-context.py:73>).

**Reproduced in a disposable checkout:** a preexisting `.env` is overwritten and then deleted. Other fixed canary filenames can also overwrite files. A failed/unavailable Docker invocation falls back to a custom text matcher, and unmatched canaries merely produce warnings before the script prints success. Even a successful build log is not an archive/layer inventory.

Recommendation: test only an isolated build context with unique synthetic secrets; never use actual configuration paths in a developer checkout. Export and inspect the actual BuildKit context/final image and relevant layers/history. Fail the release gate if inspection cannot run or a canary survives. Keep optional local checks explicitly separate from required CI verification.

Also exclude backup directories, `*.bak`, and backup manifests from the actual context. The current `.dockerignore` excludes ordinary `.db` files but does not cover all artifacts produced by the backup/migration paths. Explicit Dockerfile COPY statements help protect final image contents; they do not prevent those files from entering a remote build context.

**Additional required corrections — P2**

| ID | Finding and evidence | Recommendation / acceptance check |
|---|---|---|
| R11 | **Dry-run is a no-op preview.** [app.py:213](<D:/Desktop/test/meco news scraper/meco_news/app.py:213>) creates an empty collection unconditionally. Reproduced: no collection call, zero candidates, success. README and help promise collection/ranking, while newer tests demand offline behavior without supplying input. | Decide the contract explicitly: an offline preview needs a supplied snapshot/fixture; a collection preview needs an explicit read-only network mode. Run the actual ranking/rendering pipeline and show source/freshness/omission reasons. Do not describe the current empty output as operational validation. |
| R12 | **Routine config edits strand frozen deliveries.** [app.py:34](<D:/Desktop/test/meco news scraper/meco_news/app.py:34>) includes the entire config hash in destination identity. Reproduced: changing only `minimum_score` makes a pending delivery `needs_attention`, with zero ambiguous chunks. The available resolve command accepts only ambiguous chunks. | Separate destination identity from source/ranking config. Resume frozen payloads after unrelated edits. Provide an audited path to resolve real target changes without raw SQL. |
| R13 | **Backup can manufacture an empty source database.** [backup.py:47](<D:/Desktop/test/meco news scraper/meco_news/backup.py:47>) opens the source writable. Reproduced: a typo/missing source creates a new database and a successful backup. Timestamp-only output naming can also reuse a destination within one second. | Require an existing valid source, use a safe snapshot, create artifacts exclusively with unique IDs, validate actual integrity, and atomically publish the manifest. Test missing paths, collisions, disk full, and interrupted publication. |
| R14 | **Non-feed XML is a successful source.** [collectors.py:265](<D:/Desktop/test/meco news scraper/meco_news/collectors.py:265>) accepts any well-formed XML structure. Reproduced: an XHTML error page becomes a successful zero-item feed. GDELT also accepts an object without an `articles` field as empty success. | Validate expected RSS/Atom/GDELT document shape, separate a valid empty feed from an error/challenge page, and report all-quarantined data explicitly. Otherwise a source outage can become a healthy empty-market notice. |
| R15 | **Fuzzy budget exhaustion resurrects already rejected duplicates.** [ranking.py:284](<D:/Desktop/test/meco news scraper/meco_news/ranking.py:284>) re-adds every candidate not retained, including known duplicates. Reproduced: a pair deduped to one with a normal budget becomes two with a one-comparison budget. | Track processed, confirmed-duplicate, and unprocessed candidates separately; retain only unprocessed/unmatched items after budget exhaustion. Add permutation and exhaustion-boundary behavior tests. |
| R16 | **Rendering has a supported-format mismatch and a continuation sizing gap.** [telegram.py:217](<D:/Desktop/test/meco news scraper/meco_news/telegram.py:217>) emits `&middot;`, outside the documented sendMessage HTML named entities. [telegram.py:291](<D:/Desktop/test/meco news scraper/meco_news/telegram.py:291>) fits a block before adding its continuation header, so near-limit valid blocks can make the whole build raise. A 900-unit build with a 715-character URL suffix reproduces the latter. | Emit literal `·` or a numeric entity. Reserve continuation-header space before fitting each block; omit an unrenderable item without aborting healthy siblings. Validate final parsed HTML and both size budgets. No claim is made here about the live API's response to `&middot;`; it was not sent. [Telegram HTML contract](https://core.telegram.org/bots/api#html-style). |
| R17 | **`--json` is not one JSON document.** [app.py:884](<D:/Desktop/test/meco news scraper/meco_news/app.py:884>) emits a startup JSON log before pretty-printing the report. Reproduced: `json.loads()` fails on `--config-show --json` stdout. | Specify a machine interface: reserve stdout for one report in JSON command mode, or deliberately offer documented JSONL and keep each record on one line. Test parsing actual subprocess stdout. |
| R18 | **Log redaction does not cover the event/message field.** [observability.py:138](<D:/Desktop/test/meco news scraper/meco_news/observability.py:138>) copies `record.getMessage()` or event name without redaction, while fields/stacks are scrubbed. Reproduced with a synthetic password canary in a normal logging message. | Use fixed event identifiers and scrub the human message as well as fields, including spawned-worker logging. Test all logger entry points. This is a demonstrated formatter gap, not evidence that a real credential was exposed. |
| R19 | **Collection deadlines are global despite their per-source name.** [collectors.py:691](<D:/Desktop/test/meco news scraper/meco_news/collectors.py:691>) gives all queued jobs one deadline. Ten default Google queries share two slots; slow early jobs can consume the entire budget before late queries start. The parent also uses blocking `recv()` after `poll()`, with no separately enforced receive deadline. | Define both whole-cycle and per-source budgets, record queue time versus execution time, use fair scheduling, and bound IPC receipt. Verify slow first jobs, large results, hung workers, and all-child cleanup with real spawned processes. This is a source-verified risk; no public-feed timing claim is made. |
| R20 | **Scheduler supervision/config reload are incomplete.** [app.py:660](<D:/Desktop/test/meco news scraper/meco_news/app.py:660>) reads and discards the typed outcome, reloads before planning a potentially day-long wait, then runs with that config when waking. [app.py:438](<D:/Desktop/test/meco news scraper/meco_news/app.py:438>) heartbeats once before a batch of chunks, not before each network call. No SIGTERM shutdown handler exists. | Revalidate configuration and lease/heartbeat state immediately before work; process outcomes explicitly; renew/check the delivery lease per chunk; add graceful stop and interrupted-send recovery. Use fake-clock behavioral tests plus real process termination tests. |

**Platform/security items that still require target validation**

- **SSRF defense is incomplete without enforced egress.** [network.py:78](<D:/Desktop/test/meco news scraper/meco_news/network.py:78>) resolves and validates addresses, then urllib resolves the hostname again. The approved addresses are not bound to the connection, and urllib's default opener can inherit proxies. Deployment documents acknowledge a required egress boundary, but Compose/scripts do not establish it. Bind connections to validated addresses with correct TLS hostname verification, or enforce and test destination restrictions outside the process; include DNS rebinding, redirects, proxies, IPv4/IPv6, loopback, and metadata endpoints. This is a source-verified gap, not a demonstrated exploit against an actual deployment.
- **Windows process liveness must use a query API.** [maintenance.py:213](<D:/Desktop/test/meco news scraper/meco_news/maintenance.py:213>) uses `os.kill(pid, 0)`. Windows signal semantics differ from POSIX and errors are treated as evidence of a live process. The disposable child remained alive in this environment; **process termination was not reproduced and is not reported as a confirmed defect**. Use a Windows process-handle/liveness query and test dead, reused, inaccessible, and long-lived PIDs. [Python os.kill documentation](https://docs.python.org/3/library/os.html#os.kill).
- **Package smoke testing is checkout-dependent.** CI installs a wheel but executes from the source checkout, where both `meco_news` and `config/watchlist.json` are present. Package discovery includes only `meco_news*`, and config is loaded relative to the current directory. Run the installed CLI from a clean unrelated directory with an explicitly provisioned config; test both wheel and sdist installation. Do not infer a package failure solely from the missing local build tools reported above.
- **Version support is contradictory.** Metadata/inspection accept Python 3.12–3.14, CI tests only 3.14, README/CONTRIBUTING recommend 3.12/3.13, and decisions identify 3.14 as the target. Pick the supported range and test every advertised runtime, or narrow metadata/docs to the validated one.
- **Release reproducibility is incomplete.** Tool files pin top-level versions but not transitive dependencies/hashes. The production image/base remain mutable tags. CI has no enforced coverage job, artifact checksums/SBOM/provenance generation, candidate signing/promotion, or runtime restore/shutdown tests. Add only the controls appropriate to the selected deployment, but make required checks real failures rather than narrative promises.
- **Operational work is documented but not implemented.** There is no scheduled verified backup/7-daily–4-weekly–12-monthly retention job, independent alert route, state-row retention operation, or external test that a missed digest reaches an operator. Docker health status alone is not notification or automatic recovery. Establish one monitored scheduler, owned alerts, periodic restore drills, bounded logs/state/backups, and measured recovery objectives.
- **The evidence ledger is stale and contradictory.** The readiness status cites 150 tests and 90% coverage; the CG1 close document cites 341 and 92% against schema v3; current code is schema v4 with 380 collected tests and the measurement above. The issue ledger still lists all findings open while other documents close portions. Keep one current release checklist generated from a clean candidate and link evidence to its exact source/config/schema hashes. Retire stale claims instead of adding another large parallel plan.

**Recommended implementation order**

| Stage | Work | Required exit evidence |
|---|---|---|
| 1 — Make delivery/recovery safe | R01–R08; replace immutable live reads, establish real ownership/maintenance, repair acknowledgment uncertainty, terminal retry budgets, and restore | No automatic resend after every tested uncertain-send failure; no non-owner mutation; one maintenance owner under contention; complete WAL-aware reads/backups; restore cannot lose unresolved work silently |
| 2 — Make operator tools truthful | R09–R14, R17–R18; useful dry-run input, coherent JSON/status, correct health and source classification | Actual command outputs parse and match seeded failure states; preview evaluates real fixture content; backup of a missing source fails; routine config edits do not strand frozen content |
| 3 — Make boundaries and CI meaningful | R10, R15–R16, R19–R20, egress and packaging checks | Behavioral regression tests, isolated canary inspection, configured coverage >=90% with critical send/recovery branches exercised, cross-platform/process tests, installed artifact tests from outside checkout |
| 4 — Prove the selected deployment | Immutable candidate, one scheduler, secret/ACL/egress checks, external monitoring, scheduled backups, operator restore drill | Evidence from the actual Windows account or Linux container/volume, including reboot, outage, disk pressure, termination and recovery; measured RPO/RTO and a functioning independent alert |
| 5 — Controlled rollout | Shadow fixture/live-source comparison, non-production Telegram canary, supervised rollout and rollback | No unexplained duplicate/missing messages; reviewed digest relevance and source coverage; successful second-operator recovery; at least the existing plan's 72-hour supervised observation, with enough scheduled cycles and injected failures to assess behavior |

Do not add Redis, a queue broker, Kubernetes, a web API, or a new database to solve these defects. A single monitored process and local SQLite are a reasonable fit for a daily digest. Dependencies should be chosen on their merits: a well-maintained HTTP/parser/locking library may reduce bespoke security/concurrency code, but changing libraries does not replace testing the delivery contract.

**What is worth retaining**

The repo already separates ingestion, ranking, Telegram rendering, state, and CLI reasonably well. Typed configuration, bound checks, source-process isolation, deterministic identity keys, outbox preparation, conservative transport ambiguity handling, checksummed forward migrations, non-root container settings, and incident runbooks are valuable foundations. Several new migration tests genuinely exercise rollback and legacy fence behavior. Preserve those strengths while replacing tests that merely search source text, permissive assertions that accept nearly any result, and dummy code inserted to satisfy textual checks.

My production sign-off would require all P1 findings closed with behavioral regressions, the applicable P2 corrections completed, a clean immutable candidate with matching evidence, and one demonstrated deployment/restore/alert cycle. Passing lint or accumulating more tests is insufficient without that evidence.

**Reproduction artifacts**

- `reproduce_findings.py`: isolated probes; inspect before running. Its JSON records the current incorrect behavior and is not an acceptance suite for the fixes.
- `reproductions.json`: observed outputs, including duplicate acknowledgment recovery, WAL invisibility, maintenance races, retry reset, and restore loss.
- `coverage-summary.txt`, `coverage.json`, `coverage-data`: the separately collected current coverage measurement; the existing root `.coverage` was not overwritten by this collection.
- `verification.txt`: commands, environment limitations, and scope notes.
- `source-manifest.sha256`: hashes of reviewed application/test/script/config/build inputs for identifying this working-tree snapshot.
