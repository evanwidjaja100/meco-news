# Runbook: backup and restore

Create an online backup with `python -m meco_news --backup backups/` and retain the database plus manifest. Verify the SHA-256, schema version, application version, and `integrity=ok`; keep 7 daily, 4 weekly, and 12 monthly copies unless policy says otherwise.

To restore, stop all schedulers, verify the manifest, restore to a temporary path, run integrity/migration/preflight checks, atomically replace the target while retaining the previous file, and run a dry-run. Re-enable exactly one scheduler. Preserve the old database, manifests, logs, and evidence. Do not restore an unverified or incompatible backup.

