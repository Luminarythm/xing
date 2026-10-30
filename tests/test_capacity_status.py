import json
import csv
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from geo.db import Database
from geo.service import Service
from geo.api import server
from geo.limits import Limits, CapacityExceeded, json_bytes
from geo.reporting import events_csv
from geo.paging import page
from geo.jobs import Jobs


class CapacityStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(Database(Path(self.tmp.name) / 'test.sqlite3'))
        self.sid = self.service.create_site(dict(name='test', url='https://example.com', brands=['Go']))['id']

    def ingest(self, count=1, answer='Go'):
        self.service.ingest(self.sid, 'answers', dict(adapter='manual-v1', source='fixture', records=[
            dict(event_id=str(i), observed_at='2026-10-29T00:00:00Z', platform='Kimi', prompt='prompt', answer=answer)
            for i in range(count)]))

    def test_padded_kind_has_same_report_semantics(self):
        self.ingest()
        expected = self.service.report(self.sid, filters={'kind': 'answers'})
        self.assertEqual(self.service.report(self.sid, filters={'kind': ' answers '}), expected)

    def test_log_only_report_does_not_claim_zero_answer_samples(self):
        self.ingest()
        report = self.service.report(self.sid, filters={'kind': 'logs'})
        self.assertIsNone(report['answer_samples'])
        self.assertIsNone(report['brand_mentions'])
        self.assertEqual(report['answer_scope'], 'excluded_by_log_filter')

    def test_count_limit_is_exact_and_filters_run_first(self):
        self.ingest(3)
        self.service.limits = Limits(query_events=3)
        self.assertEqual(self.service.report(self.sid)['answer_samples'], 3)
        self.service.limits = Limits(query_events=2)
        with self.assertRaises(CapacityExceeded):
            self.service.report(self.sid)
        self.assertEqual(self.service.events(self.sid, filters={'platform': 'Other'}), [])
        self.assertEqual(self.service.events(self.sid, start='2026-10-30T00:00:00Z'), [])
        self.assertEqual(len(page(self.service, self.sid, 'secret', limit=2)['items']), 2)

    def test_byte_budget_counts_utf8_and_exact_boundary(self):
        self.ingest(answer='中文' * 300)
        with self.service.db.connect() as conn:
            size = conn.execute('SELECT length(CAST(payload AS BLOB)) FROM events').fetchone()[0]
        self.service.limits = Limits(query_bytes=size)
        self.assertEqual(len(self.service.events(self.sid)), 1)
        self.service.limits = Limits(query_bytes=size-1)
        with self.assertRaises(CapacityExceeded):
            self.service.events(self.sid)
        with self.assertRaises(CapacityExceeded):
            page(self.service, self.sid, 'secret')

    def test_csv_budget_includes_bom_quotes_and_formula_escaping(self):
        self.ingest(answer='=中文,"text"\n' * 50)
        events = self.service.events(self.sid)
        csv_text = events_csv(events)
        size = len(('\ufeff' + csv_text).encode('utf-8'))
        self.assertEqual(events_csv(events, size), csv_text)
        with self.assertRaises(CapacityExceeded):
            events_csv(events, size-1)
        value = {'text': '中文' * 500}
        size = len(json.dumps(value, ensure_ascii=False).encode())
        self.assertEqual(json.loads(json_bytes(value, size)), value)
        with self.assertRaises(CapacityExceeded):
            json_bytes(value, size-1)

    def test_limit_config_validates_without_exposing_secret(self):
        with patch.dict('os.environ', {'GEO_MAX_QUERY_EVENTS': '25', 'GEO_MAX_QUERY_BYTES': '4096', 'GEO_MAX_RESPONSE_BYTES': '8192'}):
            self.assertEqual(Limits.from_env(), Limits(25, 4096, 8192))
        for kwargs in ({'query_events': 0}, {'query_events': True}, {'query_bytes': 1023}, {'response_bytes': -1}):
            with self.assertRaises(ValueError):
                Limits(**kwargs)
        with patch.dict('os.environ', {'GEO_MAX_QUERY_EVENTS': 'oops'}):
            with self.assertRaises(ValueError):
                Limits.from_env()

    def test_projection_uses_latest_selected_reviews_without_loading_history(self):
        self.ingest(2)
        config = self.service.brands.configuration(self.sid)
        body = dict(config_id='legacy', brand_id=config['brands'][0]['id'], source='fixture',
                    event_id='0', included=True, reason='synthetic')
        first = page(self.service, self.sid, 'secret', limit=1)
        for included in (True, False, True, False):
            self.service.brands.review(self.sid, dict(body, included=included))
        self.service.limits = Limits(query_events=2)
        with self.assertRaises(CapacityExceeded):
            self.service.brands.reviews(self.sid, 'legacy')
        self.assertEqual(self.service.report(self.sid)['mentioned_samples'], 1)
        second = page(self.service, self.sid, 'secret', limit=1, cursor=first['next_cursor'])
        self.assertEqual(second['items'][0]['mentions'], ['Go'])
        events = self.service.events(self.sid)
        self.assertIs(events[0]['mention_details'][0]['included'], False)
        self.assertEqual(events[1]['mentions'], ['Go'])

    def start_http(self):
        secret = 'local-test-token-at-least-24-characters'
        app = server(self.service, secret, port=0)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            app.shutdown()
            app.server_close()
            thread.join(3)
        self.addCleanup(cleanup)
        def request(path, authorized=True):
            headers = {'Authorization': 'Bearer ' + secret} if authorized else {}
            try:
                with urlopen(Request(f'http://127.0.0.1:{app.server_port}'+path, headers=headers), timeout=4) as response:
                    return response.status, response.read()
            except HTTPError as error:
                with error:
                    return error.code, error.read()
        return request

    def test_http_capacity_returns_json_without_partial_csv(self):
        self.ingest(2, answer='中文' * 800)
        request = self.start_http()
        prefix = f'/api/v1/sites/{self.sid}/'
        self.service.limits = Limits(query_events=1)
        for endpoint in ('report', 'export.csv'):
            status, body = request(prefix+endpoint)
            self.assertEqual(status, 422)
            self.assertEqual(json.loads(body)['code'], 'capacity_exceeded')
        self.service.limits = Limits(response_bytes=1024)
        for endpoint in ('export.csv', 'events?limit=1'):
            status, body = request(prefix+endpoint)
            self.assertEqual(status, 422)
            self.assertEqual(json.loads(body)['code'], 'capacity_exceeded')
        self.assertEqual(request(prefix+'report?platform=Other')[0], 422)  # Report metadata also has a budget.
        self.service.limits = Limits()
        self.assertEqual(request(prefix+'export.csv')[0], 200)
        self.assertEqual(request(prefix+'report')[0], 200)

    def test_status_auth_counts_and_no_sensitive_content(self):
        self.ingest(2, answer='PRIVATE_ANSWER')
        jobs = Jobs(self.service, clock=lambda: 1000)
        jobs.enqueue(self.sid, dict(request_key='one', prompt='PRIVATE_PROMPT'))
        jobs.claim()
        jobs.enqueue(self.sid, dict(request_key='two', prompt='PRIVATE_PROMPT'))
        request = self.start_http()
        self.assertEqual(request('/api/v1/status', False)[0], 401)
        status, raw = request('/api/v1/status')
        self.assertEqual(status, 200)
        result = json.loads(raw)
        self.assertEqual(result['counts']['events'], 2)
        self.assertEqual(result['jobs']['due'], 1)
        self.assertEqual(result['jobs']['expired_leases'], 1)
        self.assertEqual(result['providers'], ['mock'])
        for sensitive in ('PRIVATE_', 'test.sqlite3', 'local-test-token', 'request_key'):
            self.assertNotIn(sensitive, raw.decode())
        self.assertEqual(request('/api/v1/status?unknown=x')[0], 400)

    def test_status_missing_database_returns_503_without_recreating_file(self):
        request = self.start_http()
        Path(self.service.db.path).unlink()
        status, raw = request('/api/v1/status')
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {'error': 'database unavailable'})
        self.assertFalse(Path(self.service.db.path).exists())
        self.assertEqual(request('/health', False)[0], 200)

    def test_status_rejects_incomplete_schema(self):
        with self.service.db.connect() as conn:
            conn.execute('DROP TABLE ingest_keys')
        request = self.start_http()
        self.assertEqual(request('/api/v1/status')[0], 503)

    def test_bulk_report_csv_and_paged_events_agree(self):
        for start in range(0, 1200, 400):
            self.service.ingest(self.sid, 'answers', dict(adapter='manual-v1', source='bulk', records=[
                dict(event_id=str(i), observed_at='2026-10-29T00:00:00Z', platform='Kimi' if i % 2 else 'Other',
                     prompt='synthetic', answer='Go') for i in range(start, start+400)]))
        filters = dict(platform=' Kimi ', source='bulk')
        self.service.limits = Limits(query_events=600)
        self.assertEqual(self.service.report(self.sid, filters=filters)['answer_samples'], 600)
        csv_rows = list(csv.DictReader(io.StringIO(events_csv(self.service.events(self.sid, filters=filters)))))
        self.assertEqual(len(csv_rows), 600)
        ids, cursor = [], None
        while True:
            result = page(self.service, self.sid, 'secret', filters=filters, limit=137, cursor=cursor)
            ids.extend(e['event_id'] for e in result['items'])
            cursor = result['next_cursor']
            if cursor is None:
                break
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {r['event_id'] for r in csv_rows})
        self.service.limits = Limits(query_events=599)
        with self.assertRaises(CapacityExceeded):
            self.service.report(self.sid, filters=filters)
