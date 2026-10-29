import json
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from geo.db import Database
from geo.service import Service, Conflict
from geo.api import server


class MVPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(Database(self.temp.name + '/test.sqlite3'))
        self.site = self.service.create_site(dict(name='测试', url='https://example.com', brands=['星舟']))
        self.sid = self.site['id']
        self.log = dict(event_id='1', observed_at='2026-10-29T10:00:00+08:00',
                        url='https://example.com/docs?secret=hidden', status=404, user_agent='GPTBot')
        self.answer = dict(event_id='1', observed_at='2026-10-29T03:00:00Z', platform='Kimi',
                           prompt='推荐产品', answer='星舟值得比较', citations=['https://example.com/docs'])

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, rows, kind='logs', adapter='json-v1'):
        return self.service.ingest(self.sid, kind, dict(adapter=adapter, source='fixture', records=rows))

    def test_end_to_end_persistence_and_metrics(self):
        self.ingest([self.log])
        self.ingest([self.answer], 'answers', 'manual-v1')
        self.service = Service(Database(self.temp.name + '/test.sqlite3'))
        r = self.service.report(self.sid)
        self.assertEqual((r['declared_ai_requests'], r['failed_ai_requests'], r['cited_site_samples']), (1, 1, 1))
        self.assertEqual(r['brand_mentions'], {'星舟': 1})
        self.assertEqual(r['verified_ai_requests'], 0)
        self.assertIsNone(r['coverage_rate'])
        self.assertNotIn('secret', json.dumps(self.service.events(self.sid)))

    def test_retry_is_idempotent(self):
        self.ingest([self.log])
        self.assertEqual(self.ingest([self.log]), dict(inserted=0, duplicates=1))

    def test_conflict_rolls_back_batch(self):
        self.ingest([self.log])
        with self.assertRaises(Conflict):
            self.ingest([dict(self.log, event_id='2'), dict(self.log, status=200)])
        self.assertEqual(len(self.service.events(self.sid)), 1)

    def test_invalid_batch_is_atomic(self):
        with self.assertRaises(ValueError):
            self.ingest([self.log, dict(self.log, event_id='2', status=700)])
        self.assertEqual(self.service.events(self.sid), [])

    def test_wrong_site_and_missing_site(self):
        with self.assertRaises(ValueError):
            self.ingest([dict(self.log, url='https://evil.example')])
        with self.assertRaises(LookupError):
            self.service.report('missing')
        other = self.service.create_site(dict(name='other', url='https://other.example', brands=['other']))
        self.ingest([self.log])
        self.assertEqual(self.service.report(other['id'])['log_requests'], 0)

    def test_time_window_half_open(self):
        self.ingest([self.log])
        r = self.service.report(self.sid, end='2026-10-29T02:00:00Z')
        self.assertEqual(r['log_requests'], 0)
        self.assertEqual(self.service.report(self.sid, start='2026-10-29T02:00:00Z')['log_requests'], 1)
        with self.assertRaises(ValueError):
            self.ingest([dict(self.log, observed_at='2026-10-29T10:00:00')])

    def test_nginx_adapter_equivalence(self):
        self.ingest([dict(request_id='1', time_iso8601=self.log['observed_at'], url=self.log['url'],
                         status=404, http_user_agent='GPTBot')], adapter='nginx-json-v1')
        self.assertEqual(self.service.report(self.sid)['declared_ai_requests'], 1)

    def test_unknown_ua_not_ai(self):
        self.ingest([dict(self.log, user_agent='Mozilla/5.0')])
        self.assertEqual(self.service.report(self.sid)['declared_ai_requests'], 0)

    def test_zero_denominator_and_external_citation(self):
        self.assertIsNone(self.service.report(self.sid)['citation_rate'])
        self.ingest([dict(self.answer, citations=['https://example.com.evil.test/'])], 'answers', 'manual-v1')
        self.assertEqual(self.service.report(self.sid)['citation_rate'], 0)

    def test_http_auth_and_ingest(self):
        token = 'test-token-for-local-integration'
        app = server(self.service, token, port=0)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{app.server_port}'
        def request(path, body=None, auth=True):
            headers = {'Authorization': 'Bearer ' + token} if auth else {}
            data = json.dumps(body).encode() if body is not None else None
            with urlopen(Request(base + path, data=data, headers=headers), timeout=3) as r:
                return json.load(r)
        try:
            self.assertEqual(request('/health', auth=False)['status'], 'ok')
            with self.assertRaises(HTTPError) as ctx:
                request('/api/v1/sites', auth=False)
            self.assertEqual(ctx.exception.code, 401)
            result = request(f'/api/v1/sites/{self.sid}/logs', dict(adapter='json-v1', source='http', records=[self.log]))
            self.assertEqual(result['inserted'], 1)
            self.assertEqual(request(f'/api/v1/sites/{self.sid}/report')['failed_ai_requests'], 1)
            with self.assertRaises(HTTPError) as ctx:
                request('/api/v1/sites', [])
            self.assertEqual(ctx.exception.code, 400)
        finally:
            app.shutdown()
            thread.join()
            app.server_close()


if __name__ == '__main__':
    unittest.main()
