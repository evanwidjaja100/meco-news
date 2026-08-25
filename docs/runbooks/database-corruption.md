# Runbook: database corruption

Alert: `db_corrupt`, failed integrity check, or migration checksum mismatch.

Stop the scheduler and preserve the database, WAL/SHM files, logs, status output, and file hashes. Do not run repair SQL or overwrite the only copy. Check disk and filesystem health, then identify the most recent verified backup.

Restore by checksum-verifying into a temporary path, running integrity and schema checks, atomically replacing the state, and running offline preflight plus a dry-run. Verify the lease is clear and no ambiguous delivery is forgotten. If the backup is incompatible, roll back to the recorded compatible artifact and backup. Escalate to the release and operational approvers.

