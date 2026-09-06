# MECO Daily Market Watch

MECO Daily Market Watch is a dependency-free Python service for PT Meco Inoxprima. It collects publisher RSS/search metadata, validates and bounds it, enforces a rolling freshness window, ranks commercial relevance, deduplicates deterministic story identities, and delivers links to Telegram. It does not republish full articles, bypass paywalls, or scrape authenticated pages.

The watchlist covers MECO mentions plus process plants, tanks, pressure vessels, heat exchangers, LPG, liquid-fuel logistics, aviation refuelling, energy/process infrastructure, customer-industry capex, industrial materials/regulation, and peer activity.

## Quick start

Use Python 3.12 or 3.13 and local NTFS/POSIX storage.

```powershell
Copy-Item .env.example .env
# edit .env and add TELEGRAM_BOT_TOKEN
python -m meco_news --preflight --json
python -m meco_news --discover-chat
# put the returned ID in .env
python -m meco_news --test-telegram
python -m meco_news --dry-run --verbose
python -m meco_news
```

The bot must receive `/start` before it can message a private chat. Keep `.env` secret; it is ignored by Git and excluded from the Docker build context.

## Delivery semantics

The default discovery and selection window is seven days, enforced after every collector. A known-date story older than the window, too far in the future, or missing a date is excluded by default. The daily target is 5-7 stories, but the service never pads a quiet market with unrelated filler.

Each live date has one SQLite lease owner. Prepared items and Telegram chunks are immutable. A confirmed chunk is recorded and never automatically resent. A retryable rejection waits durably for its next attempt. A timeout, reset, process death, or malformed response after a possible Telegram request is `ambiguous`; later chunks stop and an operator must reconcile it:

```powershell
python -m meco_news --status --json
python -m meco_news --resolve-chunk 12 --resolution sent --reason "confirmed in Telegram" --operator "name"
# or --resolution retry when Telegram confirms it was not accepted
```

If at least one source succeeds but no eligible story remains, the service sends one outboxed coverage notice and completes as `completed_empty`. If every source fails, it enters bounded same-day retry and becomes unhealthy after exhaustion. One-to-four stories are delivered with a coverage warning.

`--force` creates a new audited generation only after the date is complete and no ambiguity exists. It still excludes acknowledged URL/title history; it is not a replay command.

Schema migration uses an explicit audited command (`--migrate --to-version N`), but execution stays disabled until the C2.2 exclusive maintenance guard exists: it always fails closed with `maintenance_unavailable` (exit 1) and changes no state. Runtime startup never auto-migrates; a state database that needs migration fails closed with `migration_required`.

## Safe operations

```powershell
python -m meco_news --config-show --json
python -m meco_news --preflight --json
python -m meco_news --preflight --online --json
python -m meco_news --status --json
python -m meco_news --healthcheck --json
python -m meco_news --backup backups\
```

Dry-run performs collection and ranking but does not create, migrate, write, lease, send, schedule, or create a log/status file. When a state database already exists it reads history read-only; use `--ignore-history` for an intentional all-candidate preview. Invalid option combinations fail with exit code 2 before state or network initialization.

Preflight (`--preflight`) is read-only: it never creates, migrates, writes, or WAL/SHM-sidecars the state database, and `ready` is the pure conjunction of the mandatory checks (timezone, runtime, state filesystem, maintenance, database, lease, secrets, plus online checks when requested). Exit codes are deterministic for multiple failures, highest precedence first: 9 unsupported Python (`>=3.12,<3.15` required), 4 state filesystem, 8 maintenance in progress, 5 schema (missing ledger objects, checksum mismatch, migration-required N-1, or newer N+1), 6 active lease, 3 secrets, 7 online. A missing database reports `missing`/`not_yet_created` and stays ready; anything else non-compatible is fail-closed.

## Configuration

Edit `config/watchlist.json` for sources, queries, topics, scoring, limits, delivery time, and retry policy. Strict typed validation rejects unknown keys and unsafe values. `MECO_CONFIG` and `STATE_DB` can relocate config/state. Configuration is revalidated on daemon cycles; a frozen delivery does not change when configuration changes.

## Scheduling

### Windows Task Scheduler

Create a project-local venv and install an idempotent 15-minute due-check task:

```powershell
py -3.14 -m venv .venv
powershell -ExecutionPolicy Bypass -File scripts/install-windows-task.ps1 -PythonPath .venv\Scripts\python.exe
```

The application computes the configured Asia/Jakarta due window rather than trusting the host display timezone. Remove only the task with `scripts/uninstall-windows-task.ps1`; state, secrets, logs, and backups remain.

### Docker Compose

```powershell
docker compose up -d --build
docker compose logs -f meco-news
```

The container runs as UID/GID 10001 with a read-only root filesystem and only a local `/app/data` volume writable. Production promotion must pin the base image and final image by immutable digest.

## Development and release

```powershell
python -B -m unittest discover -s tests -v
```

Tests use no public network. Package metadata, CI scaffolding, security policy, platform deployment notes, and incident runbooks are in the repository. See [docs/architecture.md](docs/architecture.md), [docs/monitoring.md](docs/monitoring.md), and [docs/release.md](docs/release.md).

