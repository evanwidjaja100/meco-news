# Monitoring and alerts

Live-mode event logs are JSON on stdout by default and can also be written to a rotating JSONL file. In `--json` and `--metrics` machine modes, diagnostics move to stderr so stdout contains exactly one JSON report. Human-readable output that is not a JSON report (dry-run previews, restore confirmations, validation errors) goes to stderr, so stdout stays JSON-parseable. Every run emits one terminal run_terminal event with a stable outcome, and every collection/chunk attempt emits exactly one attempt_terminal record through a single lifecycle finalizer, grouped by run, attempt, delivery, generation, and chunk IDs (a second finalize raises instead of logging twice). All logged and persisted error text passes through recursive redaction of tokens, credentials, URL userinfo/query strings, and control/bidi characters, with hostile fields capped; stable error class/reason codes are stored separately from the sanitized display text, so logs, state, and status never carry raw secrets, response bodies, or rejected URLs.

Every operator/query command owns one `command` attempt lifecycle and emits exactly one `attempt_terminal` record with its exit code: `config_shown`, `preflight_passed`/`preflight_failed`, `status_reported`, `healthy`/`unhealthy`, `metrics_exported`, `backup_created`/`backup_failed`, `restored`/`restore_failed`, `chunk_resolved`/`resolution_failed`, `migration_applied`/`migration_failed`/`unsupported_migration_target`, `telegram_test_succeeded`/`telegram_test_failed`, and `chats_discovered`/`no_chats`. Delivery and daemon paths keep their own run/attempt records and never finalize the command lifecycle.

State/history retention keeps attempt and source-observation rows for at least 90 days and article-identity rows for at least 365 days by default (`prune_history`); rows belonging to active, retrying, ambiguous, or otherwise unresolved work are never pruned, even when older than the window.

The daemon holds a separate `scheduler` lease and heartbeats it at least every 60 seconds while idle or delivering. The `delivery` lease remains the authoritative sender lock; both leases and the active chunk are visible in `--status --json`.

Useful commands:

```powershell
python -m meco_news --status --json
python -m meco_news --healthcheck --max-heartbeat-age 180 --json
```

`--status --json` exits 0 when state is `ok` (healthy) or `missing` (initial setup), and 1 when the database is corrupt, malformed, incompatible, or unreadable.

Alert policy:

- critical: no successful run for 26 hours (including a missed first window with zero history), state corruption/migration failure/incompatible schema, all sources failed after retries, or chunk retries exhausted past the configured budget;
- high: terminal or ambiguous Telegram delivery, or the maintenance guard held (`maintenance_in_progress`);
- warning: more than half of sources fail for two runs, heartbeat is older than three minutes, or state disk space is below 1 GiB/10%.

Treat `needs_attention` and `ambiguous` as operator work, not a reason to use `--force`. Preserve the run ID, delivery ID, chunk ID, status JSON, logs, and relevant Telegram evidence before resolving or rolling back.
