# Windows Task Scheduler deployment

Use a dedicated non-admin account, a project-local virtual environment, explicit ACLs, and a local NTFS state directory. Do not rely on PATH or the host timezone for scheduling.

```powershell
py -3.13 -m venv .venv
Copy-Item .env.example .env
powershell -ExecutionPolicy Bypass -File scripts/install-windows-task.ps1 `
  -PythonPath .venv\Scripts\python.exe
```

The installer creates an idempotent task that invokes `--run-if-due` every 15 minutes, starts when available, ignores overlapping instances, retries task failures three times, and enforces a 30-minute execution limit. The application computes the configured Asia/Jakarta due window, so changing the host display timezone does not change the delivery date.

Validate the task with:

```powershell
.venv\Scripts\python.exe -m meco_news --preflight --json
.venv\Scripts\python.exe -m meco_news --status --json
.venv\Scripts\python.exe -m meco_news --healthcheck --json
```

Uninstall only the task; it preserves `.env`, state, logs, and backups:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/uninstall-windows-task.ps1
```

