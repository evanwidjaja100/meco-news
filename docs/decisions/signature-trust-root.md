# Signature trust root - APPROVED Option A (wired; CI positive control pending)

Status: APPROVED by owner 2026-09-11 (Asia/Jakarta) - Option A (Sigstore keyless); wired 2026-09-11 (uncommitted Phase 2). First green CI `package` run on `main` is still required as the Rekor positive control before any promotion.
Drafted: 2026-09-09 (Asia/Jakarta) as an implementer draft, not an approval.
Approved: 2026-09-11 (Asia/Jakarta) - owner chose Option A; expected verification identity pinned in the signature block below; wiring PR pending.

This document now records the owner decision (Option A, 2026-09-11); wiring is implemented (uncommitted Phase 2). The --require-signature gate in scripts/release-provenance.py stays fail-closed (signature:not_signed / signature:unverifiable / signature:verifier_unavailable) everywhere except a CI run that verifies bound bundles against the pinned identity plus Rekor. The release decision stays NO-GO regardless; the other blockers (target evidence, approvals, 72-hour observation) are unaffected.

## Problem

docs/release.md promotion requires externally verified attestation, but the repository defines no signing keys or attestation format, so a signature.state of signed in the JSON is never accepted as authentication. A trust root decision is required before any signature-gated promotion can pass.

## Option A - Sigstore keyless via GitHub OIDC (recommended)

- Signing happens in the package job of .github/workflows/ci.yml with a minimal id-token: write permission scoped to that job. The top-level workflow permission stays contents: read.
- Fulcio issues a short-lived certificate bound to the CI identity. Artifacts (wheel, sdist, dist/sbom.json) are signed with sigstore-python, whose pinned hash is added to requirements-build.lock. No long-lived secret is stored anywhere, which preserves the repository no-secrets posture.
- Signatures are logged to the Rekor transparency log. Verification pins the expected identity (repository URI, workflow path, git ref) plus the Rekor entry. These expected values are recorded out of band by the release approver, never self-asserted by the artifact.
- scripts/release-provenance.py gains an --attestation-report input consumed only by an external verifier step. The standing rule that a self-asserted signed state yields signature:unverifiable is kept.

## Option B - Offline cosign key in owner custody

- The owner generates a cosign key pair offline and keeps the private half in owner custody, with the custody location documented out of band. The public half is committed at a well-known path and pinned by hash in the release record.
- CI never signs; it only verifies with the committed public key. Release signing is a manual owner step whose output is recorded out of band before promotion.
- Key rotation and compromise handling follow a dated owner-signed statement. Rotation never weakens the gate in between.

## Explicit non-options

- Accepting a self-asserted signed state as authentication.
- Committing private key material or shared secrets to the repository.
- Long-lived GPG keys without a documented custody and rotation plan.

## Why Option A fits this repository

CI actions are already hash-pinned, and provenance plus SBOM are already generated per build in the package job, so keyless signing adds no secret custody. Option B fits only if the owner requires signatures producible without a transparency log or network dependency at signing time.

## Owner signature block

- Chosen option (A or B): A
- Pinned verification identity or public-key path (as applicable): Sigstore keyless - expected identity repository evanwidjaja100/meco-news, workflow .github/workflows/ci.yml, ref refs/heads/main; Rekor entry required (to be enforced by the wiring PR)
- Owner name: Evan Widjaja
- Date: 2026-09-11
- Approver signature (out of band): owner approval in chat 2026-09-11 (Asia/Jakarta): Option A, RPO 24h, RTO 60m, alerts to telegram; proceed with phase 0

The decision block is completed; until the wiring merges green on protected main, promotion stays blocked by design. Local runs prove fail-closed behavior only; the OIDC positive control exists solely in the CI package job.
