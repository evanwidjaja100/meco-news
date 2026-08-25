# Contributing

Use Python 3.12 or 3.13 and keep the runtime dependency surface deliberate. Run:

```powershell
python -B -m unittest discover -s tests -v
```

Every behavior change needs a focused regression test and documentation when it changes an operational contract. Do not call public network services from tests; use local fakes or mocks. Do not commit `.env`, databases, logs, caches, build outputs, or raw publisher payloads.

State changes must preserve short transactions, forward-only migrations, immutable prepared content, and the ambiguity invariant. Changes to collectors must preserve response/field/parser budgets and per-source isolation. Changes to Telegram rendering must validate final UTF-16 and raw HTML byte limits.

Before release, run package, migration/backup, concurrency, security-regression, Docker-context, and target-host checks. A reviewer who did not implement a migration, security boundary, or release gate must inspect it before promotion.

