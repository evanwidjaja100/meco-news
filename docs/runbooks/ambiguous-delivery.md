# Runbook: ambiguous delivery

Alert: delivery state `needs_attention` or chunk state `ambiguous`.

Run `python -m meco_news --status --json` and record the delivery ID, chunk ID, payload hash, attempt, timestamp, and logs. Check Telegram manually using the visible delivery identifier and chat history. Do not run `--force`, delete the database, or send a copied message from a second process.

If Telegram confirms the chunk was accepted, resolve it with `--resolve-chunk ID --resolution sent --reason "..." --operator "..."`. If it was not accepted, resolve with `--resolution retry`. Verify the audit row, state transition, and absence of duplicate confirmed chunks. Escalate if evidence is inconclusive; keep the scheduler stopped or single-owner until resolved.

