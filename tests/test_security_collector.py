import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from geo.db import Database, SCHEMA
from geo.service import Service
from geo.api import server
from geo.security import Security
from geo.ownership import public_addresses, verify_https
from geo.collector import collect, http_sender


class SecurityCollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Service(Database(self.root / 'db.sqlite3'))
        self.site = self.service.create_site(dict(name='site', url='https://example.com', brands=['星舟']))['id']
        self.now = 1000
        self.security = Security(self.service, verifier=lambda origin, proof: True, clock=lambda: self.now)
        self.binding = dict(endpoint='local-test', site=self.site, source='nginx')
        self.state = self.root / 'state.json'
        self.log = self.root / 'access.jsonl'
        self.row = dict(time_iso8601='2026-10-29T10:00:00+08:00', url='https://example.com/a', status=200, http_user_agent='GPTBot')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows, path=None, mode='w'):
        with (path or self.log).open(mode, encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row) + '\n')

    def send(self, body):
        return self.service.ingest(self.site, 'logs', body)

    def verified_key(self):
        self.security.challenge(self.site)
        self.security.verify(self.site)
        return self.security.issue(self.site)

    def test_verification_expiry_and_wrong_proof(self):
        with self.assertRaises(PermissionError):
            self.security.issue(self.site)
        self.security.challenge(self.site)
        self.security.verifier = lambda *args: False
        with self.assertRaises(ValueError):
            self.security.verify(self.site)
        self.security.verifier = lambda *args: True
        self.now += 1801
        with self.assertRaises(ValueError):
            self.security.verify(self.site)

    def test_scoped_key_revocation_rotation_and_digest_only(self):
        first = self.verified_key()
        second = self.security.issue(self.site)
        self.assertTrue(self.security.permits(first['token'], self.site))
        self.assertFalse(self.security.permits(first['token'], 'other'))
        self.security.revoke(self.site, first['id'])
        self.assertFalse(self.security.permits(first['token'], self.site))
        self.assertTrue(self.security.permits(second['token'], self.site))
        self.assertNotIn(second['token'].encode(), (self.root / 'db.sqlite3').read_bytes())
        self.now += 31 * 86400
        self.assertFalse(self.security.permits(second['token'], self.site))

    def test_private_and_mixed_dns_rejected(self):
        for addresses in [('127.0.0.1',), ('::1',), ('224.0.0.1',), ('8.8.8.8', '10.0.0.1')]:
            rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in addresses]
            with patch('geo.ownership.socket.getaddrinfo', return_value=rows):
                with self.assertRaises(ValueError):
                    public_addresses('example.com')
        with self.assertRaises(ValueError):
            verify_https('http://example.com', 'proof')

    def test_https_proof_is_pinned_bounded_and_no_redirect(self):
        with patch('geo.ownership.public_addresses', return_value=['8.8.8.8']), patch('geo.ownership.PinnedHTTPS') as cls:
            response = cls.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'proof\n'
            self.assertTrue(verify_https('https://example.com', 'proof'))
            cls.assert_called_with('example.com', '8.8.8.8')
            response.read.assert_called_with(513)
            response.status = 302
            self.assertFalse(verify_https('https://example.com', 'proof'))
            response.status = 200
            response.read.return_value = b'wrong'
            self.assertFalse(verify_https('https://example.com', 'proof'))

    def test_v1_database_upgrade_preserves_sites(self):
        path = self.root / 'v1.sqlite3'
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)
            conn.execute('INSERT INTO sites VALUES(?,?,?,?)', ('old', 'old', 'https://old.example', '["old"]'))
        conn.close()
        database = Database(path)
        self.assertEqual(Service(database).site('old')['name'], 'old')
        with database.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 5)

    def test_batch_limit_and_repeated_runs_drain_backlog(self):
        self.write([self.row] * 101)
        first = collect([self.log], self.state, self.binding, self.send)
        second = collect([self.log], self.state, self.binding, self.send)
        self.assertEqual((first['acknowledged'], second['acknowledged']), (100, 1))
        self.assertEqual(self.service.report(self.site)['log_requests'], 101)

    def test_failed_send_does_not_advance_and_state_lock_excludes_second_writer(self):
        from geo.collector import state_lock
        self.write([self.row])
        with self.assertRaises(OSError):
            collect([self.log], self.state, self.binding, lambda body: (_ for _ in ()).throw(OSError()))
        state = json.loads(self.state.read_text())
        self.assertEqual(next(iter(state['files'].values()))['offset'], 0)
        with state_lock(self.state):
            with self.assertRaises(OSError):
                collect([self.log], self.state, self.binding, self.send)

    def test_lost_ack_replays_same_event(self):
        self.write([self.row])
        def lost_ack(body):
            self.send(body)
            raise OSError('simulated lost acknowledgement')
        with self.assertRaises(OSError):
            collect([self.log], self.state, self.binding, lost_ack)
        self.assertIsNotNone(json.loads(self.state.read_text())['pending'])
        collect([self.log], self.state, self.binding, self.send)
        self.assertEqual(self.service.report(self.site)['log_requests'], 1)
        self.assertIsNone(json.loads(self.state.read_text())['pending'])

    def test_append_restart_and_rename_rotation(self):
        self.write([self.row])
        collect([self.log], self.state, self.binding, self.send)
        self.write([self.row], mode='a')
        rotated = self.root / 'access.1'
        self.log.rename(rotated)
        self.write([self.row])
        collect([self.log, rotated], self.state, self.binding, self.send)
        collect([self.log, rotated], self.state, self.binding, self.send)
        self.assertEqual(self.service.report(self.site)['log_requests'], 3)

    def test_partial_line_and_malformed_line_not_skipped(self):
        self.write([self.row])
        with self.log.open('a', encoding='utf-8') as f:
            f.write(json.dumps(self.row))
        result = collect([self.log], self.state, self.binding, self.send)
        self.assertEqual(result['incomplete_lines'], 1)
        with self.log.open('a', encoding='utf-8') as f:
            f.write('\ninvalid\n')
        result = collect([self.log], self.state, self.binding, self.send)
        self.assertEqual((result['acknowledged'], result['invalid_lines']), (1, 1))
        result = collect([self.log], self.state, self.binding, self.send)
        self.assertEqual((result['acknowledged'], result['invalid_lines']), (0, 1))
        self.assertEqual(self.service.report(self.site)['log_requests'], 2)

    def test_truncation_and_destination_change_fail_closed(self):
        self.write([self.row])
        collect([self.log], self.state, self.binding, self.send)
        self.log.write_text('')
        with self.assertRaises(ValueError):
            collect([self.log], self.state, self.binding, self.send)
        with self.assertRaises(ValueError):
            collect([self.log], self.state, dict(self.binding, source='other'), self.send)

    def test_http_site_token_cannot_read_or_cross_site(self):
        key = self.verified_key()['token']
        app = server(self.service, 'admin-test-token-at-least-24-chars', port=0, security=self.security)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        endpoint = f'http://127.0.0.1:{app.server_port}'
        try:
            self.write([self.row])
            result = collect([self.log], self.state, self.binding, http_sender(endpoint, self.site, key))
            self.assertEqual(result['acknowledged'], 1)
            for suffix in ('/report', '/answers', '/ingest-keys'):
                req = Request(endpoint + '/api/v1/sites/' + self.site + suffix,
                    data=b'{}' if suffix != '/report' else None, headers={'Authorization': 'Bearer ' + key})
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(req)
                self.assertEqual(ctx.exception.code, 401)
            with self.assertRaises(HTTPError) as ctx:
                http_sender(endpoint, 'other', key)(dict(adapter='nginx-json-v1', source='test', records=[]))
            self.assertEqual(ctx.exception.code, 401)
        finally:
            app.shutdown()
            thread.join()
            app.server_close()

    def test_admin_http_onboarding_and_revocation(self):
        admin = 'admin-test-token-at-least-24-chars'
        app = server(self.service, admin, port=0, security=self.security)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        endpoint = f'http://127.0.0.1:{app.server_port}'
        def post(action, body=None):
            req = Request(endpoint + '/api/v1/sites/' + self.site + '/' + action,
                          data=json.dumps(body or {}).encode(), headers={'Authorization': 'Bearer ' + admin})
            with urlopen(req) as response:
                return json.load(response)
        try:
            with self.assertRaises(HTTPError) as ctx:
                post('ingest-keys')
            self.assertEqual(ctx.exception.code, 403)
            self.assertIn('/.well-known/', post('ownership-challenge')['url'])
            self.assertTrue(post('ownership-verify')['verified'])
            key = post('ingest-keys')
            self.assertTrue(post('revoke-key', {'key_id': key['id']})['revoked'])
            with self.assertRaises(HTTPError) as ctx:
                http_sender(endpoint, self.site, key['token'])({'records': []})
            self.assertEqual(ctx.exception.code, 401)
        finally:
            app.shutdown()
            thread.join()
            app.server_close()

    def test_concurrent_replays_are_duplicates_not_conflicts(self):
        from concurrent.futures import ThreadPoolExecutor
        body = dict(adapter='nginx-json-v1', source='concurrent', records=[dict(self.row, request_id='same')])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.send(body), range(4)))
        self.assertEqual(sum(r['inserted'] for r in results), 1)
        self.assertEqual(sum(r['duplicates'] for r in results), 3)

    def test_rule_change_does_not_break_retries(self):
        self.write([self.row])
        collect([self.log], self.state, self.binding, self.send)
        with self.service.db.connect() as conn:
            row = conn.execute('SELECT payload FROM events').fetchone()
            payload = json.loads(row['payload'])
            payload['rule_version'] = 'older'
            payload.pop('user_agent_hash')
            conn.execute('UPDATE events SET payload=?', (json.dumps(payload),))
        state = json.loads(self.state.read_text())
        entry = next(iter(state['files'].values()))
        entry.update(offset=0, prefix_length=0, prefix='')
        self.state.write_text(json.dumps(state))
        collect([self.log], self.state, self.binding, self.send)
        self.assertEqual(self.service.report(self.site)['log_requests'], 1)
