from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile

from geo.bundle import export_bundle
from geo.bundle_check import verify, summary, bounded_read
from geo.db import Database
from geo.service import Service


class BundleCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        service = Service(Database(self.root / 'db.sqlite3'))
        sid = service.create_site(dict(name='demo\x1b[31m', url='https://demo.example', brands=['Go']))['id']
        self.service, self.sid = service, sid
        service.ingest(sid, 'answers', dict(adapter='manual-v1', source='fixture', records=[dict(
            event_id='one', observed_at='2026-10-30T12:00:00Z', platform='synthetic', surface='manual',
            prompt='private', answer='Go', citations=[], model='unknown')]))
        self.path = self.root / 'report.zip'
        export_bundle(service.db.path, self.path, sid, '2026-10-30T00:00:00Z', '2026-10-31T00:00:00Z')
        with zipfile.ZipFile(self.path) as archive:
            self.files = {name: archive.read(name) for name in archive.namelist()}

    def write(self, files=None, extra=None):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.path, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, data in (files or self.files).items():
                    archive.writestr(name, data)
                if extra:
                    archive.writestr(*extra)

    def mutate(self, name, change):
        files = dict(self.files)
        item = json.loads(files[name])
        change(item)
        files[name] = json.dumps(item).encode()
        if name != 'manifest.json':
            manifest = json.loads(files['manifest.json'])
            manifest['files'][name] = dict(bytes=len(files[name]), sha256=hashlib.sha256(files[name]).hexdigest())
            files['manifest.json'] = json.dumps(manifest).encode()
        self.write(files)

    def test_valid_and_safe_summary(self):
        checked = verify(self.path)
        self.assertEqual(checked['quality']['samples'], 1)
        text = summary(checked)
        self.assertIn('显式合成：1', text)
        self.assertIn('未分类不等于真实样本', text)
        self.assertNotIn('\x1b', text)
        self.assertNotIn('private', text)

    def test_brand_and_coverage_contradictions_rejected(self):
        for change in (lambda r: r['brand_mentions'].update(Go=2),
                       lambda r: r['brand_mentions'].update(unknown=0),
                       lambda r: r.update(mentioned_samples=0),
                       lambda r: r.update(coverage_rate=0.9),
                       lambda r: r['coverage'].update(pages=[{'url': 'https://demo.example/'}]),
                       lambda r: r.update(log_scope='excluded_by_answer_filter')):
            self.mutate('report.json', change)
            with self.assertRaises(ValueError):
                verify(self.path)

    def test_numeric_overflow_in_unused_json_field_rejected(self):
        files = dict(self.files)
        files['manifest.json'] = files['manifest.json'][:-1] + b', "extra": 1e999}'
        self.write(files)
        with self.assertRaises(ValueError):
            verify(self.path)

    def coverage_fixture(self, urls, filters=None):
        self.service.import_inventory(self.sid, dict(source='fixture', urls=urls))
        self.path.unlink()
        export_bundle(self.service.db.path, self.path, self.sid, '2026-10-30T00:00:00Z',
                      '2026-10-31T00:00:00Z', filters=filters)
        with zipfile.ZipFile(self.path) as archive:
            self.files = {name: archive.read(name) for name in archive.namelist()}
        return verify(self.path)

    def test_empty_and_excluded_inventory_are_valid(self):
        checked = self.coverage_fixture([])
        self.assertIsNone(checked['report']['coverage_rate'])
        checked = self.coverage_fixture(['https://demo.example/page'], {'kind': 'answers'})
        self.assertIsNone(checked['report']['declared_ai_requests'])

    def test_page_counts_and_coverage_tampering(self):
        self.service.ingest(self.sid, 'logs', dict(source='fixture', adapter='json-v1', records=[
            dict(event_id=str(i), observed_at='2026-10-30T12:00:00Z', url='https://demo.example/' + path,
                 status=status, user_agent='GPTBot') for i, (path, status) in enumerate(
                 [('ok', 200), ('ok', 100), ('bad', 404), ('outside', 301)])]))
        checked = self.coverage_fixture(['https://demo.example/ok', 'https://demo.example/bad', 'https://demo.example/none'])
        self.assertEqual(checked['report']['coverage_rate'], 2/3)
        for change in (lambda r: r['coverage'].update(observed_pages=0),
                       lambda r: r['coverage']['pages'][0].update(successful_requests=10),
                       lambda r: r['coverage']['pages'][0].update(observed=1),
                       lambda r: r['coverage'].update(outside_inventory_urls=[]),
                       lambda r: r['coverage']['pages'].append(r['coverage']['pages'][0]),
                       lambda r: r.update(declared_ai_requests=0)):
            self.mutate('report.json', change)
            with self.assertRaises(ValueError):
                verify(self.path)

    def test_multiple_brands_can_share_one_mentioned_sample(self):
        self.service.brands.configure(self.sid, dict(brands=[dict(name='Go'), dict(name='Second', aliases=['Go'])]))
        config = self.service.brands.configuration(self.sid)
        for brand in config['brands']:
            self.service.brands.review(self.sid, dict(config_id=config['id'], brand_id=brand['id'],
                source='fixture', event_id='one', included=True, reason='synthetic test'))
        checked = self.coverage_fixture([])
        self.assertEqual(sum(checked['report']['brand_mentions'].values()), 2)
        self.assertEqual(checked['report']['mentioned_samples'], 1)

    def test_chunk_reader_exact_limit_and_short_reads(self):
        class ShortReader(io.BytesIO):
            def read(self, size):
                self.assert_size = size
                return super().read(min(size, 3))
        stream = ShortReader(b'1234567')
        self.assertEqual(bounded_read(stream, 7), b'1234567')
        with self.assertRaises(ValueError):
            bounded_read(ShortReader(b'12345678'), 7)
        class LimitedRequest(io.BytesIO):
            def read(self, size):
                if size > 65536:
                    raise AssertionError('oversized buffer request')
                return super().read(size)
        self.assertEqual(bounded_read(LimitedRequest(b'tiny'), 16*1024*1024), b'tiny')

    def test_extra_duplicate_and_traversal_members(self):
        for name in ('report.json', '../outside', 'sub/report.json', '/absolute'):
            with self.subTest(name=name):
                self.write(extra=(name, b'bad'))
                with self.assertRaises(ValueError):
                    verify(self.path)
        self.assertFalse((self.root.parent / 'outside').exists())

    def test_hash_corruption_and_missing_member(self):
        files = dict(self.files, **{'README.txt': b'changed'})
        self.write(files)
        with self.assertRaises(ValueError):
            verify(self.path)
        del files['README.txt']
        self.write(files)
        with self.assertRaises(ValueError):
            verify(self.path)
        self.path.write_bytes(b'not a zip')
        with self.assertRaises(ValueError):
            verify(self.path)

    def test_rehashed_inconsistent_statistics_rejected(self):
        changes = [('quality.json', lambda q: q.update(samples=2)),
                   ('quality.json', lambda q: q['without_citations'].update(rate=0)),
                   ('quality.json', lambda q: q['without_citations'].update(evaluated_samples=True)),
                   ('quality.json', lambda q: q.update(synthetic_samples=0)),
                   ('report.json', lambda r: r.update(citation_rate=1)),
                   ('report.json', lambda r: r.update(brand_config_id='different')),
                   ('manifest.json', lambda m: m.update(version=True)),
                   ('manifest.json', lambda m: m.update(filters={'platform': 'different'})),
                   ('manifest.json', lambda m: m['window'].update(end_exclusive=False))]
        for name, change in changes:
            with self.subTest(name=name, change=change):
                self.mutate(name, change)
                with self.assertRaises(ValueError):
                    verify(self.path)

    def test_duplicate_keys_nonfinite_and_deep_json(self):
        for bad in (b'{"version":1,"version":1}', b'{"x":NaN}', b'[' * 1500 + b']' * 1500):
            self.write(dict(self.files, **{'manifest.json': bad}))
            with self.assertRaises(ValueError):
                verify(self.path)

    def test_compressed_and_uncompressed_limits(self):
        self.write(dict(self.files, **{'README.txt': b'0' * 100000}))
        with self.assertRaises(ValueError):
            verify(self.path, max_bytes=10000)
        self.path.write_bytes(b'0' * 80000)
        with self.assertRaises(ValueError):
            verify(self.path, max_bytes=1024)

    def test_symlink_and_unsupported_compression(self):
        for symlink in (True, False):
            with zipfile.ZipFile(self.path, 'w') as archive:
                for name, data in self.files.items():
                    info = zipfile.ZipInfo(name)
                    if name == 'README.txt':
                        if symlink:
                            info.create_system = 3
                            info.external_attr = (stat.S_IFLNK | 0o777) << 16
                        else:
                            info.compress_type = zipfile.ZIP_BZIP2
                    archive.writestr(info, data)
            with self.assertRaises(ValueError):
                verify(self.path)

    def test_cli_success_and_failure_no_partial_summary(self):
        command = [sys.executable, '-X', 'utf8', '-m', 'geo.bundle_check', str(self.path), '--summary']
        result = subprocess.run(command, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('本地 GEO 报告摘要', result.stdout)
        self.path.write_bytes(b'bad')
        result = subprocess.run(command, capture_output=True, encoding='utf-8')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('Traceback', result.stderr)
