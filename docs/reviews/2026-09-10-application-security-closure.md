# Application-security closure report (C4.7 implementer evidence)

**Date:** 2026-09-10 (Asia/Jakarta), main @ e9f8558.
**Scope:** the C4.7 application-security packet only: adversarial corpus replay,
decision-branch checker coverage, and per-item isolation. This is implementer
evidence, not the independent review CG4 still requires. Overall release
decision stays **NO-GO**.

## 1. Original probe replay (CG4 list)

Every probe below is a checked-in regression, re-run green on this checkout
(23 passed across the six files listed; full suite 617 passed, 1 skipped):

| Probe | Checked-in regression |
|---|---|
| Lone surrogate in title/link/summary | tests/test_c41_unicode_scalar.py::test_lone_surrogate_in_each_field_quarantines_with_healthy_sibling; tests/test_c03_hostile_red.py::test_surrogate_in_title_does_not_abort, test_title_key_with_surrogate_does_not_raise |
| UTF-16 DTD/entity | tests/test_c03_hostile_red.py::test_utf16_dtd_is_rejected; tests/test_c43_dtd_encodings.py::test_doctype_rejected_in_every_scanned_encoding, test_entity_reference_rejected_without_doctype, test_healthy_feed_still_parses |
| MemoryError propagation | tests/test_c03_hostile_red.py::test_memory_error_not_swallowed; tests/test_collector_coverage.py::test_supervisor_handles_invalid_partial_memory_and_start_failure |
| Multicast / forbidden address class | tests/test_c03_hostile_red.py::test_multicast_rejected, test_private_still_blocked |
| Source deadline with no surviving worker | tests/test_c44_worker_isolation.py::test_spawned_hang_worker_terminates_reaps_and_next_run_succeeds |
| Identity stability and merge determinism | tests/test_c03_hostile_red.py::test_title_key_source_independent, test_dedup_permutation_dependence; tests/test_c45_determinism.py::test_all_24_permutations_agree, test_permutation_proof_passes_under_distinct_hash_seeds |
| Fuzzy budget accounting | tests/test_branch_closure.py::test_rank_quality_merge_and_selection_limits (zero budget exhausts closed, healthy items retained); test_c45 asserts exact comparison counts stay bounded |
| Final Telegram payload and omission isolation | tests/test_c46_omitted_history.py::test_omitted_fingerprint_absent_from_delivery_and_sent_history; tests/test_branch_floor_closure.py digest edge tests |
| Secret and hostile-markup redaction in logs | tests/test_c14_logging.py (token, bearer, userinfo, exception/stack, control stripping) |

## 2. Reviewed decision-branch checker

`scripts/coverage_gate.py` fails closed on fictitious lines, unmeasured arcs,
and an empty register (locked by tests/test_group8_gates.py, including
`test_shipped_register_names_measured_branch_statements`, which requires every
shipped entry to name an `if/elif/except/while/for` statement with proving tests).

Measured on the full-suite coverage run for this tree: statement 93.124%
(floor 90), branch 90.977% (floor 90), critical branches 16/16, gate `passed`.

| C4.7 decision set | Register entry | Proving tests |
|---|---|---|
| Unicode scalar/control policy | meco_news/models.py:68 | C4.1 scalar tests (quarantine plus healthy control) |
| URL/DNS/redirect classification | meco_news/urls.py:229 (forbidden resolved address); meco_news/urls.py:242 (cross-scheme redirect) | test_private_still_blocked, test_multicast_rejected; test_url_canonical_and_resolution_fail_closed |
| XML DTD/entity and parser limits | meco_news/collectors.py:281 (raw-payload scan before expansion) | C4.3 DTD tests (8 encodings, entity-only, healthy control) |
| MemoryError propagation | meco_news/collectors.py:891 (memory_error frame raises) | test_memory_error_not_swallowed, test_supervisor_handles_invalid_partial_memory_and_start_failure |
| Worker termination/reaping | meco_news/collectors.py:721; bounded frame meco_news/collectors.py:630 | C4.4 isolation tests (fast exit, hung terminate/reap/next-run) |
| Identity migration | meco_news/storage.py:319 (only completed legacy runs adopt) | test_runner_adopts_legacy_v1_and_preserves_rows, test_runner_adopts_failed_legacy_run_without_replay |
| Publisher classification | outcome-proof (see note) | test_c45_determinism.py (newer/longer aggregator loses to older direct publisher in all 24 permutations) |
| Deterministic merge/fuzzy budgets | meco_news/ranking.py:302 | test_rank_quality_merge_and_selection_limits, test_all_24_permutations_agree |
| Telegram omission/sizing | meco_news/telegram.py:469; envelope meco_news/telegram.py:204 | C4.6 omission test, first-item-oversized edge test |

Note on publisher classification: `_is_aggregator` (meco_news/ranking.py:59)
is branchless by design; the direct-over-aggregator preference is a total-order
sort key, so line-branch coverage cannot express its outcomes. Correctness is
enforced at the outcome level by the C4.5 proof instead. No class of decision
is left without a checked-in proof.

## 3. Bounds asserted by the corpus

- CPU/memory/time/work: framed IPC with `max_frame_bytes`, per-source and
  cycle deadlines, comparison/posting/pair budgets, and Telegram UTF-16/byte
  limits, each with an exhaustion test above.
- No raw hostile payload in logs or stored output: C4.1 sanitization at the
  model boundary, bidi/control stripping on render, and recursive redaction
  (R18) with positive leak controls.
- No entity access: DTD/entity bytes are rejected before any parser sees them.
- No surviving worker: C4.4 asserts no live descendants under
  `mp.active_children()` and a successful next spawn after every fault.

## 4. Residuals (not claimed here)

- Independent review of C4.1-C4.7 by an agent who did not implement the packet.
- Target-host evidence: live API contract, exact-candidate Docker binding,
  firewall/egress, scheduler registration, backup/restore on target hardware.
- Signature trust-root decision and wiring; 72-hour observation; human sign-offs.

Until those close, operate only in a supervised non-production chat.
