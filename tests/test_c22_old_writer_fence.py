# C2.2c legacy old-writer fence tests (closure plan C2.2).
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from meco_news.inspection import inspect_state
from meco_news.migrate import _is_current_and_complete
from meco_news.migrations import (
    CURRENT_SCHEMA_VERSION,
    LEGACY_FENCE_TRIGGERS,
    MIGRATION_SQL,
    MigrationGuard,
    migration_checksum,
    verify_catalog,
)
from meco_news.storage import (
    StateError,
    StateStore,
    _split_sql_statements,
    run_catalog_migrations,
)

def _make_v3(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(MIGRATION_SQL[1])
        connection.executescript(MIGRATION_SQL[2])
        connection.executescript(MIGRATION_SQL[3])
        for version in (1, 2, 3):
            connection.execute(
                'INSERT INTO schema_migrations(version, checksum, applied_at, app_version) VALUES (?,?,?,?)',
                (version, migration_checksum(version), '2026-01-01T00:00:00+00:00', '2.0.0'),
            )
        connection.execute(
            'INSERT INTO sent_articles(fingerprint,title,url,source,topic,score,sent_at,delivery_date) VALUES (?,?,?,?,?,?,?,?)',
            ('fp1', 'LPG terminal', 'https://example.com/lpg', 'Example', 'lpg_energy', 10, '2026-01-01T00:00:00+00:00', '2026-01-01'),
        )
        connection.execute(
            'INSERT INTO runs(delivery_date,started_at,completed_at,status,item_count,error) VALUES (?,?,?,?,?,?)',
            ('2026-01-01', '2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00', 'completed', 1, ''),
        )
        connection.commit()
    finally:
        connection.close()

def _migrate(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return run_catalog_migrations(connection, guard=MigrationGuard.for_tests(), app_version='2.0.0')
    finally:
        connection.close()


def _triggers(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute('SELECT name FROM sqlite_master WHERE type = ?', ('trigger',)).fetchall()
        return {str(row[0]) for row in rows}
    finally:
        connection.close()


class TestSplitter(unittest.TestCase):
    def test_legacy_migrations_split_identically_to_naive(self) -> None:
        for version in (1, 2, 3):
            naive = [part for part in MIGRATION_SQL[version].split(';') if part.strip()]
            self.assertEqual(_split_sql_statements(MIGRATION_SQL[version]), naive)

    def test_fence_migration_splits_into_six_trigger_statements(self) -> None:
        statements = _split_sql_statements(MIGRATION_SQL[4])
        self.assertEqual(len(statements), 6)
        for statement in statements:
            self.assertIn('CREATE TRIGGER', statement)
            self.assertIn('RAISE', statement)

class TestOldWriterFence(unittest.TestCase):
    def test_fresh_database_installs_fences_and_full_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            with StateStore(path):
                pass
            self.assertTrue(set(LEGACY_FENCE_TRIGGERS) <= _triggers(path))
            connection = sqlite3.connect(path)
            try:
                rows = connection.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall()
            finally:
                connection.close()
            self.assertEqual([int(row[0]) for row in rows], list(range(1, CURRENT_SCHEMA_VERSION + 1)))

    def test_migration_installs_fences_and_advances_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            _make_v3(path)
            applied = _migrate(path)
            self.assertEqual(applied, CURRENT_SCHEMA_VERSION - 3)
            self.assertTrue(set(LEGACY_FENCE_TRIGGERS) <= _triggers(path))
            with StateStore(path) as store:
                self.assertEqual(store.schema_version, CURRENT_SCHEMA_VERSION)
            self.assertTrue(_is_current_and_complete(path))

    def test_pre_migration_legacy_writes_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            _make_v3(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    'INSERT INTO sent_articles(fingerprint,title,url,source,topic,score,sent_at,delivery_date) VALUES (?,?,?,?,?,?,?,?)',
                    ('fp2', 'Fuel tanker', 'https://example.com/fuel', 'Example', 'fuel_logistics', 9, '2026-02-02T00:00:00+00:00', '2026-02-02'),
                )
                connection.execute('UPDATE sent_articles SET score = 99 WHERE fingerprint = ?', ('fp1',))
                connection.execute(
                    'INSERT INTO runs(delivery_date,started_at,completed_at,status,item_count,error) VALUES (?,?,?,?,?,?)',
                    ('2026-02-02', '2026-02-02T00:00:00+00:00', '2026-02-02T01:00:00+00:00', 'completed', 2, ''),
                )
                connection.execute('UPDATE runs SET item_count = 5 WHERE delivery_date = ?', ('2026-01-01',))
                connection.execute('DELETE FROM runs WHERE delivery_date = ?', ('2026-02-02',))
                connection.execute('DELETE FROM sent_articles WHERE fingerprint = ?', ('fp2',))
                connection.commit()
            finally:
                connection.close()

    def test_post_migration_legacy_writes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            _make_v3(path)
            _migrate(path)
            sent_insert = 'INSERT INTO sent_articles(fingerprint,title,url,source,topic,score,sent_at,delivery_date) VALUES (?,?,?,?,?,?,?,?)'
            sent_replace = 'INSERT OR REPLACE INTO sent_articles(fingerprint,title,url,source,topic,score,sent_at,delivery_date) VALUES (?,?,?,?,?,?,?,?)'
            runs_insert = 'INSERT INTO runs(delivery_date,started_at,completed_at,status,item_count,error) VALUES (?,?,?,?,?,?)'
            runs_replace = 'INSERT OR REPLACE INTO runs(delivery_date,started_at,completed_at,status,item_count,error) VALUES (?,?,?,?,?,?)'
            cases = [
                ('sent_articles', 'insert', sent_insert, ('fp-new', 'Blocked', 'https://example.com/blocked', 'Example', 'lpg_energy', 1, '2026-03-03T00:00:00+00:00', '2026-03-03')),
                ('sent_articles', 'update', 'UPDATE sent_articles SET score = 1 WHERE fingerprint = ?', ('fp1',)),
                ('sent_articles', 'delete', 'DELETE FROM sent_articles WHERE fingerprint = ?', ('fp1',)),
                ('sent_articles', 'replace', sent_replace, ('fp1', 'Blocked', 'https://example.com/blocked', 'Example', 'lpg_energy', 1, '2026-03-03T00:00:00+00:00', '2026-03-03')),
                ('runs', 'insert', runs_insert, ('2026-12-12', '2026-12-12T00:00:00+00:00', '2026-12-12T01:00:00+00:00', 'completed', 1, '')),
                ('runs', 'update', 'UPDATE runs SET item_count = 7 WHERE delivery_date = ?', ('2026-01-01',)),
                ('runs', 'delete', 'DELETE FROM runs WHERE delivery_date = ?', ('2026-01-01',)),
                ('runs', 'replace', runs_replace, ('2026-01-01', '2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00', 'completed', 9, '')),
            ]
            for table, op, sql, params in cases:
                with self.subTest(table=table, op=op):
                    connection = sqlite3.connect(path)
                    try:
                        with self.assertRaises(sqlite3.Error):
                            connection.execute(sql, params)
                    finally:
                        connection.close()

    def test_adopted_rows_survive_fence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            _make_v3(path)
            _migrate(path)
            with StateStore(path) as store:
                rows = store.connection.execute('SELECT delivery_date FROM deliveries WHERE delivery_date = ?', ('2026-01-01',)).fetchall()
                self.assertEqual(len(rows), 1)
                history = store.connection.execute('SELECT fingerprint FROM article_history WHERE fingerprint = ?', ('fp1',)).fetchall()
                self.assertEqual(len(history), 1)

    def test_dropped_fence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            _make_v3(path)
            _migrate(path)
            dropped = LEGACY_FENCE_TRIGGERS[0]
            connection = sqlite3.connect(path)
            try:
                connection.execute('DROP TRIGGER ' + dropped)
                connection.commit()
            finally:
                connection.close()
            self.assertFalse(_is_current_and_complete(path))
            self.assertEqual(inspect_state(path).classification, 'malformed')
            with self.assertRaises(StateError):
                StateStore(path)

    def test_catalog_includes_fence_migration(self) -> None:
        report = verify_catalog()
        self.assertTrue(report.ok, str(report.issues))
        self.assertEqual(list(report.versions), list(range(1, CURRENT_SCHEMA_VERSION + 1)))
        self.assertEqual(CURRENT_SCHEMA_VERSION, 5)
        for name in LEGACY_FENCE_TRIGGERS:
            self.assertIn(name, MIGRATION_SQL[4])
        self.assertEqual(len(LEGACY_FENCE_TRIGGERS), 6)


if __name__ == '__main__':
    unittest.main()

