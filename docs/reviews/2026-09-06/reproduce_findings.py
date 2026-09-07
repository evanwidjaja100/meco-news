"""Isolated review probes. No real credentials, state, or public network are used.

Run from the repository root: python docs/reviews/2026-09-06/reproduce_findings.py
These record current behavior, not passing acceptance tests for the desired fixes.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import gc
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from meco_news import app, maintenance
from meco_news.backup import create_backup, restore_backup
from meco_news.collectors import CollectionResult, SourceResult, _collect_rss
from meco_news.config import load_config
from meco_news.inspection import WalProbeResult
from meco_news.models import NewsItem
from meco_news.observability import JsonEventFormatter
from meco_news.preflight import healthcheck, run_preflight
from meco_news.ranking import deduplicate
from meco_news.storage import StateStore
from meco_news.telegram import TelegramClient, TelegramSendError, build_digest


def main():
    config = load_config(ROOT / 'config/watchlist.json')
    observations = {}
    with tempfile.TemporaryDirectory(prefix='meco-review-') as directory:
        base = Path(directory)
        env = {'STATE_DB': str(base / 'run.db'), 'TELEGRAM_BOT_TOKEN': '123456:review-fake-token',
               'TELEGRAM_CHAT_ID': '123', 'LOG_FILE': ''}
        with patch.dict(os.environ, env, clear=True):
            # Keep a live WAL writer open, after checkpointing only the empty schema.
            path = base / 'wal.db'
            with StateStore(path):
                pass
            with StateStore(path) as writer:
                writer.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
                writer.acquire_lease('scheduler', 'live-owner', 180)
                writer.create_delivery('2026-09-06')
                with StateStore(path, readonly=True) as reader:
                    observations['wal_visibility'] = {
                        'writer_lease': writer.lease_info('scheduler') is not None,
                        'reader_lease': reader.lease_info('scheduler') is not None,
                        'writer_deliveries': writer.connection.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],
                        'reader_deliveries': reader.connection.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]}
                code, report = run_preflight(config, state_path=path)
                observations['preflight_with_live_wal_lease'] = {'code': code, 'ready': report['ready'],
                                                              'lease_check': report['checks'].get('lease')}
                with maintenance.MaintenanceContext.acquire(path, owner='maintenance') as guard:
                    writer.create_delivery('2026-09-07')
                    observations['open_writer_during_maintenance'] = {'guard_live': guard.live, 'write_succeeded': True}

            # Two acquisitions pause after their checks and before marker publication.
            barrier = threading.Barrier(2)
            original_replace = os.replace
            race_path = base / 'race.db'
            def racing_replace(src, dst):
                if str(dst).endswith('.maintenance.json'):
                    barrier.wait(timeout=5)
                return original_replace(src, dst)
            with patch.object(maintenance.os, 'replace', side_effect=racing_replace):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(maintenance.MaintenanceContext.acquire, race_path, owner=name)
                               for name in ('one', 'two')]
                    guards = [future.result() for future in futures]
            observations['maintenance_race'] = {'successful_acquires': len(guards), 'live_after_race': sum(g.live for g in guards)}
            for guard in guards:
                guard.release()

            # Only our own disposable child is probed. It never touches files/network.
            if os.name == 'nt':
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    reported_alive = maintenance._pid_alive(child.pid)
                    try:
                        exit_code = child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        exit_code = None
                    observations['windows_pid_probe'] = {'reported_alive': reported_alive, 'child_exit_code': exit_code}
                finally:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=5)

            failed = CollectionResult([], [SourceResult('fake', 'fake', 'failed')], datetime.now(UTC), 1)
            rounds = []
            with patch.object(app, 'collect_all', return_value=failed), patch.object(app, 'TelegramClient'):
                for _ in range(5):
                    outcome = app.run_once(config)
                    with StateStore(env['STATE_DB']) as store:
                        latest = store.status_snapshot()['latest_delivery']
                        rounds.append({'outcome': outcome.outcome, 'generation': latest['generation'],
                                       'attempts': store.collection_retry_count(latest['delivery_id'])})
                        with store.connection:
                            store.connection.execute("UPDATE deliveries SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE state='retry_wait'")
            observations['retry_budget_reset'] = rounds

            duplicate_path = base / 'ack-failure.db'
            news = NewsItem('PT Meco Inoxprima LPG terminal project', 'https://example.com/project', 'Example',
                            published_at=datetime.now(UTC), source_host='example.com')
            collection = CollectionResult([news], [SourceResult('fake', 'fake', 'succeeded', items=[news])], datetime.now(UTC), 1)
            original_finish = StateStore.finish_chunk
            first_ack = True
            def fail_first_ack(self, *args, **kwargs):
                nonlocal first_ack
                if first_ack:
                    first_ack = False
                    raise sqlite3.OperationalError('injected acknowledgment write failure')
                return original_finish(self, *args, **kwargs)
            with patch.dict(os.environ, {'STATE_DB': str(duplicate_path)}), patch.object(app, 'collect_all', return_value=collection), \
                 patch.object(app, 'TelegramClient') as client, patch.object(StateStore, 'finish_chunk', fail_first_ack):
                client.return_value.send_html.return_value = '123'
                first = app.run_once(config)
                with StateStore(duplicate_path) as store:
                    first_states = [row[0] for row in store.connection.execute('SELECT state FROM outbox_chunks')]
                second = app.run_once(config)
                observations['ack_write_failure_duplicates'] = {
                    'first_outcome': first.outcome, 'first_chunk_states': first_states,
                    'second_outcome': second.outcome, 'send_calls': client.return_value.send_html.call_count}

            authority = base / 'authority.db'
            with StateStore(authority) as owner:
                owner.acquire_lease('delivery', 'owner')
                delivery = owner.create_delivery('2026-09-06')
                with StateStore(authority) as stranger:
                    stranger.complete_run('2026-09-06', [])
                observations['non_owner_completion'] = {'state': owner.delivery(delivery.delivery_id).state,
                                                       'lease_owner': owner.lease_info()['owner_id']}

            frozen_path = base / 'frozen.db'
            with patch.dict(os.environ, {'STATE_DB': str(frozen_path)}):
                with StateStore(frozen_path) as store:
                    delivery = store.create_delivery(app._delivery_date(config))
                    store.acquire_lease('delivery', 'owner')
                    store.prepare_delivery(delivery.delivery_id, [], ['<b>pending</b>'], owner_id='owner',
                                           target_snapshot=app._target_snapshot(config))
                    store.release_lease('delivery', 'owner')
                raw = config.as_dict()
                raw['minimum_score'] += 1
                changed_path = base / 'changed.json'
                changed_path.write_text(json.dumps(raw))
                changed = load_config(changed_path)
                with patch.object(app, 'TelegramClient') as client:
                    outcome = app.run_once(changed)
                with StateStore(frozen_path) as store:
                    observations['config_edit_blocks_frozen_delivery'] = {
                        'outcome': outcome.outcome, 'ambiguous_chunks': store.status_snapshot()['unresolved_ambiguity_count'],
                        'send_calls': client.return_value.send_html.call_count}

            with patch.object(app, 'collect_all') as collect, contextlib.redirect_stderr(io.StringIO()) as preview:
                outcome = app.run_once(config, dry_run=True)
            observations['dry_run'] = {'outcome': outcome.outcome, 'collect_called': collect.called,
                                       'preview_first_line': preview.getvalue().splitlines()[0]}

            with patch('meco_news.preflight.inspection.probe_wal_capability', return_value=WalProbeResult(False, 'delete', 'no WAL')):
                code, report = run_preflight(config, state_path=base / 'missing.db')
            observations['wal_probe_ignored'] = {'code': code, 'ready': report['ready'],
                                                 'wal': report['checks']['state_filesystem']['wal']}

            stuck = base / 'stuck.db'
            with StateStore(stuck) as store:
                store.create_delivery('2000-01-01')
            healthy, report = healthcheck(config, state_path=stuck)
            observations['stuck_first_delivery_health'] = {'healthy': healthy, 'reasons': report['reasons']}

            item = NewsItem('LPG terminal project', 'https://example.com/a', 'Example', published_at=datetime.now(UTC),
                            topic_label='Energy', relevance_reason='Project')
            built = build_digest([item], 'MECO', 'UTC')
            named = re.findall(r'&([A-Za-z]+);', ''.join(built.messages))
            observations['telegram_named_entities'] = {'unsupported': sorted(set(named) - {'lt', 'gt', 'amp', 'quot'})}
            fake = TelegramClient('123456:review-fake-token', '123')
            response_results = []
            for body in (b'{}', b'{"ok":true,"result":{"message_id":null}}', b'{"ok":"false","result":{"message_id":12}}'):
                response = io.BytesIO(body)
                with patch('meco_news.telegram.urlopen', return_value=response):
                    try:
                        result = fake.send_html('<b>test</b>')
                        response_results.append({'body': body.decode(), 'accepted_message_id': result})
                    except TelegramSendError as error:
                        response_results.append({'body': body.decode(), 'outcome': error.outcome})
            observations['telegram_envelope_validation'] = response_results

            with patch('meco_news.collectors._fetch', return_value=b'<html><body>Service unavailable</body></html>'):
                result = _collect_rss({'id': 'fake', 'name': 'fake', 'url': 'https://example.com/'}, 5, config=config)
            observations['non_feed_xml'] = {'outcome': result.outcome, 'accepted_count': result.accepted_count}

            items = [NewsItem('LPG terminal construction project ' + tail, 'https://example.com/' + tail, 'S')
                     for tail in ('alpha', 'beta')]
            observations['dedup_budget'] = {
                'normal_count': len(deduplicate(items, {'limits': {'fuzzy_comparisons': 20}})),
                'exhausted_count': len(deduplicate(items, {'limits': {'fuzzy_comparisons': 1}}))}

            record = logging.LogRecord('review', logging.ERROR, __file__, 1, 'password=review-canary', (), None)
            observations['log_message_redaction'] = {'secret_survives': 'review-canary' in JsonEventFormatter().format(record)}

            missing_source = base / 'typo.db'
            artifact = create_backup(missing_source, base / 'backups')
            observations['backup_missing_source'] = {'created_source': missing_source.exists(), 'returned_backup': artifact.database.exists()}
            target = base / 'restore-target.db'
            with StateStore(target) as store:
                delivery = store.create_delivery('2026-09-06')
                store.acquire_lease('delivery', 'owner')
                store.prepare_delivery(delivery.delivery_id, [], ['<b>unsent</b>'], owner_id='owner')
                store.release_lease('delivery', 'owner')
            restore_backup(artifact.database, target)
            with StateStore(target) as store:
                observations['restore_discards_prepared'] = {'delivery_count_after': store.connection.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]}

            corrupt = base / 'corrupt.db'
            corrupt.write_bytes(b'not sqlite')
            observations['corrupt_status'] = app._state_status(corrupt, config)
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()), patch.object(app, 'load_dotenv'):
                app.main(['--config', str(ROOT / 'config/watchlist.json'), '--config-show', '--json'])
                try:
                    json.loads(output.getvalue())
                    observations['cli_json_single_document'] = True
                except json.JSONDecodeError:
                    observations['cli_json_single_document'] = False
            for handler in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(handler)
                handler.close()

            spec = importlib.util.spec_from_file_location('review_sentinel', ROOT / 'scripts/verify-build-context.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            fake_checkout = base / 'checkout'
            fake_checkout.mkdir()
            (fake_checkout / '.env').write_text('ORIGINAL=review-canary')
            (fake_checkout / '.dockerignore').write_text('.env\ndata/\nlogs/\n.git\n__pycache__/\n')
            with patch.object(module.subprocess, 'run', side_effect=FileNotFoundError), contextlib.redirect_stderr(io.StringIO()):
                module._check_actual_context(fake_checkout)
            observations['sentinel_destroys_existing_env'] = {'env_exists_after': (fake_checkout / '.env').exists()}
            # _history_reader can abandon its connection on corrupt input.
            # Collect it so Windows can remove our disposable directory.
            gc.collect()

    print(json.dumps(observations, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
