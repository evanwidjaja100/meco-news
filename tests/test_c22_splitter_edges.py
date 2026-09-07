# C2.2c splitter edge coverage plus trigger-probe failure (closure plan C2.2).
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from meco_news.migrate import _is_current_and_complete
from meco_news.storage import (
    StateError,
    StateStore,
    _split_sql_statements,
)


class TestSplitterEdgeCases(unittest.TestCase):
    def test_empty_and_blank_inputs_return_no_statements(self) -> None:
        self.assertEqual(_split_sql_statements(''), [])
        self.assertEqual(_split_sql_statements('   \n  '), [])

    def test_semicolon_inside_single_quoted_string_is_kept(self) -> None:
        q = chr(39)
        sql = 'INSERT INTO t(a) VALUES (' + q + 'x;y' + q + ');'
        self.assertEqual(_split_sql_statements(sql), [sql[:-1]])

    def test_escaped_single_quote_inside_string_is_kept(self) -> None:
        q = chr(39)
        sql = 'INSERT INTO t(a) VALUES (' + q + 'it' + q + q + 's;x' + q + ');'
        self.assertEqual(_split_sql_statements(sql), [sql[:-1]])

    def test_semicolon_inside_double_quoted_name_is_kept(self) -> None:
        d = chr(34)
        sql = 'CREATE TABLE ' + d + 'a;b' + d + ' (x);'
        self.assertEqual(_split_sql_statements(sql), [sql[:-1]])

    def test_doubled_double_quote_inside_name_is_kept(self) -> None:
        d = chr(34)
        sql = 'CREATE TABLE ' + d + 'a' + d + d + 'b' + d + ' (x);'
        self.assertEqual(_split_sql_statements(sql), [sql[:-1]])

    def test_line_comment_semicolons_are_kept(self) -> None:
        self.assertEqual(
            _split_sql_statements('SELECT 1; -- keep;this\nSELECT 2;'),
            ['SELECT 1', ' -- keep;this\nSELECT 2'],
        )

    def test_trailing_line_comment_without_newline_is_kept(self) -> None:
        self.assertEqual(_split_sql_statements('SELECT 1; -- tail'), ['SELECT 1', ' -- tail'])

    def test_block_comment_semicolons_are_kept(self) -> None:
        self.assertEqual(
            _split_sql_statements('SELECT /* a;b */ 1; SELECT 2;'),
            ['SELECT /* a;b */ 1', ' SELECT 2'],
        )

    def test_unterminated_block_comment_runs_to_end(self) -> None:
        sql = 'SELECT 1 /* open'
        self.assertEqual(_split_sql_statements(sql), [sql])

    def test_lowercase_begin_end_keep_trigger_body(self) -> None:
        sql = 'create trigger t after insert on x begin update y set z = 1; end; select 1;'
        self.assertEqual(
            _split_sql_statements(sql),
            ['create trigger t after insert on x begin update y set z = 1; end', ' select 1'],
        )

    def test_stray_end_does_not_swallow_following_statements(self) -> None:
        self.assertEqual(_split_sql_statements('END; SELECT 1;'), ['END', ' SELECT 1'])

    def test_statement_without_trailing_semicolon_is_kept(self) -> None:
        self.assertEqual(_split_sql_statements('SELECT _a1'), ['SELECT _a1'])

    def test_arithmetic_operators_are_not_comments(self) -> None:
        self.assertEqual(_split_sql_statements('SELECT 1-2/3*4;'), ['SELECT 1-2/3*4'])


class TestTriggerProbeFailure(unittest.TestCase):
    def test_trigger_probe_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            with StateStore(path):
                pass
            live = sqlite3.connect(path)
            try:
                ledger = [
                    (str(row[0]), str(row[1]))
                    for row in live.execute('SELECT version, checksum FROM schema_migrations ORDER BY version').fetchall()
                ]
                tables = [str(row[0]) for row in live.execute('SELECT name FROM sqlite_master').fetchall()]
                columns = [str(row[1]) for row in live.execute('PRAGMA table_info(deliveries)').fetchall()]
            finally:
                live.close()

            def _handler(sql, *args):
                text = str(sql)
                if 'trigger' in text:
                    raise sqlite3.DatabaseError('boom')
                if 'schema_migrations' in text:
                    rows = ledger
                elif 'table_info' in text:
                    probe_columns = columns
                    if 'outbox_chunks' in text:
                        probe_columns = ["first_attempt_at", "last_attempt_at", "retry_deadline_at"]
                    rows = [(None, name) for name in probe_columns]
                else:
                    rows = [(name,) for name in tables]
                result = MagicMock()
                result.fetchall.return_value = rows
                result.__iter__.return_value = iter(rows)
                return result

            connection = MagicMock()
            connection.execute.side_effect = _handler
            with patch('meco_news.migrate._ro_connection', return_value=connection), self.assertRaisesRegex(StateError, 'trigger probe failed'):
                _is_current_and_complete(path)
            connection.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

