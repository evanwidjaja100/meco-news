# Runbook: source outage

Alert: source failures or more than half of sources failing for two runs.

Inspect `run_terminal` and source result reason codes; do not log or paste raw URLs/payloads. A partial outage should still deliver healthy-source stories with a coverage note. An all-source outage must remain `retry_wait` and unhealthy after exhaustion.

Recovery: verify DNS/egress and the configured source host, then run a bounded dry-run. Correct only the watchlist/source allowlist through review. Do not disable URL, byte, parser, or deadline limits. Verify a healthy source result, a successful or `completed_empty` delivery, and updated status. Preserve logs and the config hash; roll back a bad configuration change.

