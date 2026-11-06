import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from geo.bundle import export_bundle
from geo.bundle_compare import compare, summary, difference
from geo.db import Database
from geo.service import Service


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = Service(Database(self.root / 'db.sqlite3'))
        self.sid = self.service.create_site(dict(name='fixture', url='https://demo.example', brands=['Go']))['id']
        self.windows = [('2026-10-29T00:00:00Z', '2026-10-30T00:00:00Z'),
                        ('2026-10-30T00:00:00Z', '2026-10-31T00:00:00Z')]

    def ingest(self, day, identity, citations=None):
        self.service.ingest(self.sid, 'answers', dict(source='fixture', adapter='manual-v1', records=[dict(
            event_id=identity, observed_at=f'2026-10-{day}T12:00:00Z', platform='synthetic', surface='manual',
            model='unknown', prompt='private', answer='Go', citations=citations or [])]))

    def export(self, index, **kwargs):
        path = self.root / f'{index}-{len(list(self.root.glob("*.zip")))}.zip'
        export_bundle(self.service.db.path, path, self.sid, *self.windows[index], **kwargs)
        return path

    def test_rates_use_percentage_points_and_show_denominators(self):
        self.ingest(29, 'one', ['https://demo.example/'])
        self.ingest(30, 'two')
        self.ingest(30, 'three', ['https://demo.example/'])
        result = compare(self.export(0), self.export(1))
        self.assertEqual(result['metrics']['citation_rate']['delta'], -50)
        self.assertEqual(result['metrics']['answer_samples']['delta'], 1)
        self.assertIn('sample_denominator_changed', result['warnings'])
        self.assertTrue(result['quality']['without_citations']['denominator_changed'])
        self.assertEqual(result['before']['synthetic_samples'], 1)
        self.assertIn('百分点', summary(result))
        self.assertNotIn('private', json.dumps(result))

    def test_empty_unknown_and_zero_are_distinct(self):
        before = self.export(0)
        self.ingest(30, 'one')
        result = compare(before, self.export(1))
        self.assertIsNone(result['metrics']['citation_rate']['delta'])
        self.assertEqual(result['metrics']['citation_rate']['after'], 0)
        self.assertEqual(result['metrics']['citation_rate']['reason'], 'unknown_value')
        self.assertIsNone(result['metrics']['coverage_rate']['delta'])
        self.assertEqual(difference(0, 0)['delta'], 0)

    def test_changed_brand_and_inventory_suppress_only_related_deltas(self):
        self.ingest(29, 'one')
        self.ingest(30, 'two')
        self.service.import_inventory(self.sid, dict(source='fixture', urls=['https://demo.example/a']))
        before = self.export(0)
        self.service.brands.configure(self.sid, dict(brands=[dict(name='New')]))
        self.service.import_inventory(self.sid, dict(source='fixture', urls=['https://demo.example/b']))
        result = compare(before, self.export(1))
        self.assertEqual(result['metrics']['mentioned_samples']['reason'], 'brand_configuration_changed')
        self.assertEqual(result['metrics']['coverage_rate']['reason'], 'inventory_changed')
        self.assertIsNone(result['brands']['Go']['delta'])
        self.assertIsNone(result['brands']['New']['delta'])
        self.assertEqual(result['metrics']['citation_rate']['delta'], 0)

    def test_filter_and_site_mismatch_rejected(self):
        before = self.export(0)
        with self.assertRaises(ValueError):
            compare(before, self.export(1, filters={'platform': 'synthetic'}))
        self.sid = self.service.create_site(dict(name='other', url='https://other.example', brands=['Go']))['id']
        with self.assertRaises(ValueError):
            compare(before, self.export(1))

    def test_reverse_overlap_and_same_window_rejected(self):
        before, after = self.export(0), self.export(1)
        for first, second in ((after, before), (before, before)):
            with self.assertRaises(ValueError):
                compare(first, second)
        self.windows[1] = ('2026-10-29T12:00:00Z', '2026-10-31T00:00:00Z')
        with self.assertRaises(ValueError):
            compare(before, self.export(1))

    def test_gap_and_unequal_durations_are_explicit(self):
        self.windows[1] = ('2026-10-31T00:00:00Z', '2026-11-02T00:00:00Z')
        result = compare(self.export(0), self.export(1))
        self.assertIn('gap_between_windows', result['warnings'])
        self.assertIn('unequal_window_duration', result['warnings'])
        self.assertEqual(result['after']['duration_seconds'], 172800)

    def test_same_id_changed_definition_and_missing_metadata(self):
        before, after = self.export(0), self.export(1)
        with zipfile.ZipFile(after) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        report = json.loads(files['report.json'])
        report['brand_definitions'][0]['aliases'] = ['Changed']
        def write():
            files['report.json'] = json.dumps(report).encode()
            manifest = json.loads(files['manifest.json'])
            manifest['files']['report.json'] = dict(bytes=len(files['report.json']),
                sha256=hashlib.sha256(files['report.json']).hexdigest())
            files['manifest.json'] = json.dumps(manifest).encode()
            with zipfile.ZipFile(after, 'w') as archive:
                for name, data in files.items():
                    archive.writestr(name, data)
        write()
        self.assertEqual(compare(before, after)['metrics']['mentioned_samples']['reason'], 'brand_configuration_changed')
        del report['site']['origin']
        write()
        with self.assertRaises(ValueError):
            compare(before, after)

    def test_input_budget_rejection_does_not_modify_bundles(self):
        before, after = self.export(0), self.export(1)
        original = (before.read_bytes(), after.read_bytes())
        with self.assertRaises(ValueError):
            compare(before, after, max_bytes=1024)
        self.assertEqual(original, (before.read_bytes(), after.read_bytes()))

    def test_excluded_logs_keep_unknown_and_cli_failure_is_clean(self):
        before, after = (self.export(i, filters={'kind': 'answers'}) for i in (0, 1))
        result = compare(before, after)
        self.assertIsNone(result['metrics']['log_requests']['delta'])
        command = [sys.executable, '-X', 'utf8', '-m', 'geo.bundle_compare', str(before), str(after)]
        run = subprocess.run(command, capture_output=True, encoding='utf-8')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['format'], 'xing-report-comparison')
        after.write_bytes(b'corrupt')
        run = subprocess.run(command, capture_output=True, encoding='utf-8')
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(run.stdout, '')
        self.assertNotIn('Traceback', run.stderr)
