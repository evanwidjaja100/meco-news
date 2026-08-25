# Runbook: state disk exhaustion

Alert: less than 1 GiB/10% free, SQLite write failure, or inability to create WAL/SHM.

Stop the scheduler before cleanup. Preserve status/log evidence. Confirm the state path is local and writable. Remove only reviewed old logs/backups according to retention; never delete the active database or article history as a first response.

Create and verify a backup after space is available, run integrity/preflight, then perform one canary dry-run and resume one scheduler. If corruption occurred, use the database-corruption runbook. Document files removed, recovery point, and owner approval.

