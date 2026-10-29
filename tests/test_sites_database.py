import json
import io
import runpy
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

from geo.api import server
from geo.db import Database
from geo.service import Service
from geo.limits import Limits, CapacityExceeded
from geo.paging import site_page, review_page
from geo.backup import copy_database


class SitesDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'synthetic.sqlite3'
        self.service = Service(Database(self.path))
        self.sid = self.new_site('first')['id']
        self.secret = 'synthetic-local-token-at-least-24-characters'

    def new_site(self, name):
        return self.service.create_site(dict(name=name, url=f'https://{name}.example', brands=['Go']))

    def http(self):
        app = server(self.service, self.secret, port=0)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            app.shutdown()
            app.server_close()
            thread.join(3)
        self.addCleanup(cleanup)
        def call(path, body=None, auth=True):
            req = Request(f'http://127.0.0.1:{app.server_port}'+path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization': 'Bearer ' + self.secret} if auth else {})
            try:
                with urlopen(req, timeout=5) as response:
                    return response.status, json.load(response), response.headers
            except HTTPError as exc:
                with exc:
                    return exc.code, json.load(exc), exc.headers
        call.port = app.server_port
        return call

    def test_existing_inventory_respects_read_budget(self):
        self.service.import_inventory(self.sid, dict(source='fixture', urls=[
            f'https://first.example/中文/{i}' for i in range(80)]))
        self.service.limits = Limits(query_bytes=1024)
        with self.assertRaises(CapacityExceeded):
            self.service.inventory(self.sid)

    def test_busy_database_has_retryable_http_contract(self):
        call = self.http()
        original = sqlite3.connect
        def short_connect(*args, **kwargs):
            kwargs['timeout'] = 0.01
            return original(*args, **kwargs)
        with self.service.db.connect() as locked:
            locked.execute('BEGIN IMMEDIATE')
            with patch('geo.db.sqlite3.connect', side_effect=short_connect):
                status, body, headers = call('/api/v1/sites', dict(name='new', url='https://new.example', brands=['Go']))
            self.assertEqual(status, 503)
            self.assertEqual(body['code'], 'database_busy')
            self.assertTrue(body['retryable'])
            self.assertEqual(headers['Retry-After'], '1')
        self.assertEqual(len(self.service.sites()), 1)
        self.assertEqual(call('/api/v1/sites', dict(name='new', url='https://new.example', brands=['Go']))[0], 201)

    def test_sites_paging_excludes_new_sites_and_deleted_max_sequence(self):
        self.new_site('second')
        last = self.new_site('third')
        first = site_page(self.service, self.secret, limit=1)
        with self.service.db.connect() as conn:
            conn.execute('DELETE FROM sites WHERE id=?', (last['id'],))
        self.new_site('later')
        remaining = site_page(self.service, self.secret, limit=2, cursor=first['next_cursor'])
        self.assertEqual([s['name'] for s in first['items']+remaining['items']], ['first', 'second'])
        self.assertIsNone(remaining['next_cursor'])
        self.assertEqual([s['name'] for s in site_page(self.service, self.secret)['items']], ['first', 'second', 'later'])

    def test_sites_cursor_rejects_tamper_expiry_rotation_and_other_resource(self):
        self.new_site('second')
        cursor = site_page(self.service, self.secret, limit=1)['next_cursor']
        for bad in ('invalid', cursor+'x'):
            with self.assertRaises(ValueError):
                site_page(self.service, self.secret, cursor=bad)
        with self.assertRaises(ValueError):
            site_page(self.service, 'rotated', cursor=cursor)
        with patch('geo.cursors.time.time', return_value=99999999999):
            with self.assertRaises(ValueError):
                site_page(self.service, self.secret, cursor=cursor)
        with self.assertRaises(ValueError):
            review_page(self.service, self.sid, self.secret, cursor=cursor)
        with self.assertRaises(ValueError):
            site_page(self.service, self.secret, limit=501)

    def test_sites_http_contract_and_query_validation(self):
        self.new_site('second')
        call = self.http()
        status, first, _ = call('/api/v1/sites?limit=1')
        self.assertEqual(status, 200)
        _, second, _ = call('/api/v1/sites?' + urlencode({'cursor': first['next_cursor']}))
        self.assertEqual([s['name'] for s in first['items']+second['items']], ['first', 'second'])
        for query in ('limit=0', 'limit=1&limit=2', 'unknown=x', 'cursor='):
            self.assertEqual(call('/api/v1/sites?'+query)[0], 400)
        self.assertEqual(call('/api/v1/sites', auth=False)[0], 401)

    def test_sites_page_has_byte_budget(self):
        self.service.create_site(dict(name='large', url='https://large.example', brands=['中文' * 45 + str(i) for i in range(20)]))
        self.service.limits = Limits(query_bytes=1024)
        with self.assertRaises(CapacityExceeded):
            site_page(self.service, self.secret)

    def test_v5_migration_preserves_order_and_backup_restore(self):
        self.new_site('second')
        with self.service.db.connect() as conn:
            conn.execute('DROP TABLE site_positions')
            conn.execute('PRAGMA user_version=5')
        backup = Path(self.tmp.name) / 'v5-backup.sqlite3'
        copy_database(self.path, backup)
        upgraded = Service(Database(backup))
        self.assertEqual([s['name'] for s in site_page(upgraded, self.secret)['items']], ['first', 'second'])
        with upgraded.db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 6)
        restored = Path(self.tmp.name) / 'v6-restored.sqlite3'
        copy_database(backup, restored)
        self.assertEqual(site_page(Service(Database(restored)), self.secret)['items'], site_page(upgraded, self.secret)['items'])

    def test_inventory_utf8_exact_boundary_and_precommit_budget(self):
        urls = [f'https://first.example/中文/{i}' for i in range(80)]
        size = len(json.dumps(sorted(urls), ensure_ascii=False).encode())
        self.service.limits = Limits(query_bytes=size)
        item = self.service.import_inventory(self.sid, dict(source='fixture', urls=urls))
        self.assertEqual(self.service.inventory(self.sid)['urls'], sorted(urls))
        self.service.limits = Limits(query_bytes=size-1)
        with self.assertRaises(CapacityExceeded):
            self.service.inventory(self.sid, item['id'])
        with self.assertRaises(CapacityExceeded):
            self.service.import_inventory(self.sid, dict(source='fixture', urls=urls))
        self.service.limits = Limits(response_bytes=1024)
        with self.assertRaises(CapacityExceeded):
            self.service.import_inventory(self.sid, dict(source='fixture', urls=urls))
        with self.service.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM inventories').fetchone()[0], 1)

    def test_inventory_scope_and_http_capacity(self):
        item = self.service.import_inventory(self.sid, dict(source='fixture', urls=[f'https://first.example/{i}' for i in range(100)]))
        other = self.new_site('other')['id']
        with self.assertRaises(LookupError):
            self.service.inventory(other, item['id'])
        self.service.limits = Limits(query_bytes=1024)
        call = self.http()
        for path in ('inventory', 'report'):
            status, body, _ = call(f'/api/v1/sites/{self.sid}/{path}')
            self.assertEqual(status, 422)
            self.assertEqual(body['code'], 'capacity_exceeded')

    def test_query_timeout_rolls_back_all_writes(self):
        with self.assertRaises(sqlite3.OperationalError) as error:
            with self.service.db.connect() as conn:
                conn.execute('INSERT INTO sites VALUES(?,?,?,?)', ('temporary', 'temporary', 'https://temporary.example', '[]'))
                with patch('geo.db.time.monotonic', return_value=float('inf')):
                    conn.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n').fetchone()
        self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
        self.assertEqual(len(self.service.sites()), 1)
        self.new_site('after-timeout')
        self.assertEqual(len(self.service.sites()), 2)

    def test_real_query_deadline_has_nonautomatic_retry_contract(self):
        self.service.db.query_timeout_ms = 1
        def costly_report(*args):
            with self.service.db.connect() as conn:
                return conn.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n').fetchone()[0]
        call = self.http()
        with patch.object(self.service, 'report', side_effect=costly_report):
            status, body, headers = call(f'/api/v1/sites/{self.sid}/report')
        self.assertEqual(status, 503)
        self.assertEqual(body['code'], 'query_timeout')
        self.assertFalse(body['retryable'])
        self.assertIsNone(headers.get('Retry-After'))

    def test_database_timeout_config_validation(self):
        for kwargs in ({'busy_timeout_ms': 0}, {'query_timeout_ms': True}, {'query_timeout_ms': 60001}):
            with self.assertRaises(ValueError):
                Database(self.path, **kwargs)
        with patch.dict('os.environ', {'GEO_DB_BUSY_TIMEOUT_MS': '100', 'GEO_DB_QUERY_TIMEOUT_MS': '900'}):
            configured = Database.from_env(self.path)
            self.assertEqual((configured.busy_timeout_ms, configured.query_timeout_ms), (100, 900))

    def test_demo_reuses_site_beyond_first_page(self):
        for i in range(99):
            self.new_site(f'fixture-{i}')
        demo = self.service.create_site(dict(name='demo', url='https://demo.example', brands=['星舟', '竞品甲']))
        call = self.http()
        reports = []
        with patch.dict('os.environ', {'GEO_PORT': str(call.port), 'GEO_API_TOKEN': self.secret}):
            for _ in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts' / 'demo.py'), run_name='__main__')
                reports.append(json.loads(output.getvalue()))
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[0]['site']['id'], demo['id'])
        self.assertEqual(reports[0]['answer_samples'], 1)
        self.assertEqual(len(self.service.sites()), 101)
