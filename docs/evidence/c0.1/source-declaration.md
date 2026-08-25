# MECO News Scraper — Source Provenance Declaration

**Status:** Imported snapshot — authoritative remote/history not found
**Audit snapshot date:** 2026-08-24
**Declaration date:** 2026-08-25
**Repository path:** D:\Desktop\test\meco news scraper
**Finding linkage:** F-001 (PRODUCTION_READINESS_CLOSURE_IMPLEMENTATION_PLAN.md:278)
**Task linkage:** C0.1 (closure plan Section 10)

## Provenance search performed

1. Checked `.git/` — absent before 2026-08-25 (Test-Path .git -> False, git 2.55.0.windows.2 verified).
2. Searched parent directories for `.git` — none found in snapshot.
3. No .github remote configuration, no prior commit history, no tags present at audit.

Result: No usable Git history/remote was recoverable from this filesystem snapshot. This search was performed locally on the Windows host before any git init.

## Declaration

This directory is treated as the **authoritative imported snapshot** for the production-readiness program. Pre-import provenance (original author history, prior remotes, prior protected branches) is **unknown** and must not be invented.

- The baseline manifest `docs/evidence/c0.1/baseline-manifest.sha256` records SHA-256 of every tracked file at import time (generated 2026-08-25, UTF-8, LF, sorted).
- The original 2026-08-24 audited snapshot is preserved as the initial commit `baseline: imported snapshot 2026-08-24 (pre-import provenance unknown)` on branch `main`.
- All subsequent changes must be reviewable as diffs from that commit. Unrelated changes must not silently enter the production-readiness program (plan 8.2).

## Required approvals before CG0

- Owner signature confirming this directory is the authoritative source or restoration of a discovered authoritative remote (plan C0.1).
- Named release approver + operations approver recorded in `docs/decisions/adr-index.json` (ADR-C03 / D2).
- Protected integration branch (`main`) with required reviews before merge — to be evidenced in `docs/evidence/c0.1/branch-protection.json` after remote is created.

## Rolling back / stopping

- Never fabricate history or rewrite an authoritative remote if one is later discovered (C0.1 rollback note).
- If snapshot authority is disputed, stop release work.

## Signatures

- Coordinator: ______________________ Date: __________
- Owner: ______________________ Date: __________ (required before CG0)
- Release approver: ______________________ Date: __________
