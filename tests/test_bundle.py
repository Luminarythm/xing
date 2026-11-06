import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from geo.bundle import export_bundle
from geo.db import Database
from geo.limits import CapacityExceeded, Limits
from geo.quality import report as quality_report
from geo.service import Service


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.db = self.folder / 'db.sqlite3'
        self.service = Service(Database(self.db))
        self.sid = self.service.create_site(dict(name='本地', url='https://demo.example', brands=['Go']))['id']
        self.output = self.folder / 'report.zip'
        self.start, self.end = '2026-10-30T08:00:00+08:00', '2026-10-31T00:00:00Z'

    def ingest(self, event_id='one', **updates):
        row = dict(event_id=event_id, observed_at='2026-10-30T00:00:00Z', platform='synthetic',
                   model='unknown', surface='manual', prompt='PRIVATE_PROMPT', answer='Go PRIVATE_ANSWER',
                   citations=[], evidence_url='https://secret.example/evidence')
        row.update(updates)
        self.service.ingest(self.sid, 'answers', dict(adapter='manual-v1', source='fixture', records=[row]))

    def export(self, **kwargs):
        return export_bundle(self.db, self.output, self.sid, self.start, self.end, **kwargs)

    def read(self):
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), {'report.json', 'quality.json', 'manifest.json', 'README.txt'})
            self.assertIsNone(archive.testzip())
            return {name: archive.read(name) for name in archive.namelist()}

    def test_shared_transaction_inventory_and_manifest(self):
        self.ingest()
        before = self.db.read_bytes()
        result = self.export()
        self.assertEqual(self.db.read_bytes(), before)
        data = self.read()
        manifest = json.loads(data['manifest.json'])
        self.assertEqual(result['samples'], 1)
        self.assertEqual(manifest['window']['start'], '2026-10-30T00:00:00+00:00')
        self.assertEqual(manifest['brand_config_id'], 'legacy')
        self.assertIsNone(manifest['inventory_id'])
        self.assertEqual(manifest['schema_version'], 8)
        for name, info in manifest['files'].items():
            self.assertEqual(info, dict(bytes=len(data[name]), sha256=hashlib.sha256(data[name]).hexdigest()))
        self.assertEqual(result['uncompressed_bytes'], sum(map(len, data.values())))
        joined = b''.join(data.values())
        for secret in (b'PRIVATE_PROMPT', b'PRIVATE_ANSWER', b'secret.example', str(self.db).encode()):
            self.assertNotIn(secret, joined)

    def test_snapshot_survives_concurrent_import_config_inventory_and_review(self):
        with self.service.db.connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
        self.ingest()
        config = self.service.brands.configuration(self.sid)
        def changed(service, *args, **kwargs):
            self.ingest('two')
            self.service.brands.review(self.sid, dict(config_id='legacy', brand_id=config['brands'][0]['id'],
                source='fixture', event_id='one', included=False, reason='local test'))
            self.service.brands.configure(self.sid, dict(brands=[dict(name='New')]))
            self.service.import_inventory(self.sid, dict(source='fixture', urls=['https://demo.example/page']))
            return quality_report(service, *args, **kwargs)
        with patch('geo.bundle.quality_report', side_effect=changed):
            self.export()
        data = self.read()
        basic, quality = json.loads(data['report.json']), json.loads(data['quality.json'])
        self.assertEqual((basic['answer_samples'], quality['samples']), (1, 1))
        self.assertEqual(basic['brand_mentions'], {'Go': 1})
        self.assertEqual(basic['brand_config_id'], 'legacy')
        self.assertIsNone(basic['coverage']['inventory_id'])
        live = self.service.report(self.sid)
        self.assertEqual(live['answer_samples'], 2)
        self.assertNotEqual(live['brand_config_id'], 'legacy')
        self.assertIsNotNone(live['coverage']['inventory_id'])

    def test_window_filters_empty_and_explicit_snapshots(self):
        self.ingest()
        self.ingest('end', observed_at=self.end)
        self.ingest('other', platform='Other')
        inventory = self.service.import_inventory(self.sid, dict(source='fixture', urls=['https://demo.example/page']))
        self.service.brands.configure(self.sid, dict(brands=[dict(name='New')]))
        self.export(filters=dict(source=' fixture ', platform='synthetic'), inventory_id=inventory['id'], brand_config_id='legacy')
        data = self.read()
        self.assertEqual(json.loads(data['quality.json'])['samples'], 1)
        self.assertEqual(json.loads(data['manifest.json'])['inventory_id'], inventory['id'])
        self.output = self.folder / 'empty.zip'
        self.export(filters=dict(platform='missing'))
        data = self.read()
        self.assertIsNone(json.loads(data['report.json'])['citation_rate'])
        self.assertIsNone(json.loads(data['quality.json'])['without_citations']['rate'])

    def test_invalid_input_missing_database_and_old_schema_leave_no_files(self):
        for values in (dict(start=None), dict(end=self.start), dict(filters={'kind': 'logs'}),
                       dict(filters={'typo': 'x'}), dict(site_id='missing'), dict(query_timeout_ms=0)):
            params = dict(database=self.db, destination=self.output, site_id=self.sid, start=self.start, end=self.end)
            params.update(values)
            with self.assertRaises((ValueError, LookupError)):
                export_bundle(**params)
        missing = self.folder / 'missing.sqlite3'
        with self.assertRaises(ValueError):
            export_bundle(missing, self.output, self.sid, self.start, self.end)
        self.assertFalse(missing.exists())
        with self.service.db.connect() as conn:
            conn.execute('PRAGMA user_version=7')
        with self.assertRaises(ValueError):
            self.export()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 7)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.folder.glob('.xing-report-*')), [])

    def test_existing_target_and_publish_race_never_overwrite(self):
        self.output.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(self.output.read_bytes(), b'keep')
        self.output.unlink()
        original = os.link
        def race(source, target):
            Path(target).write_bytes(b'racing owner')
            original(source, target)
        with patch('geo.bundle.os.link', side_effect=race), self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(self.output.read_bytes(), b'racing owner')
        self.assertEqual(list(self.folder.glob('.xing-report-*')), [])

    def test_write_publish_and_interrupt_failures_clean_temporary_file(self):
        for target, error in (('zipfile.ZipFile.writestr', OSError('disk full')),
                              ('os.link', OSError('hard links unsupported')),
                              ('zipfile.ZipFile.writestr', KeyboardInterrupt())):
            with patch('geo.bundle.' + target, side_effect=error), self.assertRaises(type(error)):
                self.export()
            self.assertFalse(self.output.exists())
            self.assertEqual(list(self.folder.glob('.xing-report-*')), [])

    def test_query_and_total_uncompressed_capacity(self):
        self.ingest()
        self.ingest('two', platform='Other')
        with self.assertRaises(CapacityExceeded):
            self.export(limits=Limits(query_events=1))
        result = self.export(limits=Limits(query_events=1), filters={'platform': 'synthetic'})
        self.output = self.folder / 'too-small.zip'
        with self.assertRaises(CapacityExceeded):
            self.export(filters={'platform': 'synthetic'}, limits=Limits(response_bytes=result['uncompressed_bytes']-1))
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.folder.glob('.xing-report-*')), [])

    def test_cli_exports_and_refuses_overwrite(self):
        self.ingest()
        command = [sys.executable, '-m', 'geo.bundle', '--db', str(self.db), '--output', str(self.output),
                   '--site', self.sid, '--start', self.start, '--end', self.end, '--platform', 'synthetic']
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['samples'], 1)
        before = self.output.read_bytes()
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already exists', result.stderr)
        self.assertEqual(before, self.output.read_bytes())

    def test_lock_and_snapshot_deadline_leave_no_artifact(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(sqlite3.OperationalError):
                self.export(busy_timeout_ms=20)
            writer.rollback()
        def exceed_deadline(*args, **kwargs):
            result = quality_report(*args, **kwargs)
            timer.return_value = 10
            return result
        with patch('geo.bundle.time.monotonic', return_value=0) as timer:
            with patch('geo.bundle.quality_report', side_effect=exceed_deadline):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'deadline'):
                    self.export(query_timeout_ms=1000)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.folder.glob('.xing-report-*')), [])
