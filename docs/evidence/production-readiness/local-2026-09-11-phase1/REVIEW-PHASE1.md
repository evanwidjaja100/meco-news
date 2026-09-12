# Phase 1 independent re-review CG1-CG5 record (2026-09-11, Asia/Jakarta)

Reviewer: Evan Widjaja, owner self-review. Accepted deviation from the
different-reviewer rule; independence is limited and recorded here.
Head: 952a72a on main, uncommitted work only, no branches or commits.
Captured: 2026-09-11T21:07:39+07:00.

Method: replayed the Sep-07 probe set plus the R01-R20 and C4.x corpus
against the current tree and attempted counterexamples with reviewer
probes RP1-RP5 (disposable dirs only, mocked transports, never .env).
Any probe failure would reopen its finding.

## Verdicts

- CG1 control plane: PASS local. Ruff clean on meco_news tests scripts;
  strict mypy clean on 22 modules; RP5 signature verify still fails
  closed unsigned; preflight and dry-run behavior unchanged.
- CG2 state and restore: PASS local with findings fixed. RP1 fresh-target
  restore sends 0x and holds chunks ambiguous; RP2 ambiguous restore
  refused; 50-process lease race yields exactly one winner with a
  consistent database afterwards.
- CG3 outbox and scheduler: PASS local with finding fixed. RP3 mismatch
  blocks 0x, audited reconcile resumes the original frozen payload 1x;
  partly-sent work stays blocked with no resume path by design.
- CG4 hostile input: PASS local with finding fixed. RP4 healthy sibling
  survives while the hostile item quarantines as invalid_unicode_scalar;
  XML-illegal C0 controls no longer abort the whole feed.
- CG5 metrics and backup operations: local matrices PASS. Target-host
  reports and second-operator restore evidence stay explicitly OPEN and
  belong to Phase 4. No target claim is made here.

## Findings found and fixed in this review

- F01 fresh-target restore replay: restore to a new path replayed
  acknowledged sends. Fix: quarantine unreconciled sendable chunks to
  ambiguous in meco_news/backup.py. Regression: tests/test_backup_coverage.py.
- F-C4.1 C0 abort: raw C0 bytes aborted the whole feed in expat, killing
  healthy siblings. Fix: map XML-illegal C0 controls to space before parse
  in meco_news/collectors.py. Regression: tests/test_c41_unicode_scalar.py.
- F03 no resume path: unsent destination-mismatch deliveries blocked
  forever with no supported command. Fix: StateStore.reconcile_delivery
  plus CLI --reconcile-delivery with audited reason and operator, wired
  through RunOptions and validation. Regression:
  tests/test_c52_delivery_reconcile.py. README documents the command.
- F04 register staleness: product edits shifted branch line numbers and
  the coverage gate failed closed on 6 entries. Fix: re-pinned app.py
  1009 to 1010 and 1266 to 1267, collectors.py 281 to 284, 630 to 638,
  721 to 729, 891 to 899 in scripts/critical-branches.json.
- F05 race publish lock: the 50-process lease test failed once under full
  suite load when a transient Windows file lock broke the runtime marker
  publish with PermissionError WinError 32. Fix: bounded
  PermissionError-only retry in _durable_replace, applied to all four
  marker publish sites in meco_news/maintenance.py. Semantics unchanged;
  persistent failure still raises. Regression: the CG2 race test itself,
  green standalone and in the final full suite.

## Final numbers on the reviewed tree

- Full suite: 641 passed, 1 skipped, 0 failed in 125.88s
  (phase1-pytest-final3.log).
- Coverage: statements 93.079 percent (6133/6589), branches 90.943
  percent (1968/2164), critical register 16/16 with nothing missing or
  unknown (coverage.json, coverage gate exit 0).
- Reviewer probes RP1-RP5: all PASS (reviewer-probes.log,
  reviewer-probes.json).
- Ruff and strict mypy: clean.

## Decision

Phase 1 local re-review is COMPLETE. Overall release decision stays
NO-GO until Phase 2 through Phase 5 close: signing and SBOM, exact
candidate container proof, target-host validation with a second operator,
rollout, and 72-hour observation.

Signed: Evan Widjaja, 2026-09-11 (owner self-review).
