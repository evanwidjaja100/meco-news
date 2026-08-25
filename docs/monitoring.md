# Monitoring and alerts

Logs are JSON to stdout and optionally to a rotating JSONL file. Every attempt emits one terminal `run_terminal` event with a stable outcome. Fields are bounded and redact Telegram tokens, credentials, query strings, raw response bodies, and rejected URLs.

The daemon holds a separate `scheduler` lease and heartbeats it at least every 60 seconds while idle or delivering. The `delivery` lease remains the authoritative sender lock; both leases and the active chunk are visible in `--status --json`.

Useful commands:

```powershell
python -m meco_news --status --json
python -m meco_news --healthcheck --max-heartbeat-age 180 --json
```

Alert policy:

- critical: no successful run for 26 hours, state corruption/migration failure, or all sources failed after retries;
- high: terminal or ambiguous Telegram delivery;
- warning: more than half of sources fail for two runs, heartbeat is older than three minutes, or state disk space is below 1 GiB/10%.

Treat `needs_attention` and `ambiguous` as operator work, not a reason to use `--force`. Preserve the run ID, delivery ID, chunk ID, status JSON, logs, and relevant Telegram evidence before resolving or rolling back.
