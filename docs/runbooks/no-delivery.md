# Runbook: no delivery

Alert: `no successful run for 26 hours`.

Symptoms: no `completed`/`completed_empty` event, stale status, or a missing task/container. Preserve status JSON, logs, run ID, host time, and database checksum. Run `python -m meco_news --status --json` and `--healthcheck --json`.

Decision: `already_running` means find the owner process; `retry_wait` means wait for its durable deadline; `needs_attention` means follow the Telegram ambiguity runbook; `failed_terminal` means inspect the terminal reason. Check disk space and preflight before restarting one scheduler.

Recovery: fix the cause, run `--preflight --json`, then invoke one `--run-if-due`/container cycle. Verify a terminal event and status transition. Roll back only with the release runbook. Escalate to the operational approver; never use `--force` to bypass unresolved work.

