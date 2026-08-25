# Runbook: rollback

Disable the scheduler/container and confirm no process or lease is active. Preserve the current database, logs, status, image digest, and ambiguity evidence. If the previous artifact supports the current schema, deploy its immutable digest. Otherwise restore the verified pre-release backup before deploying the previous artifact.

Run offline preflight and dry-run, reconcile every ambiguous/partial delivery, then enable one scheduler. Verify a terminal event, health, backup, and no duplicate confirmed chunk. Never use mutable tags, overwrite the only database copy, run old/new binaries together, or force through unresolved state. Escalate to the release approver and record a regression test.

