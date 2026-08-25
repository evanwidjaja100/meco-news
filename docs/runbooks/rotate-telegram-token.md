# Runbook: rotate Telegram token

Create a replacement token through the approved Telegram owner, stop the scheduler, and preserve the incident/change record. Update only the runtime secret store or `.env` with mode `0600`; never put the token in source, config JSON, logs, status, backups, or a command line captured by task history.

Run `--preflight --online` and `--test-telegram` against the intended chat, then start exactly one scheduler and observe a canary cycle. Revoke the old token after successful verification. If the new token fails, restore the previous secret only while the old token remains valid and follow the Telegram-failure runbook. Preserve timestamps and approver identity.

