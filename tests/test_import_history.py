import json
from pathlib import Path
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from unittest.mock import patch
from geo.db import Database
from geo.service import Service
from geo.imports import Imports
from geo.import_history import History, main
from geo.service import Conflict
from geo.limits import Limits, CapacityExceeded
from geo.backup import copy_database


class ImportHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(Database(Path(self.tmp.name) / 'db.sqlite3'))
        self.sid = self.service.create_site(dict(name='synthetic', url='https://demo.example', brands=['Go']))['id']
        self.importer = Imports(self.service)
        self.file = Path(__file__).resolve().parents[1] / 'examples' / 'manual-samples.jsonl'
        self.secret = 'synthetic-local-key-at-least-24-characters'
        self.history = History(self.service, self.secret)

    def test_expiry_equality_rejects_commit(self):
        plan = self.importer.preview(self.sid, self.file)
        with patch('geo.imports.time.time', return_value=plan['expires']):
            with self.assertRaises(ValueError):
                self.importer.execute(self.sid, self.file, plan['id'], 'import-previewed-samples')

    def old_plan(self):
        with patch('geo.imports.time.time', return_value=100):
            return self.importer.preview(self.sid, self.file)

    def test_quality_counts_file_samples_and_remains_historical(self):
        plan = self.importer.preview(self.sid, self.file)
        self.assertEqual(plan['quality'], dict(scope='input_file_including_duplicates', samples=2,
            without_citations=1, without_evidence_url=1, unknown_model=2,
            by_surface={'manual': 2, 'consumer_ui': 0, 'model_api': 0}))
        self.importer.execute(self.sid, self.file, plan['id'], 'import-previewed-samples')
        duplicate = self.importer.preview(self.sid, self.file)
        self.assertEqual(duplicate['quality'], plan['quality'])
        item = self.history.page(self.sid)['items'][0]
        self.assertEqual(item['quality']['samples'], 2)
        for name in ('batches', 'sha256', 'source', 'answer', 'prompt'):
            self.assertNotIn(name, item)

    def test_page_excludes_new_plans_after_cleanup(self):
        first, second = self.old_plan(), self.old_plan()
        page = self.history.page(self.sid, limit=1)
        cleanup = self.history.cleanup_preview(self.sid)
        self.history.cleanup_execute(self.sid, cleanup['cursor'], 'delete-expired-import-previews')
        new = self.importer.preview(self.sid, self.file)
        self.assertEqual(self.history.page(self.sid, cursor=page['next_cursor'])['items'], [])
        fresh = self.history.page(self.sid)['items'][0]
        self.assertEqual(fresh['id'], new['id'])
        self.assertGreater(fresh['sequence'], page['snapshot_high_watermark'])

    def test_cleanup_preserves_receipt_and_unexpired_plan(self):
        committed = self.old_plan()
        with patch('geo.imports.time.time', return_value=101):
            done = self.importer.execute(self.sid, self.file, committed['id'], 'import-previewed-samples')
        old = self.old_plan()
        fresh = self.importer.preview(self.sid, self.file)
        selection = self.history.cleanup_preview(self.sid)
        self.assertEqual(selection['count'], 1)
        self.assertEqual(self.history.cleanup_execute(self.sid, selection['cursor'], 'delete-expired-import-previews')['deleted'], 1)
        self.assertEqual(self.importer.execute(self.sid, self.file, committed['id'], 'import-previewed-samples'), done)
        self.assertEqual(self.importer.result(self.sid, fresh['id'])['state'], 'preview')
        with self.assertRaises(LookupError):
            self.importer.result(self.sid, old['id'])
        self.assertEqual(self.service.report(self.sid)['answer_samples'], 2)

    def test_cleanup_confirm_scope_tamper_and_expiry(self):
        self.old_plan()
        selection = self.history.cleanup_preview(self.sid)
        other = self.service.create_site(dict(name='other', url='https://other.example', brands=['Go']))['id']
        for sid, cursor, confirm in ((self.sid, selection['cursor'], 'yes'),
                                    (other, selection['cursor'], 'delete-expired-import-previews'),
                                    (self.sid, selection['cursor']+'x', 'delete-expired-import-previews')):
            with self.assertRaises(ValueError):
                self.history.cleanup_execute(sid, cursor, confirm)
        with patch('geo.cursors.time.time', return_value=99999999999):
            with self.assertRaises(ValueError):
                self.history.cleanup_execute(self.sid, selection['cursor'], 'delete-expired-import-previews')
        with self.assertRaises(ValueError):
            self.history.page(self.sid, cursor=selection['cursor'])

    def test_cleanup_batch_limit_and_changed_selection(self):
        for _ in range(3):
            self.old_plan()
        first = self.history.cleanup_preview(self.sid, limit=2)
        competing = self.history.cleanup_preview(self.sid, limit=1)
        self.history.cleanup_execute(self.sid, competing['cursor'], 'delete-expired-import-previews')
        with self.assertRaises(Conflict):
            self.history.cleanup_execute(self.sid, first['cursor'], 'delete-expired-import-previews')
        self.assertEqual(len(self.history.page(self.sid)['items']), 2)

    def test_output_budget_failure_precedes_delete(self):
        self.old_plan()
        selection = self.history.cleanup_preview(self.sid)
        with patch.object(self.history, 'output', side_effect=CapacityExceeded('simulated capacity')):
            with self.assertRaises(CapacityExceeded):
                self.history.cleanup_execute(self.sid, selection['cursor'], 'delete-expired-import-previews')
        self.assertEqual(len(self.history.page(self.sid)['items']), 1)

    def test_legacy_quality_unknown_and_v7_upgrade(self):
        plan = self.old_plan()
        with self.service.db.connect() as conn:
            legacy = dict(plan)
            legacy.pop('quality')
            conn.execute('UPDATE sample_imports SET payload=? WHERE id=?', (json.dumps(legacy), plan['id']))
            conn.execute('DROP TABLE import_positions')
            conn.execute('PRAGMA user_version=7')
        upgraded = Service(Database(self.service.db.path))
        self.assertIsNone(History(upgraded, self.secret).page(self.sid)['items'][0]['quality'])
        backup = Path(self.tmp.name) / 'backup.sqlite3'
        copy_database(self.service.db.path, backup)
        self.assertEqual(len(History(Service(Database(backup)), self.secret).page(self.sid)['items']), 1)

    def test_cli_no_network_and_key_required(self):
        self.old_plan()
        with patch.dict('os.environ', {'GEO_API_TOKEN': self.secret}), redirect_stdout(io.StringIO()), patch('socket.socket', side_effect=AssertionError('no network')):
            self.assertEqual(main(['list', '--db', self.service.db.path, '--site', self.sid]), 0)
        with self.assertRaises(ValueError):
            History(self.service, '')
