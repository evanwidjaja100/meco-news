# CG1 Closure Evidence — Fail-Closed Inspection Contract (C1.1–C1.4)

**Gate:** Closure Gate CG1 (`PRODUCTION_READINESS_CLOSURE_IMPLEMENTATION_PLAN.md` Section 7)
**Date:** 2026-09-06
**Owner / reviewer:** Evan Widjaja (solo maintainer; solo-review basis per CG0 precedent, ADR-C03 signed 2026-09-05)
**Branch:** `cg1-close` → `main`
**Production deployment allowed:** no (Wave 2+ required)

## Phase PRs (one per phase, independently reviewed via CI + diff review)

| Phase | PR | Merge commit | Scope |
|---|---|---|---|
| C1.0 | #3 | `23c4d3e` | ADR-C04 interim (v3 baseline) + ADR-C11 signed |
| C1.1 | #4 | `2d30ecc` | Orphan `--resolution`/`--reason`/`--operator` guard (F-002 companion), CLI matrix RED-then-GREEN |
| C1.2 | #5 | `724071d` | Classified read-only inspector + maintenance guard (`inspection.py`, `maintenance.py`) |
| C1.3 | #6 | `427d4ff` | Status/health separation, fail-closed health truth table |
| C1.4 | #7 | `4824411` | Safe structured logging: hardened `redact()`, `AttemptLifecycle` exactly-once finalizer |

Each PR: 5 CI checks green before `--merge`.

## Gate criteria mapping

- **C1.1–C1.4 independently reviewed:** PRs #3–#7, each with full CI (unit ubuntu+windows, quality, package, container) green; solo-maintainer diff review recorded here.
- **Original CLI, schema false-green, terminal-health, logging, redaction probes pass:** `tests/test_c03_control_red.py` (F-002 orphan CLI matrix, F-004 health false-green, F-020 log redaction) green; `tests/test_c12_inspector.py`, `tests/test_c13_health.py`, `tests/test_c14_logging.py` — 108 tests green.
- **Control-plane critical branches 100% covered:** `inspection.py` 100%, `maintenance.py` 100%, `observability.py` 100% (statements + branches).
- **All probes read-only where required:** C1.2 preflight creates no DB/WAL/SHM and changes no bytes/timestamps (RED-tested); C1.3 status/health leave state bytes and timestamps unchanged (RED-tested).
- **Full offline/static suite green:** 341 tests OK; `ruff check` clean; strict `mypy` clean (18 files); `--config-show` exit 0.

## Verification battery (run 2026-09-06, `.venv` Python 3.14.6, offline)

- `python -B -m unittest discover -s tests` → Ran 341 tests, OK
- `tests.test_c12_inspector + test_c13_health + test_c14_logging` → Ran 108 tests, OK
- `python -m ruff check meco_news tests` → All checks passed
- `python -m mypy meco_news` → Success, no issues in 18 source files
- `coverage report` → TOTAL 92% (gate: >= 90%); `inspection.py` / `maintenance.py` / `observability.py` 100%
- `python -B -m meco_news --config-show --json` → exit 0

Coverage deltas below 100% (`app.py` 94%, `preflight.py` 97%, others) are pre-existing non-CG1 branches, unchanged by CG1 phases; no CG1 control-plane branch is uncovered.

## Frozen fail-closed baseline (audited; changes invalidate this gate)

- Application version: `2.0.0` (`meco_news/__init__.py`)
- Current schema version: v3 (`meco_news/migrations.py`, 13 tables + 5 indexes per ADR-C04 interim)
- Config hash: `f6646c4f6905bbc5b7b43534a32951181719ea09fe4011593caa023ec85e6467`
- Inspection contract: missing / migration_required / compatible / newer_incompatible / malformed / corrupt, schema exit 5 (`inspection.py`)

**Boundary:** this gate accepts the fail-closed inspection contract against the audited baseline only; any later schema/signature change must rerun C1.2/C1.3 and CG1 evidence before CG2.

## Ledger linkage

- F-002 → C1.1 (closed by PR #4)
- F-003 → C1.2 (inspection half; migration half deferred to C2.1)
- F-004 → C1.3 (closed by PR #6)
- F-020 → C1.4 (logging half; metrics/alert half deferred to C5.1)

## Sign-off

- Owner: Evan Widjaja, 2026-09-06 — CG1 criteria verified against the frozen baseline above.