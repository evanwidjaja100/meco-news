# MECO News Scraper — Issue Ledger for Production Readiness (F-001..F-028)

Generated: 2026-08-25
Source declaration: docs/evidence/c0.1/source-declaration.md
Baseline manifest: docs/evidence/c0.1/baseline-manifest.sha256
Branch protection intent: docs/evidence/c0.1/branch-protection.json
Closure plan: PRODUCTION_READINESS_CLOSURE_IMPLEMENTATION_PLAN.md Section 7
Predecessor plan: PRODUCTION_READINESS_IMPLEMENTATION_PLAN.md Sections 7-14

All 28 findings are mandatory. No waiver may close correctness/ambiguity/schema/lease/secret/restore invariants.

| Finding | Closure task(s) | Lifecycle | Red test / protocol | Owner | Reviewer |
|---|---|---|---|---|---|
| F-001 | C0.1, C0.2, C6.5 | Open | source-declaration + manifest + CODEOWNERS | Coordinator | owner/release approver |
| F-002 | C1.1 | Open | tests/test_c03_control_red.py CLI matrix | Agent C | Agent A |
| F-003 | C1.2, C2.1 | Open | tests/test_c03_state_red.py N-1/N/N+1 | Agent C/A | Agent A |
| F-004 | C1.3, C5.1 | Open | tests/test_c03_control_red.py health false-green | Agent C | Agent A |
| F-005 | C2.4 | Open | tests/test_c03_state_red.py gen-zero | Agent A | Coordinator |
| F-006 | C2.1, C2.2 | Open | tests/test_c03_state_red.py immutable catalog + legacy fence | Agent A | Agent C |
| F-007 | C2.3, C3.3 | Open | tests/test_c03_state_red.py non-owner mutator | Agent A | Agent C |
| F-008 | C2.2, C2.3, C2.5, C5.3 | Open | tests/test_c03_state_red.py restore active-work refusal | Agent A/C | Agent B/C |
| F-009 | C3.1 | Open | fake Telegram stage×envelope matrix | Agent B | Agent A |
| F-010 | C3.2, C1.3 | Open | delay/attempt/elapsed/clock restart | Agent B/A | Agent C |
| F-011 | C2.3, C2.4, C3.3 | Open | kill-point + mapping mismatch blocking | Agent A | Agent B |
| F-012 | C3.4 | Open | scheduler fake-clock + reload matrix | Agent C | Agent A |
| F-013 | C3.5, C1.3 | Open | zero/outage/degraded/dry table | Agent A/C | Agent C |
| F-014 | C4.1, C4.6 | Open | lone surrogate corpus whole pipeline | Agent B | Agent C/A |
| F-015 | C4.3, C4.4 | Open | multi-encoding DTD/entity corpus | Agent B | Agent A |
| F-016 | C4.2, C5.4, C5.5, C6.6 | Open | address/DNS/redirect + egress protocol | Agent B | Agent C |
| F-017 | C4.4, C2.3, C3.4 | Open | spawned worker deadline reap | Agent B | Agent A |
| F-018 | C4.5 | Open | identity v2 + permutation + fuzzy counters | Agent B/A | Coordinator |
| F-019 | C4.6 | Open | Telegram sizing property suite | Agent B | Agent A/C |
| F-020 | C1.4, C5.1 | Open | log canary + exactly-one terminal | Agent C | Agent B/A |
| F-021 | C5.1, C5.2 | Open | metrics + alert injected matrix | Agent C | Agent A |
| F-022 | C5.3 | Open | backup 7/4/12 + RPO/RTO drill | Agent C/A | second operator |
| F-023 | C5.4, C5.5, C6.6 | Open | target-host evidence reports | Agent C | Agent B |
| F-024 | C6.4, C6.5 | Open | secret canary context/layers/history | Agent C | Agent B |
| F-025 | C4.7, C6.1, C6.2 | Open | ≥90% + 100% critical branches + hostile corpus | Coordinator | outside domain |
| F-026 | C6.3, C6.5 | Open | hash locks + signed bundle | Agent C | A/B |
| F-027 | C0.3, C5.6 | Open | honest status + second-operator runbook drill | Agent C | independent operator |
| F-028 | C7.1, C7.2, C7.3, C7.4, C7.5 | Open | RA-S/RA-C/RA-P + shadow/canary/72h evidence | Coordinator | business/ops/security/release |
