# Changelog

## 2.0.0 - production-readiness implementation

- Added typed strict configuration and safe CLI/preflight/status/health/backup modes.
- Added versioned SQLite migrations, leases, immutable generations, delivery attempts, durable outbox chunks, ambiguity resolution, and URL/title history.
- Added freshness enforcement, deterministic bounded deduplication, URL/redirect/SSRF policy, bounded parsers, source quarantine, and Telegram size/error invariants.
- Added structured redacted events, backup/restore tooling, hardened Docker/Compose and Windows scheduling, packaging metadata, CI scaffolding, and runbooks.

## 1.0.0

- Initial dependency-free collectors, ranker, Telegram formatter, and SQLite history store.

