# Phase 2 signing and SBOM wiring record (CG6, 2026-09-11, Asia/Jakarta)

Head: 952a72a on main, uncommitted work only, no branches or commits.
Captured: 2026-09-11T21:54:00+07:00.

Method: implement the owner-approved Option A (Sigstore keyless via GitHub
OIDC) wiring from docs/decisions/signature-trust-root.md, lock it with
regression tests, run the exact-candidate local build plus SBOM/provenance
flow, and prove the signature gate still fails closed everywhere except a
CI run that verifies bound bundles against the pinned identity plus Rekor.

## What changed

- scripts/release-provenance.py: create_provenance binds
  --signature-bundle ARTIFACT=BUNDLE entries (hash-pinned, rejected when
  the artifact is not a bound wheel/sdist/SBOM); verify checks every bound
  artifact plus the attached SBOM has a bundle, bytes match, then runs
  python -m sigstore verify identity per pair against the pinned issuer
  https://token.actions.githubusercontent.com and identity
  https://github.com/evanwidjaja100/meco-news/.github/workflows/ci.yml@refs/heads/main.
  Fail codes: not_signed, bundle_missing/mismatch/unknown,
  bundles_malformed, rejected:<path>, verifier_unavailable.
  A self-asserted signed state without bundles still reports unverifiable.
- .github/workflows/ci.yml package job: job-level permissions
  contents:read plus id-token:write (top-level stays contents:read);
  installs requirements-dev.lock plus requirements-build.lock; signs the
  wheel, sdist, and dist/sbom.json with the pinned sigstore; creates
  provenance with three --signature-bundle mappings; verifies with
  --require-sbom --require-signature.
- requirements-build.lock: pins sigstore 4.5.0 plus transitive deps
  (hash-download validated 2026-09-11: all wheels downloaded, no hash
  warnings; CI re-downloads this lock on every package run, so hash rot
  fails loudly there).
- tests/test_release_signature.py: 17 tests, stdlib unittest only,
  Rekor stubbed by monkeypatching _verify_bundle (real check runs only
  in the CI package job).
- tests/test_sbom_generation.py: merged component count re-pinned 16 to
  46 (build lock gained the signer subtree; PEP 503 normalization merges
  typing-extensions instead of duplicating it) with sigstore pinned to the
  excluded build scope at 4.5.0.
- docs/release.md, docs/decisions/signature-trust-root.md,
  docs/production-readiness-roadmap.md: signing wiring documented.

## Local verification on the exact candidate build

- python -m build --no-isolation: wheel plus sdist built
  (phase2 build log at repo root, removed after capture; see
  commands.txt).
- generate-sbom.py plus release-provenance.py create: SBOM attached,
  unsigned provenance verifies with --require-sbom (exit 0).
- Signature gate fails closed locally, as designed: unsigned verify with
  --require-signature reports signature:not_signed (exit 1); provenance
  created with dummy bundle bytes reports
  signature:verifier_unavailable (exit 1, no signer or Rekor outside CI).

## Honesty note

The OIDC/Rekor positive control exists only in the CI package job on
protected main. Local evidence proves wiring plus fail-closed behavior,
never a passing signature gate. CG6 closes on the first green package
run on main, not on this record.

## Final numbers on this tree

- Full pytest: 658 passed, 1 skipped, 0 failed
  (phase2-pytest-final.log, PYTEST-EXIT=0).
- Full unittest discover: 659 tests OK, skipped=1
  (phase2-unittest-final.err, UNITTEST-EXIT=0).
- Targeted: test_release_signature (17) plus test_sbom_generation (6)
  plus test_group8_gates (17): 40 tests OK.
- Ruff check on meco_news tests scripts: clean. Strict mypy on
  meco_news (22 modules): clean.

## Decision

Phase 2 is WIRED locally. Overall release decision stays NO-GO until
CG6 closes in CI plus Phase 3 through Phase 5: exact-candidate
container proof, target-host validation with a second operator,
rollout, and 72-hour observation.
