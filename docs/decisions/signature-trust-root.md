# Signature trust root - PROPOSED (not in effect)

Status: PROPOSED - awaiting owner signature.
Drafted: 2026-09-09 (Asia/Jakarta) as an implementer draft, not an approval.

This document puts nothing into effect. The --require-signature gate in scripts/release-provenance.py stays fail-closed (signature:not_signed / signature:unverifiable) until the owner signs one option below AND a follow-up implementation PR wires it. The release decision stays NO-GO regardless; the other blockers (target evidence, approvals, 72-hour observation) are unaffected.

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

- Chosen option (A or B): __________
- Pinned verification identity or public-key path (as applicable): __________
- Owner name: __________
- Date: __________
- Approver signature (out of band): __________

Until this block is completed AND the wiring PR merges green, the gate reports signature:unverifiable and promotion stays blocked by design.
