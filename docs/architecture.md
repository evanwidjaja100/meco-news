# Architecture

MECO News is a single-scheduler service. A CLI invocation or daemon validates the typed configuration, acquires the `delivery` lease, collects bounded source metadata, applies one UTC freshness policy, ranks and deduplicates deterministically, then freezes an immutable delivery and outbox chunks in SQLite.

The sender marks one chunk `in_flight` in a short transaction before calling Telegram. A confirmed response marks that chunk `sent` and records its article history. A retryable rejection enters `retry_wait`; an acceptance-unknown transport result enters `ambiguous` and changes the delivery to `needs_attention`. Ambiguous chunks are never automatically resent. Use `--resolve-chunk` only after an operator reconciles Telegram.

SQLite uses WAL, foreign keys, a busy timeout, UTC timestamps, effective `synchronous=FULL` on authoritative writers, checksummed forward-only migrations, and online backup. No transaction is held across collection or Telegram. The legacy `sent_articles`/`runs` schema is adopted through the immutable catalog into current schema version 5 with a uniquely reserved, manifest-last pre-migration backup. Migration v4 additionally installs old-writer fence triggers that reject direct writes to those legacy tables; migration and restore run only under the exclusive maintenance capability.

Normal runs skip a completed Jakarta calendar date. `--force` creates a new generation after completion but still excludes acknowledged URL/title history. It is not a replay command. Daemon cycles reload and validate configuration before planning the next due time; frozen deliveries do not change when the source configuration changes.

The application deliberately uses the standard library for the runtime boundary. This keeps the deployed dependency surface empty while retaining typed dataclasses, bounded HTTP/XML/JSON processing, explicit URL/redirect policy, framed bounded worker IPC, and redacted JSONL observability. Offline dry-run consumes only a validated frozen-input file and never opens the live state database.
