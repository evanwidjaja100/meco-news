"""SQLite schema definitions and migration checksums."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256


CURRENT_SCHEMA_VERSION = 5

LEGACY_FENCE_TRIGGERS = (
    'trg_sent_articles_no_insert',
    'trg_sent_articles_no_update',
    'trg_sent_articles_no_delete',
    'trg_runs_no_insert',
    'trg_runs_no_update',
    'trg_runs_no_delete',
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    app_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_articles (
    fingerprint TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    topic TEXT NOT NULL,
    score INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    delivery_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    delivery_date TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS run_leases (
    scope TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_date TEXT NOT NULL,
    generation INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('content', 'empty', 'collection_retry')),
    state TEXT NOT NULL,
    run_id TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    target_snapshot TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    prepared_at TEXT,
    completed_at TEXT,
    next_attempt_at TEXT,
    terminal_error TEXT NOT NULL DEFAULT '',
    retry_policy_json TEXT NOT NULL DEFAULT '{}',
    retry_first_at TEXT,
    retry_last_at TEXT,
    retry_attempt_high_water INTEGER NOT NULL DEFAULT 0,
    retry_deadline_at TEXT,
    retry_elapsed_seconds INTEGER NOT NULL DEFAULT 0,
    force_operator TEXT NOT NULL DEFAULT '',
    force_reason TEXT NOT NULL DEFAULT '',
    predecessor_delivery_id INTEGER REFERENCES deliveries(delivery_id),
    UNIQUE(delivery_date, generation)
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    chunk_id INTEGER,
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT NOT NULL,
    error_class TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL,
    UNIQUE(delivery_id, chunk_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS delivery_items (
    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    position INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    url_key TEXT NOT NULL,
    title_key TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    summary TEXT NOT NULL DEFAULT '',
    collector TEXT NOT NULL DEFAULT '',
    query_name TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL,
    topic TEXT NOT NULL,
    topic_label TEXT NOT NULL DEFAULT '',
    relevance_reason TEXT NOT NULL DEFAULT '',
    matches_json TEXT NOT NULL DEFAULT '[]',
    chunk_index INTEGER NOT NULL,
    PRIMARY KEY(delivery_id, position)
);

CREATE TABLE IF NOT EXISTS outbox_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    sequence INTEGER NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'in_flight', 'sent', 'retry_wait', 'failed_terminal', 'ambiguous')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    in_flight_at TEXT,
    next_attempt_at TEXT,
    telegram_message_id TEXT,
    error_class TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    first_attempt_at TEXT,
    last_attempt_at TEXT,
    retry_deadline_at TEXT,
    UNIQUE(delivery_id, sequence)
);

CREATE TABLE IF NOT EXISTS article_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_key TEXT NOT NULL,
    title_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    chunk_id INTEGER REFERENCES outbox_chunks(chunk_id),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    sent_at TEXT NOT NULL,
    UNIQUE(delivery_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS source_results (
    source_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER REFERENCES deliveries(delivery_id),
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    bytes_read INTEGER NOT NULL DEFAULT 0,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    quarantined_count INTEGER NOT NULL DEFAULT 0,
    reason_code TEXT NOT NULL DEFAULT '',
    error_class TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_resolutions (
    resolution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id INTEGER NOT NULL REFERENCES outbox_chunks(chunk_id),
    resolution TEXT NOT NULL CHECK(resolution IN ('sent', 'retry')),
    reason TEXT NOT NULL,
    operator TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    from_state TEXT NOT NULL DEFAULT '',
    to_state TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS force_audits (
    force_audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    predecessor_delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    config_hash TEXT NOT NULL DEFAULT '',
    target_snapshot TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_fences (
    fence_id INTEGER PRIMARY KEY CHECK(fence_id = 1),
    epoch INTEGER NOT NULL,
    token TEXT NOT NULL,
    owner TEXT NOT NULL,
    scope TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_article_history_url ON article_history(url_key, sent_at);
CREATE INDEX IF NOT EXISTS idx_article_history_title ON article_history(title_key, sent_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_date_state ON deliveries(delivery_date, state);
CREATE INDEX IF NOT EXISTS idx_chunks_due ON outbox_chunks(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_source_results_delivery ON source_results(delivery_id);
CREATE INDEX IF NOT EXISTS idx_state_transitions_entity ON state_transitions(entity_type, entity_id, transition_id);
CREATE INDEX IF NOT EXISTS idx_force_audits_delivery ON force_audits(delivery_id, force_audit_id);

-- C2.2c: old-writer fence - legacy sent_articles/runs are read-only after v4
CREATE TRIGGER IF NOT EXISTS trg_sent_articles_no_insert
BEFORE INSERT ON sent_articles
BEGIN
SELECT RAISE(ABORT, 'legacy sent_articles writes are blocked after migration v4');
END;

CREATE TRIGGER IF NOT EXISTS trg_sent_articles_no_update
BEFORE UPDATE ON sent_articles
BEGIN
SELECT RAISE(ABORT, 'legacy sent_articles writes are blocked after migration v4');
END;

CREATE TRIGGER IF NOT EXISTS trg_sent_articles_no_delete
BEFORE DELETE ON sent_articles
BEGIN
SELECT RAISE(ABORT, 'legacy sent_articles writes are blocked after migration v4');
END;

CREATE TRIGGER IF NOT EXISTS trg_runs_no_insert
BEFORE INSERT ON runs
BEGIN
SELECT RAISE(ABORT, 'legacy runs writes are blocked after migration v4');
END;

CREATE TRIGGER IF NOT EXISTS trg_runs_no_update
BEFORE UPDATE ON runs
BEGIN
SELECT RAISE(ABORT, 'legacy runs writes are blocked after migration v4');
END;

CREATE TRIGGER IF NOT EXISTS trg_runs_no_delete
BEFORE DELETE ON runs
BEGIN
SELECT RAISE(ABORT, 'legacy runs writes are blocked after migration v4');
END;
"""

