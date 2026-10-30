import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode
from geo.db import Database
from geo.service import Service, Conflict
from geo.api import server
from geo.paging import page
from geo.retention import Retention
from geo.jobs import Jobs
from geo.backup import copy_database


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Service(Database(self.root / 'live.sqlite3'))
        self.sid = self.service.create_site(dict(name='test', url='https://example.com', brands=['Go']))['id']
        self.secret = 'local-test-token-at-least-24-characters'
        self.now = 1000
        self.jobs = Jobs(self.service, clock=lambda: self.now)
        self.retention = Retention(self.service)

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, ids, at='2026-10-29T00:00:00Z'):
        return self.service.ingest(self.sid, 'answers', dict(adapter='manual-v1', source='fixture', records=[
            dict(event_id=i, observed_at=at, platform='Kimi', prompt='prompt', answer='Go') for i in ids]))

    def test_page_no_duplicates_and_new_arrivals_excluded(self):
        self.ingest(['a', 'b', 'c', 'd'])
        first = page(self.service, self.sid, self.secret, limit=2)
        self.ingest(['later'])
        second = page(self.service, self.sid, self.secret, limit=2, cursor=first['next_cursor'])
        self.assertEqual([e['event_id'] for e in first['items']+second['items']], ['a', 'b', 'c', 'd'])
        self.assertIsNone(second['next_cursor'])

    def test_cursor_tamper_scope_and_filters_rejected(self):
        self.ingest(['a', 'b'])
        cursor = page(self.service, self.sid, self.secret, limit=1)['next_cursor']
        for kwargs in (dict(cursor=cursor+'x'), dict(cursor=cursor, filters={'platform': 'Other'}), dict(cursor='invalid'), dict(limit=501)):
            with self.assertRaises(ValueError):
                page(self.service, self.sid, self.secret, **kwargs)
        other = self.service.create_site(dict(name='other', url='https://other.example', brands=['x']))['id']
        with self.assertRaises(ValueError):
            page(self.service, other, self.secret, cursor=cursor)

    def test_page_pins_brand_and_review_history(self):
        self.ingest(['a', 'b'])
        config = self.service.brands.configuration(self.sid)
        first = page(self.service, self.sid, self.secret, limit=1)
        self.service.brands.review(self.sid, dict(config_id='legacy', brand_id=config['brands'][0]['id'],
            source='fixture', event_id='b', included=False, reason='new review'))
        self.service.brands.configure(self.sid, {'brands': [{'name': 'Other'}]})
        second = page(self.service, self.sid, self.secret, limit=1, cursor=first['next_cursor'])
        self.assertEqual(second['brand_config_id'], 'legacy')
        self.assertEqual(second['items'][0]['mentions'], ['Go'])

    def test_filter_before_page_limit(self):
        self.ingest(['a', 'b', 'c'])
        result = page(self.service, self.sid, self.secret, filters={'platform': 'Kimi'}, limit=2)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(page(self.service, self.sid, self.secret, filters={'platform': 'Other'})['items'], [])

    def test_retention_preview_confirm_idempotency_and_watermark(self):
        self.ingest(['old'])
        self.ingest(['boundary'], at='2026-10-30T00:00:00Z')
        plan = self.retention.preview(self.sid, '2026-10-30T00:00:00Z')
        self.assertEqual(plan['count'], 1)
        self.assertEqual(self.service.report(self.sid)['answer_samples'], 2)
        with self.assertRaises(ValueError):
            self.retention.execute(self.sid, plan['id'], 'yes')
        done = self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        self.assertEqual(done, self.retention.execute(self.sid, plan['id'], 'delete-previewed-events'))
        self.assertEqual(self.service.report(self.sid)['answer_samples'], 1)
        with self.assertRaises(ValueError):
            self.ingest(['old'])
        with self.service.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM event_positions').fetchone()[0], 1)

    def test_retention_changed_selection_requires_new_preview(self):
        self.ingest(['a'])
        plan = self.retention.preview(self.sid, '2026-10-30T00:00:00Z')
        self.ingest(['b'])
        with self.assertRaises(Conflict):
            self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        self.assertEqual(self.service.report(self.sid)['answer_samples'], 2)
        with self.assertRaises(LookupError):
            self.retention.execute('other', plan['id'], 'delete-previewed-events')

    def test_retention_removes_reviews_jobs_and_fences_running_worker(self):
        job = self.jobs.enqueue(self.sid, dict(request_key='one', prompt='synthetic', observed_at='2026-10-29T00:00:00Z'))
        claim = self.jobs.claim()
        self.ingest(['a'])
        config = self.service.brands.configuration(self.sid)
        self.service.brands.review(self.sid, dict(config_id='legacy', brand_id=config['brands'][0]['id'], source='fixture', event_id='a', included=True, reason='review'))
        plan = self.retention.preview(self.sid, '2026-10-30T00:00:00Z')
        self.assertEqual((len(plan['job_ids']), len(plan['review_sequences'])), (1, 1))
        self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        self.assertFalse(self.jobs.finish(claim))
        self.assertEqual(self.jobs.list(self.sid), [])
        self.assertEqual(self.service.brands.reviews(self.sid, 'legacy'), [])

    def test_deleted_high_sequence_not_reused_in_existing_cursor(self):
        self.ingest(['keep'], at='2026-10-31T00:00:00Z')
        self.ingest(['old'])
        first = page(self.service, self.sid, self.secret, limit=1)
        plan = self.retention.preview(self.sid, '2026-10-30T00:00:00Z')
        self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        self.ingest(['new'], at='2026-10-31T00:00:00Z')
        self.assertEqual(page(self.service, self.sid, self.secret, cursor=first['next_cursor'])['items'], [])

    def test_job_idempotency_restart_and_lease_fence(self):
        body = dict(request_key='one', prompt='synthetic')
        job = self.jobs.enqueue(self.sid, body)
        self.assertEqual(self.jobs.enqueue(self.sid, body)['id'], job['id'])
        with self.assertRaises(ValueError):
            self.jobs.enqueue(self.sid, dict(body, prompt='different'))
        old = self.jobs.claim()
        self.assertIsNone(self.jobs.claim())
        self.now += 31
        restarted = Jobs(Service(Database(self.root / 'live.sqlite3')), clock=lambda: self.now)
        fresh = restarted.claim()
        self.assertFalse(restarted.finish(old))
        self.assertTrue(restarted.finish(fresh))
        self.assertFalse(restarted.finish(fresh))
        self.assertEqual(self.service.report(self.sid)['answer_samples'], 1)
        self.assertEqual(restarted.list(self.sid)[0]['state'], 'succeeded')

    def test_job_failure_backoff_and_attempt_cap(self):
        self.jobs.enqueue(self.sid, dict(request_key='one', prompt='synthetic'))
        for attempt in range(3):
            claim = self.jobs.claim()
            self.assertIsNotNone(claim)
            self.jobs.finish(claim, fail=True)
            self.assertIsNone(self.jobs.claim())
            self.now += 10
        self.assertEqual(self.jobs.list(self.sid)[0]['state'], 'failed')
        self.assertIsNone(self.jobs.claim())
        with self.assertRaises(ValueError):
            self.jobs.enqueue(self.sid, dict(request_key='real', prompt='x', provider='real'))

    def test_backup_restore_preserves_report_and_refuses_overwrite(self):
        self.ingest(['a'])
        expected = self.service.report(self.sid)
        backup = self.root / 'backup.sqlite3'
        restored = self.root / 'restored.sqlite3'
        copy_database(self.root / 'live.sqlite3', backup)
        self.ingest(['b'])
        copy_database(backup, restored)
        self.assertEqual(Service(Database(restored)).report(self.sid), expected)
        with self.assertRaises(FileExistsError):
            copy_database(backup, restored)
        with self.assertRaises(ValueError):
            copy_database(backup, backup)
        self.assertEqual(self.service.report(self.sid)['answer_samples'], 2)

    def test_bad_backup_rejected_without_leaving_output(self):
        source, target = self.root / 'bad.sqlite3', self.root / 'target.sqlite3'
        source.write_text('not a database')
        with self.assertRaises(Exception):
            copy_database(source, target)
        self.assertFalse(target.exists())

    def test_expired_retention_plan_does_not_delete(self):
        self.ingest(['a'])
        plan = self.retention.preview(self.sid, '2026-10-30T00:00:00Z')
        with patch('geo.retention.time.time', return_value=plan['expires']+1):
            with self.assertRaises(ValueError):
                self.retention.execute(self.sid, plan['id'], 'delete-previewed-events')
        self.assertEqual(self.service.report(self.sid)['answer_samples'], 1)

    def test_crashed_jobs_stop_after_three_leases(self):
        self.jobs.enqueue(self.sid, dict(request_key='crash', prompt='fixture'))
        for _ in range(3):
            self.assertIsNotNone(self.jobs.claim())
            self.now += 31
        self.assertIsNone(self.jobs.claim())
        self.assertEqual(self.jobs.list(self.sid)[0]['state'], 'failed')

    def test_http_paging_retention_and_job_permissions(self):
        self.ingest(['a', 'b'])
        app = server(self.service, self.secret, port=0)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        prefix = f'http://127.0.0.1:{app.server_port}/api/v1/sites/{self.sid}/'
        def call(path, body=None, token=None):
            req = Request(prefix+path, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization': 'Bearer ' + (token or self.secret)})
            with urlopen(req, timeout=3) as response:
                return json.load(response)
        try:
            first = call('events?limit=1')
            second = call('events?' + urlencode(dict(limit=1, cursor=first['next_cursor'])))
            self.assertEqual(len(first['items'])+len(second['items']), 2)
            job = call('sample-jobs', dict(request_key='http', prompt='fixture'))
            self.assertEqual(job['state'], 'queued')
            self.assertEqual(len(call('sample-jobs')), 1)
            plan = call('retention-preview', dict(before='2026-10-30T00:00:00Z'))
            self.assertEqual(call('retention-execute', dict(plan_id=plan['id'], confirm='delete-previewed-events'))['state'], 'deleted')
            for endpoint in ('events', 'sample-jobs', 'retention-preview', 'retention-execute'):
                with self.assertRaises(HTTPError) as ctx:
                    call(endpoint, {} if endpoint.startswith('retention') else None, token='invalid')
                self.assertEqual(ctx.exception.code, 401)
        finally:
            app.shutdown()
            thread.join()
            app.server_close()
