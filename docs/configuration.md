# Configuration

Copy `.env.example` to `.env`. `.env` is runtime-only and must not be committed.

The JSON watchlist is validated strictly. Unknown top-level or nested keys, duplicate IDs, non-HTTPS feeds, invalid timezones, invalid `HH:MM` values, placeholder values, and out-of-range budgets fail before collection or state transition. `--config-show` prints the effective redacted JSON.

Important behavior:

- `lookback_days` is enforced after every collector returns; Google/GDELT time filters are only hints.
- `missing_date_policy=exclude` is the production default.
- `title_dedupe_days` limits title-only cross-run suppression; canonical URL suppression uses `url_retention_days`.
- `daily_min` is an objective, not permission to send irrelevant filler.
- A healthy source set with zero eligible stories sends one `completed_empty` coverage notice.
- An all-source failure enters durable same-day retry and alerts after retry exhaustion.

The `limits` block controls soft budgets. Hard ceilings are enforced by validation and cannot be disabled. The `network_policy` block permits same-host HTTPS redirects by default; cross-host redirects require an exact allowlist. The `retry_policy` block controls bounded exponential backoff. Set `MECO_DISABLE_RETRIES=1` only as an emergency kill switch.

Runtime variables:

- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are required for live delivery.
- `MECO_CONFIG` selects the JSON file.
- `STATE_DB` selects the local SQLite file.
- `LOG_LEVEL` and optional `LOG_FILE` control redacted JSON logs.

