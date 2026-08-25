# Linux/NAS deployment

Use local POSIX storage for SQLite. SMB/NFS state is unsupported unless a separate locking test profile has passed.

1. Create `.env` from `.env.example`, set the Telegram credentials, and protect it with mode `0600`.
2. Review `config/watchlist.json` with `python -m meco_news --config-show --json`.
3. Build from a release commit. Production promotion must replace the local Python base tag with the exact digest recorded in the release manifest.
4. Start one scheduler:

   ```sh
   docker compose up -d --build
   docker compose logs -f meco-news
   ```

The container runs as UID/GID 10001, has a read-only root filesystem, drops capabilities, uses a private temporary filesystem, and writes only `/app/data`. The named volume must be local and writable for the directory, including SQLite `-wal` and `-shm` files. Configuration is mounted read-only and secrets are supplied through the runtime environment.

Check readiness before enabling alerts:

```sh
docker compose exec meco-news python -m meco_news --preflight --json
docker compose exec meco-news python -m meco_news --status --json
docker compose exec meco-news python -m meco_news --healthcheck --json
```

Run one backup before upgrades and verify the manifest checksum. Stop the old scheduler before deploying a new image; the SQLite lease is a defense in depth, not permission to run two schedulers indefinitely.