MIGRATION_DESCRIPTIONS = {
    1: "legacy sent_articles/runs adoption",
    2: "durable leases deliveries items chunks and source results",
    3: "delivery target snapshot for outbox immutability",
    4: 'legacy old-writer fence on sent_articles and runs',
    5: "durable retry policy, force audit, transition audit, and maintenance facts",
}

# ponytail: immutable per-migration bytes Ã¢â‚¬â€ adding a future migration must not change prior checksums (C2.1)
MIGRATION_SQL = {
    1: """
CREATE TABLE IF NOT EXISTS sent_articles (
    fingerprint TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    topic TEXT NOT NULL,
    score INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    delivery_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    delivery_date TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
""",
    2: """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    app_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_leases (
    scope TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_date TEXT NOT NULL,
    generation INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('content', 'empty', 'collection_retry')),
    state TEXT NOT NULL,
    run_id TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    prepared_at TEXT,
    completed_at TEXT,
    next_attempt_at TEXT,
    terminal_error TEXT NOT NULL DEFAULT '',
    UNIQUE(delivery_date, generation)
);
CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    chunk_id INTEGER,
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT NOT NULL,
    error_class TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL,
    UNIQUE(delivery_id, chunk_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS delivery_items (
    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    position INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    url_key TEXT NOT NULL,
    title_key TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    summary TEXT NOT NULL DEFAULT '',
    collector TEXT NOT NULL DEFAULT '',
    query_name TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL,
    topic TEXT NOT NULL,
    topic_label TEXT NOT NULL DEFAULT '',
    relevance_reason TEXT NOT NULL DEFAULT '',
    matches_json TEXT NOT NULL DEFAULT '[]',
    chunk_index INTEGER NOT NULL,
    PRIMARY KEY(delivery_id, position)
);
CREATE TABLE IF NOT EXISTS outbox_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    sequence INTEGER NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'in_flight', 'sent', 'retry_wait', 'failed_terminal', 'ambiguous')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    in_flight_at TEXT,
    next_attempt_at TEXT,
    telegram_message_id TEXT,
    error_class TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(delivery_id, sequence)
);
CREATE TABLE IF NOT EXISTS article_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_key TEXT NOT NULL,
    title_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    chunk_id INTEGER REFERENCES outbox_chunks(chunk_id),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    sent_at TEXT NOT NULL,
    UNIQUE(delivery_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS source_results (
    source_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER REFERENCES deliveries(delivery_id),
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    bytes_read INTEGER NOT NULL DEFAULT 0,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    quarantined_count INTEGER NOT NULL DEFAULT 0,
    reason_code TEXT NOT NULL DEFAULT '',
    error_class TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_resolutions (
    resolution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id INTEGER NOT NULL REFERENCES outbox_chunks(chunk_id),
    resolution TEXT NOT NULL CHECK(resolution IN ('sent', 'retry')),
    reason TEXT NOT NULL,
    operator TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_article_history_url ON article_history(url_key, sent_at);
CREATE INDEX IF NOT EXISTS idx_article_history_title ON article_history(title_key, sent_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_date_state ON deliveries(delivery_date, state);
CREATE INDEX IF NOT EXISTS idx_chunks_due ON outbox_chunks(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_source_results_delivery ON source_results(delivery_id);
""",
    3: """
-- C3.3: freeze outbox destination identity Ã¢â‚¬â€ target_snapshot binds chat/config/parse_mode
ALTER TABLE deliveries ADD COLUMN target_snapshot TEXT NOT NULL DEFAULT '';
""",
    4: """
-- C2.2c: old-writer fence - legacy sent_articles/runs are read-only after v4
CREATE TRIGGER IF NOT EXISTS trg_sent_articles_no_insert
BEFORE INSERT ON sent_articles
BEGIN
SELECT RAISE(ABORT, 'legacy sent_articles writes are blocked after migration v4');
END;

CREATE TRIGGER IF NOT EXISTS trg_sent_articles_no_update
BEFORE UPDATE ON sent_articles
BEGIN
SELECT RAISE(ABORT, 'legacy sent_articles writes are blocked after migration v4');
END;

CREATE TRIGGER IF NOT EXISTS trg_sent_articles_no_delete
BEFORE DELETE ON sent_articles
BEGIN
SELECT RAISE(ABORT, 'legacy sent_articles writes are blocked after migration v4');
END;

CREATE TRIGGER IF NOT EXISTS trg_runs_no_insert
BEFORE INSERT ON runs
BEGIN
SELECT RAISE(ABORT, 'legacy runs writes are blocked after migration v4');
END;

CREATE TRIGGER IF NOT EXISTS trg_runs_no_update
BEFORE UPDATE ON runs
BEGIN
SELECT RAISE(ABORT, 'legacy runs writes are blocked after migration v4');
END;

CREATE TRIGGER IF NOT EXISTS trg_runs_no_delete
BEFORE DELETE ON runs
BEGIN
SELECT RAISE(ABORT, 'legacy runs writes are blocked after migration v4');
END;
""",
    5: """
-- C2.2/C2.4/C3.2: provision all durable facts needed for safe resume,
-- forced generations, reconciliation, and operator audit.
ALTER TABLE deliveries ADD COLUMN retry_policy_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE deliveries ADD COLUMN retry_first_at TEXT;
ALTER TABLE deliveries ADD COLUMN retry_last_at TEXT;
ALTER TABLE deliveries ADD COLUMN retry_attempt_high_water INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deliveries ADD COLUMN retry_deadline_at TEXT;
ALTER TABLE deliveries ADD COLUMN retry_elapsed_seconds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deliveries ADD COLUMN force_operator TEXT NOT NULL DEFAULT '';
ALTER TABLE deliveries ADD COLUMN force_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE deliveries ADD COLUMN predecessor_delivery_id INTEGER REFERENCES deliveries(delivery_id);
ALTER TABLE outbox_chunks ADD COLUMN first_attempt_at TEXT;
ALTER TABLE outbox_chunks ADD COLUMN last_attempt_at TEXT;
ALTER TABLE outbox_chunks ADD COLUMN retry_deadline_at TEXT;

CREATE TABLE IF NOT EXISTS state_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    from_state TEXT NOT NULL DEFAULT '',
    to_state TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS force_audits (
    force_audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    predecessor_delivery_id INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    config_hash TEXT NOT NULL DEFAULT '',
    target_snapshot TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_fences (
    fence_id INTEGER PRIMARY KEY CHECK(fence_id = 1),
    epoch INTEGER NOT NULL,
    token TEXT NOT NULL,
    owner TEXT NOT NULL,
    scope TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_entity ON state_transitions(entity_type, entity_id, transition_id);
CREATE INDEX IF NOT EXISTS idx_force_audits_delivery ON force_audits(delivery_id, force_audit_id);
""",
}


