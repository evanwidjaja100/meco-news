"""SQLite schema definitions and migration checksums."""

from __future__ import annotations

from hashlib import sha256


CURRENT_SCHEMA_VERSION = 2

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
"""

MIGRATION_DESCRIPTIONS = {
    1: "legacy sent_articles/runs adoption",
    2: "durable leases deliveries items chunks and source results",
}

# ponytail: immutable per-migration bytes — adding a future migration must not change prior checksums (C2.1)
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
}


def migration_checksum(version: int) -> str:
    description = MIGRATION_DESCRIPTIONS.get(version, "")
    # Use per-migration SQL so future additions do not mutate prior checksums
    sql = MIGRATION_SQL.get(version, "")
    return sha256(f"{version}:{description}:{sql}".encode()).hexdigest()
