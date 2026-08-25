# Architecture

MECO News is a single-scheduler service. A CLI invocation or daemon validates the typed configuration, acquires the `delivery` lease, collects bounded source metadata, applies one UTC freshness policy, ranks and deduplicates deterministically, then freezes an immutable delivery and outbox chunks in SQLite.

The sender marks one chunk `in_flight` in a short transaction before calling Telegram. A confirmed response marks that chunk `sent` and records its article history. A retryable rejection enters `retry_wait`; an acceptance-unknown transport result enters `ambiguous` and changes the delivery to `needs_attention`. Ambiguous chunks are never automatically resent. Use `--resolve-chunk` only after an operator reconciles Telegram.

SQLite uses WAL, foreign keys, a busy timeout, UTC timestamps, checksummed forward-only migrations, and online backup. No transaction is held across collection or Telegram. The legacy `sent_articles`/`runs` schema is adopted into schema version 2 with a pre-migration backup.

Normal runs skip a completed Jakarta calendar date. `--force` creates a new generation after completion but still excludes acknowledged URL/title history. It is not a replay command. Daemon cycles reload and validate configuration before planning the next due time; frozen deliveries do not change when the source configuration changes.

The application deliberately uses the standard library for the runtime boundary. This keeps the deployed dependency surface empty while retaining typed dataclasses, bounded HTTP/XML/JSON processing, and explicit URL/redirect policy.