def migration_checksum(version: int) -> str:
    description = MIGRATION_DESCRIPTIONS.get(version, "")
    # Use per-migration SQL so future additions do not mutate prior checksums
    sql = MIGRATION_SQL.get(version, "")
    return sha256(f"{version}:{description}:{sql}".encode()).hexdigest()


class MigrationNotPermitted(RuntimeError):
    """A catalog migration was attempted without an explicit guard (C2.1)."""


@dataclass(frozen=True, slots=True)
class MigrationGuard:
    """Explicit capability required to run catalog migrations.

    C2.1 admits only the ``tests`` scope. The exclusive maintenance guard
    (C2.2, ADR-C05) widens the accepted scope; the public migrate command
    never constructs this object and always fails closed until then.
    """

    scope: str

    @classmethod
    def for_tests(cls) -> MigrationGuard:
        return cls(scope="tests")


# Objects each migration must define, from immutable canonical bytes, so a
# future migration cannot silently drop a predecessor dependency (C2.1).
REQUIRED_CATALOG_OBJECTS: dict[int, tuple[str, ...]] = {
    1: ("sent_articles", "runs"),
    2: (
        "schema_migrations",
        "run_leases",
        "deliveries",
        "delivery_attempts",
        "delivery_items",
        "outbox_chunks",
        "article_history",
        "source_results",
        "delivery_resolutions",
    ),
    3: ("target_snapshot",),
    4: (
        'trg_sent_articles_no_insert',
        'trg_sent_articles_no_update',
        'trg_sent_articles_no_delete',
        'trg_runs_no_insert',
        'trg_runs_no_update',
        'trg_runs_no_delete',
    ),
    5: (
        "retry_policy_json",
        "retry_first_at",
        "retry_last_at",
        "retry_attempt_high_water",
        "retry_deadline_at",
        "retry_elapsed_seconds",
        "force_operator",
        "force_reason",
        "predecessor_delivery_id",
        "first_attempt_at",
        "last_attempt_at",
        "retry_deadline_at",
        "state_transitions",
        "force_audits",
        "maintenance_fences",
    ),
}

