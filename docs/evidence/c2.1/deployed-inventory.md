# C2.1 Deployed Schema/Checksum Inventory (owner-approved imported baseline)

**Date:** 2026-09-06
**Task:** C2.1 (closure plan Section 7)
**Owner:** Evan Widjaja

## Finding

No production state database exists. The production-readiness program has not
authorized production deployment (CG0/CG1 gates), so there are no deployed
schemas or issued historical checksums to preserve beyond the code baseline.

Filesystem search (2026-09-06): the only SQLite state databases under this
repository are throwaway dev/test artifacts (`data/` is git-ignored; tests use
per-test temporary directories). No `*.db` file is tracked in git and no
backup manifest references a production database.

## Imported baseline (authoritative for compatibility assignment)

Source: `meco_news/migrations.py` (`CURRENT_SCHEMA_VERSION = 3`).

| Version | Shape | Canonical checksum (sha256 `version:description:sql`) |
|---|---|---|
| v1 prior (legacy) | `sent_articles` + `runs`, no `schema_migrations` ledger | `21cf6dc74642049a02fa31f91ca54a0465203f5f06f135f37489f8a5036245fa` |
| v2 intermediate | ledger rows 1-2, `deliveries` without `target_snapshot` | `042fe55f8661343be805014bcbfb6092c751ea0f73579341e41dcaf9749af0ae` |
| v3 current | ledger rows 1-3, `target_snapshot` on `deliveries`, 13 tables + 5 indexes (ADR-C04 interim) | `22e3ab2465d7e89c7480fc46c5b8307f2382de0f69f7cf317941efb7422d84d1` |

Compatibility assigned by the immutable catalog (`verify_catalog`) and the
C1.2 read-only inspector against exactly these bytes. Any future migration
must keep all three checksums unchanged (RED-tested) and any later
schema/signature change must rerun C1.2/C1.3 and CG1 evidence before CG2.

## Approval

- Owner: Evan Widjaja, 2026-09-06 — no deployed production schema exists; the imported baseline above is authoritative.