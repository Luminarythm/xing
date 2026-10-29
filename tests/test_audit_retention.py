import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

from geo.db import Database
from geo.service import Service, Conflict
from geo.limits import Limits, CapacityExceeded
from geo.retention import Retention
from geo.jobs import Jobs
from geo.paging import page, review_page
from geo.api import server


class AuditRetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.service = Service(Database(self.path))
        self.sid = self.service.create_site(dict(name='test', url='https://example.com', brands=['Go']))['id']
        self.retention = Retention(self.service)
        self.before = '2026-10-30T00:00:00Z'
        self.secret = 'local-synthetic-token-at-least-24-characters'

    def ingest(self, ids):
        self.service.ingest(self.sid, 'answers', dict(adapter='manual-v1', source='fixture', records=[
            dict(event_id=i, observed_at='2026-10-29T00:00:00Z', platform='synthetic', prompt='prompt', answer='Go')
            for i in ids]))

    def test_missing_live_database_is_not_silently_recreated(self):
        self.path.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            self.service.site(self.sid)
        self.assertFalse(self.path.exists())

    def test_retention_over_budget_does_not_save_partial_plan(self):
        self.ingest(['a', 'b'])
        self.service.limits = Limits(query_events=1)
        with self.assertRaises(CapacityExceeded):
            self.retention.preview(self.sid, self.before)
        with self.service.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM retention_plans').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)

    def review(self, event_id='a', included=True, reason='synthetic'):
        config = self.service.brands.configuration(self.sid, 'legacy')
        return self.service.brands.review(self.sid, dict(config_id='legacy', brand_id=config['brands'][0]['id'],
            source='fixture', event_id=event_id, included=included, reason=reason))

    def count(self, table):
        with self.service.db.connect() as conn:
            return conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_review_pages_keep_repeated_decisions_and_pin_config(self):
        self.ingest(['a'])
        expected = [self.review(included=bool(i % 2))['id'] for i in range(5)]
        self.service.limits = Limits(query_events=1)
        first = review_page(self.service, self.sid, self.secret, limit=2)
        self.review()
        self.service.brands.configure(self.sid, {'brands': [{'name': 'New'}]})
        second = review_page(self.service, self.sid, self.secret, limit=2, cursor=first['next_cursor'])
        third = review_page(self.service, self.sid, self.secret, limit=2, cursor=second['next_cursor'])
        self.assertEqual([r['id'] for p in (first, second, third) for r in p['items']], expected)
        self.assertEqual(third['brand_config_id'], 'legacy')
        self.assertIsNone(third['next_cursor'])
        self.assertEqual(review_page(self.service, self.sid, self.secret)['items'], [])

    def test_review_cursor_scope_signature_expiry_and_limits(self):
        self.ingest(['a', 'b'])
        self.review()
        self.review()
        cursor = review_page(self.service, self.sid, self.secret, limit=1)['next_cursor']
        other = self.service.create_site(dict(name='other', url='https://other.example', brands=['Go']))['id']
        for kwargs in ({'cursor': cursor+'x'}, {'cursor': cursor, 'brand_config_id': 'legacy'}, {'limit': 501}, {'limit': 0}):
            with self.assertRaises(ValueError):
                review_page(self.service, self.sid, self.secret, **kwargs)
        with self.assertRaises(ValueError):
            review_page(self.service, other, self.secret, cursor=cursor)
        with self.assertRaises(ValueError):
            review_page(self.service, self.sid, 'rotated-secret', cursor=cursor)
        with patch('geo.cursors.time.time', return_value=99999999999):
            with self.assertRaises(ValueError):
                review_page(self.service, self.sid, self.secret, cursor=cursor)
        with self.assertRaises(ValueError):
            page(self.service, self.sid, self.secret, cursor=cursor)
        event_cursor = page(self.service, self.sid, self.secret, limit=1)['next_cursor']
        with self.assertRaises(ValueError):
            review_page(self.service, self.sid, self.secret, cursor=event_cursor)

    def test_review_cursor_deletion_and_sequence_reuse(self):
        self.ingest(['a'])
        self.review()
        self.review()
        first = review_page(self.service, self.sid, self.secret, limit=1)
        plan = self.retention.preview(self.sid, self.before)
        self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        self.service.ingest(self.sid, 'answers', dict(adapter='manual-v1', source='fixture', records=[
            dict(event_id='new', observed_at=self.before, platform='synthetic', prompt='prompt', answer='Go')]))
        self.review('new')
        self.assertEqual(review_page(self.service, self.sid, self.secret, cursor=first['next_cursor'])['items'], [])
        fresh = review_page(self.service, self.sid, self.secret)
        self.assertGreater(fresh['items'][0]['sequence'], first['review_high_watermark'])

    def test_review_page_bytes_checked_before_output(self):
        self.ingest(['a'])
        self.review(reason='中文' * 500)
        self.service.limits = Limits(query_bytes=1024)
        with self.assertRaises(CapacityExceeded):
            review_page(self.service, self.sid, self.secret)

    def test_retention_combines_event_review_and_job_budgets(self):
        self.ingest(['a'])
        self.review()
        Jobs(self.service).enqueue(self.sid, dict(request_key='job', prompt='synthetic', observed_at='2026-10-29T00:00:00Z'))
        self.service.limits = Limits(query_events=2)
        with self.assertRaises(CapacityExceeded):
            self.retention.preview(self.sid, self.before)
        self.assertEqual(self.count('retention_plans'), 0)
        self.service.limits = Limits(query_events=3)
        plan = self.retention.preview(self.sid, self.before)
        self.assertEqual(plan['selected_entities'], 3)
        self.service.limits = Limits(query_events=2)
        with self.assertRaises(CapacityExceeded):
            self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        for table in ('events', 'mention_reviews', 'sample_jobs'):
            self.assertEqual(self.count(table), 1)
        self.assertEqual(self.count('retention_watermarks'), 0)
        self.service.limits = Limits(query_events=3)
        result = self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        self.assertEqual(result['removed_jobs'], 1)
        self.assertEqual(result, self.retention.execute(self.sid, plan['id'], 'delete-previewed-events'))

    def test_retention_identifier_bytes_are_bounded(self):
        self.ingest(['中' * 190 + str(i) for i in range(3)])
        self.service.limits = Limits(query_bytes=1024)
        with self.assertRaises(CapacityExceeded):
            self.retention.preview(self.sid, self.before)
        self.assertEqual(self.count('retention_plans'), 0)
        self.assertEqual(self.count('events'), 3)

    def test_plan_size_and_execution_response_preflight(self):
        jobs = Jobs(self.service)
        for i in range(25):
            jobs.enqueue(self.sid, dict(request_key=str(i), prompt='synthetic', observed_at='2026-10-29T00:00:00Z'))
        self.service.limits = Limits(response_bytes=1024)
        with self.assertRaises(CapacityExceeded):
            self.retention.preview(self.sid, self.before)
        self.assertEqual(self.count('retention_plans'), 0)
        self.service.limits = Limits()
        plan = self.retention.preview(self.sid, self.before)
        size = len(json.dumps(plan, ensure_ascii=False).encode())
        self.service.limits = Limits(response_bytes=size)
        with self.assertRaises(CapacityExceeded):
            self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        self.assertEqual(self.count('sample_jobs'), 25)
        self.assertEqual(self.count('retention_watermarks'), 0)
        with self.service.db.connect() as conn:
            saved = json.loads(conn.execute('SELECT payload FROM retention_plans').fetchone()[0])
        self.assertEqual(saved['state'], 'preview')

    def test_new_arrival_after_preview_cannot_trigger_partial_deletion(self):
        self.ingest(['a'])
        plan = self.retention.preview(self.sid, self.before)
        self.ingest(['b'])
        self.service.limits = Limits(query_events=1)
        with self.assertRaises(CapacityExceeded):
            self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        self.assertEqual(self.count('events'), 2)
        self.service.limits = Limits()
        with self.assertRaises(Conflict):
            self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        self.assertEqual(self.count('events'), 2)

    def test_concurrent_review_and_delete_are_serialized(self):
        self.ingest(['a'])
        plan = self.retention.preview(self.sid, self.before)
        barrier = threading.Barrier(2)
        def review():
            barrier.wait(timeout=3)
            try:
                self.review()
                return 'reviewed'
            except LookupError:
                return 'gone'
        def delete():
            barrier.wait(timeout=3)
            try:
                self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
                return 'deleted'
            except Conflict:
                return 'changed'
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(review), pool.submit(delete)
            result = (a.result(timeout=5), b.result(timeout=5))
        self.assertIn(result, (('reviewed', 'changed'), ('gone', 'deleted')))
        remaining = int(result[0] == 'reviewed')
        self.assertEqual(self.count('events'), remaining)
        self.assertEqual(self.count('mention_reviews'), remaining)

    def test_http_review_pages_and_preview_capacity(self):
        self.ingest(['a', 'b'])
        expected = [self.review()['id'] for _ in range(3)]
        app = server(self.service, self.secret, port=0)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        prefix = f'http://127.0.0.1:{app.server_port}/api/v1/sites/{self.sid}/'
        def call(path, body=None, authorized=True):
            request = Request(prefix+path, data=json.dumps(body).encode() if body is not None else None,
                              headers={'Authorization': 'Bearer ' + self.secret} if authorized else {})
            try:
                with urlopen(request, timeout=4) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)
        try:
            status, first = call('mention-reviews?limit=2')
            self.assertEqual(status, 200)
            _, second = call('mention-reviews?' + urlencode(dict(limit=2, cursor=first['next_cursor'])))
            self.assertEqual([r['id'] for r in first['items']+second['items']], expected)
            self.assertEqual(call('mention-reviews', authorized=False)[0], 401)
            self.assertEqual(call('mention-reviews?limit=0')[0], 400)
            self.assertEqual(call('mention-reviews?limit=1&limit=2')[0], 400)
            self.service.limits = Limits(query_events=1)
            status, error = call('retention-preview', {'before': self.before})
            self.assertEqual(status, 422)
            self.assertEqual(error['code'], 'capacity_exceeded')
            self.assertEqual(self.count('retention_plans'), 0)
        finally:
            app.shutdown()
            app.server_close()
            thread.join(3)
