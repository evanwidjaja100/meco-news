# Changelog

## Unreleased

- Windows CI hardening (PR #12): hermetic Docker gate via `MECO_DISABLE_DOCKER_PROBE` (can only force "blocked", never fake "passed"); 8.3 short-path canonicalization in path comparisons; `maintenance.runtime_dir()` resolves its input so short/long aliases share one marker dir; stdlib parser maps hostile-markup `AssertionError` to `ValueError`.
- Build-context verifier: probe image carries a CMD so `docker create` works on scratch; scan tarballs stay outside the probe build context; dockerignore matcher handles dotfiles and bare directory patterns; `sys.platform` guards for Windows-only APIs.
- Release evidence (PR #13): deterministic CycloneDX 1.5 SBOM generator from the hash-locked build/dev inputs (`scripts/generate-sbom.py`, fixed-timestamp reproducible); provenance attach/verify gains an independent `--require-sbom` flag (`sbom:missing` / `sbom:mismatch` / `sbom:not_attached` fail closed). The signature gate stays fail-closed (`signature:unverifiable`) until a trust root is approved and wired.
- Release decision remains NO-GO; supervised non-production pilot only (see `PRODUCTION_READINESS_CLOSURE_IMPLEMENTATION_PLAN.md`).
- Release process (PR #15): the offline gates in `docs/release.md` now generate the SBOM and verify attached provenance (`--sbom-report` / `--require-sbom`); the readiness status carries the verified 2026-09-09 delta.
- CI release evidence (PR #16): every `package` build generates `dist/sbom.json`, binds it into `dist/provenance.json`, and verifies with `--require-sbom`. Fixed the `scripts/generate-sbom.py` default timestamp to `datetime.now(UTC)` (codebase spelling; the `datetime.datetime.UTC` attribute does not exist on this matrix) with a regression test covering the no-`--timestamp` path.

- Ledger (PR #17/PR #18): PR #17 records the PR #15/PR #16 release-process entries; PR #18 records the CI build-context probe evidence in the readiness status. No behavior change; release decision remains NO-GO.
- Ledger (PR #19/PR #20): PR #19 records the PR #17/PR #18 entries and the post-merge green runs; PR #20 adds a PROPOSED signature trust-root decision draft (Options A/B) for owner signature with the --require-signature gate unchanged and fail-closed. No behavior change; release decision remains NO-GO.
- Determinism proof (PR #21): new tests/test_c45_determinism.py checks all 24 input permutations of a four-item merge fixture for identical fingerprints and stats, proves caller-owned items are unmutated and unaliased, and replays the proof in subprocesses under PYTHONHASHSEED 0 and 42. No behavior change.
- Ledger (PR #21): PR #21 merged to main @ 7da4c19; post-merge run 34303969200 completed success on protected main. No behavior change; release decision remains NO-GO.
- Worker-isolation proof (PR #22): new tests/test_c44_worker_isolation.py runs real spawn-context workers through _source_process_entry, proving a fast worker returns a typed SourceResult frame and exits 0, a hung worker is terminated and reaped with no survivors under mp.active_children(), and the next spawn succeeds. No behavior change.
- Ledger (PR #22): PR #22 merged to main @ 0eec0e5; post-merge run 34304755480 completed success on protected main. No behavior change; release decision remains NO-GO.
- Numeric-IP proof (PR #23): new tests/test_c42_numeric_ip_forms.py locks in the fail-closed handling of obfuscated loopback spellings (decimal, hex, octal) hermetically. The classifier fails closed on every unparseable form, and the resolution layer rejects a glibc-style 127.0.0.1 answer with ssrf_address_class for each form under stubbed getaddrinfo. No behavior change.
- Ledger (PR #23): PR #23 merged to main @ ef36379; post-merge run 34475045879 completed success on protected main. Full local pytest re-verified 609 tests (608 passed, 1 skipped, zero failures). No behavior change; release decision remains NO-GO.
- Ledger (PR #24): PR #24 merged to main @ c91789b; post-merge run 34475802001 completed success on protected main. No behavior change; release decision remains NO-GO.
- DTD proof (PR #25): new tests/test_c43_dtd_encodings.py converts the prior disposable DTD probe sentence into a checked-in C4.3 regression (DOCTYPE rejection with xml_dtd_disallowed across 8 encodings, entity-only variants, healthy-feed control). The test exposed a real ordering defect: C4.1 UTF-8 sanitization ran before the DTD scan and misaligned BOM-prefixed UTF-32 so it surfaced xml_parse_error; the scan now runs on the raw payload first in _parse_xml_once. Still fail-closed before and after; no behavior change beyond the accurate reason code. Re-pins the critical-branch register (collectors.py 625 to 627) for the resulting +2 line shift; the bounded-frame branch itself is untouched.
- Ledger (PR #28): PR #28 merged to main @ 4cc6232; post-merge run 34481004592 completed success on protected main. Full local pytest re-verified 617 tests (616 passed, 1 skipped, zero failures). No behavior change; release decision remains NO-GO.
- Scalar proof (PR #28): new tests/test_c41_unicode_scalar.py locks the C4.1 text boundary (bidi overrides/isolates/marks stripped from title/summary, lone surrogates in title/link/summary quarantine invalid_unicode_scalar with the healthy sibling surviving, emoji/combining preserved, C0/C1 stripped, illegal-XML controls fail closed). The proof exposed a real gap: bidi controls passed into stored output; _CONTROL_RE now strips U+200E/200F, U+202A-202E, U+2066-2069 (U+061C kept as legitimate text). Re-pins the critical-branch register (collectors.py 627 to 630) for the resulting +3 line shift; the bounded-frame branch itself is untouched.
- Ledger (PR #27): PR #27 merged to main @ 6dd99fc; post-merge run 34479713900 completed success on protected main. No behavior change; release decision remains NO-GO.
- Ledger (PR #26): PR #26 merged to main @ df8ac9e; post-merge run 34478574014 completed success on protected main. Full local pytest re-verified 612 tests (611 passed, 1 skipped, zero failures). No behavior change; release decision remains NO-GO.
- Ledger (PR #25): PR #25 merged to main @ 3cf2072; post-merge run 34477168065 completed success on protected main. Full local pytest re-verified 612 tests (611 passed, 1 skipped, zero failures). No behavior change; release decision remains NO-GO.

## 2.0.0 - production-readiness implementation

- Added typed strict configuration and safe CLI/preflight/status/health/backup modes.
- Added versioned SQLite migrations, leases, immutable generations, delivery attempts, durable outbox chunks, ambiguity resolution, and URL/title history.
- Added freshness enforcement, deterministic bounded deduplication, URL/redirect/SSRF policy, bounded parsers, source quarantine, and Telegram size/error invariants.
- Added structured redacted events, backup/restore tooling, hardened Docker/Compose and Windows scheduling, packaging metadata, CI scaffolding, and runbooks.

## 1.0.0

- Initial dependency-free collectors, ranker, Telegram formatter, and SQLite history store.

