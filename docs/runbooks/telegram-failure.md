# Runbook: Telegram failure

Alert: `telegram_rate_limited`, `telegram_terminal`, or `telegram_ambiguous`.

For rate limits, inspect the durable `next_retry_at` and wait for the bounded retry. For terminal errors, verify chat membership, bot permissions, and credentials with `--preflight --online` or `--test-telegram`. A timeout/reset or malformed response is ambiguous by design.

Recovery: fix the external cause, keep one scheduler, and let safe retry resume. For a terminal chunk, use the release/incident decision before a new generation. Preserve delivery/chunk IDs, payload hash, response class, and Telegram-side evidence. Never automatically resend an ambiguous chunk.

