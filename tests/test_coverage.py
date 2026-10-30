import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from geo.adapters import classify
from geo.catalog import catalog
from geo.db import Database
from geo.service import Service
from geo.api import server
from geo.collector import collect


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Service(Database(self.root / 'db.sqlite3'))
        self.sid = self.service.create_site(dict(name='coverage', url='https://example.com', brands=['品牌']))['id']

    def tearDown(self):
        self.tmp.cleanup()

    def inventory(self, paths):
        return self.service.import_inventory(self.sid, dict(source='fixture', urls=['https://example.com' + p for p in paths]))

    def logs(self, items):
        rows = [dict(event_id=str(i), observed_at='2026-10-29T00:00:00Z', url='https://example.com' + path,
                     status=status, user_agent=ua) for i, (path, status, ua) in enumerate(items)]
        return self.service.ingest(self.sid, 'logs', dict(source='test', adapter='json-v1', records=rows))

    def test_empty_and_missing_inventory_unknown(self):
        self.assertIsNone(self.service.report(self.sid)['coverage_rate'])
        self.assertIsNone(self.service.report(self.sid)['coverage']['inventory_size'])
        self.inventory([])
        result = self.service.report(self.sid)
        self.assertIsNone(result['coverage_rate'])
        self.assertEqual(result['coverage']['inventory_size'], 0)

    def test_success_failure_redirect_and_outside(self):
        self.inventory(['/ok', '/bad', '/redirect', '/none'])
        self.logs([('/ok', 200, 'Bytespider'), ('/ok', 200, 'GPTBot'), ('/bad', 404, 'GPTBot'),
                   ('/redirect', 301, 'GPTBot'), ('/outside', 200, 'GPTBot'), ('/none', 200, 'Baiduspider')])
        report = self.service.report(self.sid)
        self.assertEqual(report['coverage_rate'], 0.75)
        self.assertEqual(report['successful_coverage_rate'], 0.25)
        self.assertEqual(report['search_engine_requests'], 1)
        self.assertEqual(report['declared_ai_requests'], 5)
        self.assertEqual(report['coverage']['outside_inventory_urls'], ['https://example.com/outside'])
        pages = {p['url']: p for p in report['coverage']['pages']}
        self.assertEqual(pages['https://example.com/redirect']['redirect_requests'], 1)
        self.assertFalse(pages['https://example.com/redirect']['successful'])
        self.assertFalse(pages['https://example.com/none']['observed'])

    def test_snapshot_selection_preserves_denominator(self):
        first = self.inventory(['/a', '/b'])
        self.logs([('/a', 200, 'GPTBot')])
        self.inventory(['/a'])
        self.assertEqual(self.service.report(self.sid)['coverage_rate'], 1)
        self.assertEqual(self.service.report(self.sid, inventory_id=first['id'])['coverage_rate'], 0.5)
        self.assertEqual(self.service.report(self.sid, end='2026-10-29T00:00:00Z')['coverage_rate'], 0)

    def test_inventory_validation_and_site_isolation(self):
        first = self.inventory(['/a', '/a?secret=yes', '/a#section'])
        self.assertEqual(len(first['urls']), 1)
        for bad in (['https://foreign.example/'], ['bad'], 'not-a-list', [None]):
            with self.assertRaises(ValueError):
                self.service.import_inventory(self.sid, dict(source='test', urls=bad))
        self.assertEqual(self.service.inventory(self.sid)['id'], first['id'])
        other = self.service.create_site(dict(name='other', url='https://other.example', brands=['x']))['id']
        with self.assertRaises(LookupError):
            self.service.inventory(other, first['id'])

    def test_catalog_evidence_and_ambiguous_ua(self):
        self.assertEqual(classify('Bytespider/1.0')['crawler_provider'], 'ByteDance')
        self.assertEqual(classify('Baiduspider/2.0')['crawler_category'], 'search_engine')
        self.assertEqual(classify('KimiBot/1.0')['verification'], 'unknown')
        both = classify('GPTBot Bytespider')
        self.assertIsNone(both['declared_bot'])
        self.assertEqual(len(both['ambiguous_tokens']), 2)
        for item in catalog()['entries']:
            self.assertTrue(item['source_url'].startswith('https://'))
            self.assertEqual(item['reviewed_at'], '2026-10-29')
        self.assertTrue(all(not item['enabled'] for item in catalog()['pending']))

    def test_old_catalog_event_replay_keeps_historical_counts(self):
        items = [('/a', 200, 'GPTBot')]
        self.logs(items)
        with self.service.db.connect() as conn:
            row = json.loads(conn.execute('SELECT payload FROM events').fetchone()[0])
            for field in ('crawler_category', 'crawler_provider', 'crawler_source', 'crawler_evidence', 'ambiguous_tokens'):
                row.pop(field)
            row['rule_version'] = '2'
            conn.execute('UPDATE events SET payload=?', (json.dumps(row),))
        self.assertEqual(self.logs(items)['duplicates'], 1)
        self.assertEqual(self.service.report(self.sid)['declared_ai_requests'], 1)

    def test_collector_empty_ua_does_not_block_next_line(self):
        path = self.root / 'log.jsonl'
        rows = [dict(time_iso8601='2026-10-29T00:00:00Z', url='https://example.com/a', status=200, http_user_agent=ua)
                for ua in ('', 'GPTBot')]
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
        result = collect([path], self.root / 'state.json', dict(source='file'),
                         lambda body: self.service.ingest(self.sid, 'logs', body))
        self.assertEqual(result['acknowledged'], 2)
        self.assertEqual(self.service.report(self.sid)['declared_ai_requests'], 1)

    def test_http_inventory_catalog_and_snapshot(self):
        token = 'admin-only-test-token-at-least-24'
        app = server(self.service, token, port=0)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        def request(path, body=None, auth=token):
            req = Request(f'http://127.0.0.1:{app.server_port}' + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization': 'Bearer ' + auth})
            with urlopen(req, timeout=3) as response:
                return json.load(response)
        try:
            prefix = '/api/v1/sites/' + self.sid
            created = request(prefix + '/inventory', dict(source='http', urls=['https://example.com/a']))
            self.assertEqual(request(prefix + '/inventory')['id'], created['id'])
            report = request(prefix + '/report?inventory_id=' + created['id'])
            self.assertEqual(report['coverage_rate'], 0)
            self.assertEqual(request('/api/v1/crawlers')['version'], '3')
            for path in ('/api/v1/crawlers', prefix + '/inventory'):
                with self.assertRaises(HTTPError) as ctx:
                    request(path, auth='invalid')
                self.assertEqual(ctx.exception.code, 401)
        finally:
            app.shutdown()
            thread.join()
            app.server_close()
