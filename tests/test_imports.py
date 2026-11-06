import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from geo.imports import Imports, main
from geo.retention import Retention
from geo.limits import Limits, CapacityExceeded
from geo.backup import copy_database
from geo.db import Database
from geo.service import Service, Conflict


class ImportsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'db.sqlite3'
        self.service = Service(Database(self.path))
        self.sid = self.service.create_site(dict(name='demo', url='https://demo.example', brands=['Go']))['id']
        self.importer = Imports(self.service)
        self.file = Path(self.tmp.name) / 'samples.jsonl'

    def write(self, count=2, answer='Go'):
        rows = [dict(source='synthetic', record=dict(event_id=str(i), observed_at='2026-10-30T00:00:00Z',
            platform='synthetic', model='unknown', surface='manual', locale='zh-CN', prompt='fixture',
            answer=answer, citations=[])) for i in range(count)]
        self.file.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows)+'\n', encoding='utf-8')

    def count(self):
        with self.service.db.connect() as conn:
            return conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]

    def execute(self, plan):
        return self.importer.execute(self.sid, self.file, plan['id'], 'import-previewed-samples')

    def test_registration_rejects_normalized_brand_collision(self):
        with self.assertRaises(ValueError):
            self.service.create_site(dict(name='collision', url='https://collision.example', brands=['Go', 'Ｇｏ']))

    def test_preview_rollback_commit_and_idempotent_result(self):
        self.write(501)
        plan = self.importer.preview(self.sid, self.file)
        self.assertEqual(self.count(), 0)
        self.assertEqual([b['rows'] for b in plan['batches']], [500, 1])
        done = self.execute(plan)
        self.assertEqual(done['inserted'], 501)
        self.assertEqual(self.execute(plan), done)
        self.assertEqual(self.importer.result(self.sid, plan['id']), done)
        second = self.importer.preview(self.sid, self.file)
        self.assertEqual(sum(b['duplicates'] for b in second['batches']), 501)

    def test_file_change_expiry_and_confirmation(self):
        self.write()
        plan = self.importer.preview(self.sid, self.file)
        with self.assertRaises(ValueError):
            self.importer.execute(self.sid, self.file, plan['id'], 'yes')
        with patch('geo.imports.time.time', return_value=plan['expires']+1):
            with self.assertRaises(ValueError):
                self.execute(plan)
        self.write(answer='changed')
        with self.assertRaises(Conflict):
            self.execute(plan)
        self.assertEqual(self.count(), 0)

    def test_cross_site_and_context_change(self):
        self.write()
        plan = self.importer.preview(self.sid, self.file)
        other = self.service.create_site(dict(name='other', url='https://other.example', brands=['Go']))['id']
        with self.assertRaises(LookupError):
            self.importer.execute(other, self.file, plan['id'], 'import-previewed-samples')
        self.service.brands.configure(self.sid, {'brands': [{'name': 'New'}]})
        with self.assertRaises(Conflict):
            self.execute(plan)
        self.assertEqual(self.count(), 0)

    def test_conflict_in_later_batch_does_not_import_earlier_batch(self):
        self.write(501)
        plan = self.importer.preview(self.sid, self.file)
        last = json.loads(self.file.read_text(encoding='utf-8').splitlines()[-1])
        last['record']['answer'] = 'conflicting'
        self.service.ingest(self.sid, 'answers', dict(adapter='manual-v1', source=last['source'], records=[last['record']]))
        with self.assertRaises(Conflict):
            self.execute(plan)
        self.assertEqual(self.count(), 1)
        saved = self.importer.result(self.sid, plan['id'])
        self.assertEqual(saved['last_attempt']['batches'][-1]['status'], 'conflict')
        self.assertEqual(saved['state'], 'preview')
        blocked = self.importer.preview(self.sid, self.file)
        self.assertFalse(blocked['executable'])
        self.assertEqual(self.count(), 1)

    def test_partial_operational_failure_rolls_back(self):
        self.write(501)
        plan = self.importer.preview(self.sid, self.file)
        original = self.service.ingest
        calls = 0
        def fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('simulated failure')
            return original(*args, **kwargs)
        with patch.object(self.service, 'ingest', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.execute(plan)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.execute(plan)['inserted'], 501)

    def test_changed_duplicates_require_new_preview(self):
        self.write()
        first = self.importer.preview(self.sid, self.file)
        second = self.importer.preview(self.sid, self.file)
        self.execute(first)
        with self.assertRaises(Conflict):
            self.execute(second)
        self.assertEqual(self.count(), 2)

    def test_retention_boundary_and_completed_retry_do_not_resurrect(self):
        self.write()
        before = self.importer.preview(self.sid, self.file)
        done = self.execute(before)
        retention = Retention(self.service)
        delete = retention.preview(self.sid, '2026-10-31T00:00:00Z')
        retention.execute(self.sid, delete['id'], 'delete-previewed-events')
        self.assertEqual(self.execute(before), done)
        self.assertEqual(self.count(), 0)
        blocked = self.importer.preview(self.sid, self.file)
        self.assertFalse(blocked['executable'])

    def test_capacity_and_invalid_file_leave_no_samples(self):
        self.write()
        self.service.limits = Limits(query_events=1)
        with self.assertRaises(CapacityExceeded):
            self.importer.preview(self.sid, self.file)
        self.file.write_text('{}\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.importer.preview(self.sid, self.file)
        self.assertEqual(self.count(), 0)

    def test_unicode_line_separator_and_no_body_in_receipt(self):
        self.write(answer='PRIVATE\u2028BODY')
        plan = self.importer.preview(self.sid, self.file)
        self.assertEqual(self.execute(plan)['inserted'], 2)
        self.assertNotIn('PRIVATE', json.dumps(plan))

    def test_migration_backup_and_cli_result(self):
        with self.service.db.connect() as conn:
            conn.execute('DROP TABLE sample_imports')
            conn.execute('PRAGMA user_version=6')
        self.service = Service(Database(self.path))
        self.importer = Imports(self.service)
        self.write()
        plan = self.importer.preview(self.sid, self.file)
        done = self.execute(plan)
        backup = Path(self.tmp.name) / 'backup.sqlite3'
        copy_database(self.path, backup)
        self.assertEqual(Imports(Service(Database(backup))).result(self.sid, plan['id']), done)
        with redirect_stdout(io.StringIO()), patch('socket.socket', side_effect=AssertionError('network forbidden')):
            self.assertEqual(main(['result', '--db', str(backup), '--site', self.sid, '--plan', plan['id']]), 0)