CatalogEntry = tuple[int, str, str]


def catalog_entries() -> tuple[CatalogEntry, ...]:
    versions = sorted(set(MIGRATION_DESCRIPTIONS) | set(MIGRATION_SQL))
    return tuple((version, MIGRATION_DESCRIPTIONS.get(version, ""), MIGRATION_SQL.get(version, "")) for version in versions)


@dataclass(frozen=True, slots=True)
class CatalogReport:
    ok: bool
    versions: tuple[int, ...]
    checksums: tuple[str, ...]
    issues: tuple[str, ...]


def verify_catalog(entries: Iterable[CatalogEntry] | None = None) -> CatalogReport:
    """Verify the immutable migration catalog without touching any database.

    Checks ordered versions (gaps, duplicates, missing, unsupported future),
    per-version description/SQL presence, checksum stability, and required
    objects. Adding a future migration must not alter prior checksums.
    """
    items = list(catalog_entries() if entries is None else entries)
    issues: list[str] = []
    versions = [int(version) for version, _, _ in items]
    counts = Counter(versions)
    duplicates = sorted(version for version, count in counts.items() if count > 1)
    if duplicates:
        issues.append(f"duplicate migration version(s): {', '.join(str(v) for v in duplicates)}")
    unique = sorted(set(versions))
    expected = list(range(1, CURRENT_SCHEMA_VERSION + 1))
    if unique != expected:
        missing = [v for v in expected if v not in set(unique)]
        unsupported = sorted(v for v in unique if v > CURRENT_SCHEMA_VERSION)
        if missing:
            issues.append(f"gap: missing migration version(s): {', '.join(str(v) for v in missing)}")
        if unsupported:
            issues.append(f"unsupported migration version(s) newer than {CURRENT_SCHEMA_VERSION}: " + ", ".join(str(v) for v in unsupported))
    checksums: list[str] = []
    for version, description, sql in items:
        entry_checksum = sha256(f"{version}:{description}:{sql}".encode()).hexdigest()
        checksums.append(entry_checksum)
        if not description:
            issues.append(f"migration {version} has no description")
        if not sql.strip():
            issues.append(f"migration {version} has no canonical SQL")
        if entry_checksum != migration_checksum(int(version)):
            issues.append(f"migration {version} checksum mismatch against canonical bytes")
        for required in REQUIRED_CATALOG_OBJECTS.get(int(version), ()):
            if required not in str(sql):
                issues.append(f"migration {version} is missing required object {required!r}")
    ordered = tuple(sorted(set(versions)))
    return CatalogReport(not issues, ordered, tuple(checksums), tuple(issues))


def ledger_contiguity_issue(versions: list[int]) -> str | None:
    """Report a gap/duplicate/empty defect in a migration ledger version list."""
    if not versions:
        return "migration ledger is empty"
    counts = Counter(versions)
    duplicates = sorted(version for version, count in counts.items() if count > 1)
    if duplicates:
        return f"migration ledger has duplicate version(s): {', '.join(str(v) for v in duplicates)}"
    unique = sorted(set(versions))
    if unique != list(range(1, unique[-1] + 1)):
        return f"migration ledger has a gap: versions {', '.join(str(v) for v in unique)} are not contiguous from 1"
    return None
