import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from geo.db import Database
from geo.service import Service
from geo.api import server
from geo.reporting import events_csv, safe_cell


class BrandReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(Database(Path(self.tmp.name) / 'db.sqlite3'))
        self.site = self.service.create_site(dict(name='test', url='https://example.com', brands=['Go']))['id']
        self.token = 'synthetic-admin-token-at-least-24'

    def tearDown(self):
        self.tmp.cleanup()

    def answers(self, rows):
        defaults = dict(observed_at='2026-10-29T00:00:00Z', platform='Kimi', model='sample', surface='manual', prompt='question')
        return self.service.ingest(self.site, 'answers', dict(adapter='manual-v1', source='fixture', records=[{**defaults, **row} for row in rows]))

    def test_english_brand_does_not_match_inside_another_word(self):
        self.answers([dict(event_id='a', answer='Google is mentioned here')])
        self.assertEqual(self.service.report(self.site)['brand_mentions']['Go'], 0)

    def test_http_unknown_filter_rejected_instead_of_ignored(self):
        app = server(self.service, self.token, port=0)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        try:
            req = Request(f'http://127.0.0.1:{app.server_port}/api/v1/sites/{self.site}/report?platfrom=Kimi',
                          headers={'Authorization': 'Bearer ' + self.token})
            with self.assertRaises(HTTPError) as ctx:
                urlopen(req)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            app.shutdown()
            thread.join()
            app.server_close()

    def config(self):
        return self.service.brands.configure(self.site, {'brands': [
            {'name': '星舟', 'role': 'own', 'aliases': ['StarShip', '共同名']},
            {'name': '竞品', 'role': 'competitor', 'aliases': ['OtherBrand', '共同名']}]})

    def test_alias_dedup_and_ambiguous_shared_alias(self):
        config = self.config()
        self.answers([dict(event_id='1', answer='星舟 STARSHIP 星舟'), dict(event_id='2', answer='共同名')])
        report = self.service.report(self.site)
        self.assertEqual(report['brand_mentions'], {'星舟': 1, '竞品': 0})
        self.assertEqual(report['mentioned_samples'], 1)
        self.assertEqual(report['ambiguous_mention_samples'], 1)
        self.assertEqual(report['brand_config_id'], config['id'])

    def test_configuration_history_and_stable_identity(self):
        first = self.config()
        brand = first['brands'][0]
        self.answers([dict(event_id='1', answer='StarShip')])
        second = self.service.brands.configure(self.site, {'brands': [dict(brand, aliases=[])]})
        self.assertEqual(self.service.report(self.site)['mentioned_samples'], 0)
        self.assertEqual(self.service.report(self.site, brand_config_id=first['id'])['mentioned_samples'], 1)
        self.assertEqual(second['brands'][0]['id'], brand['id'])
        self.assertEqual(self.answers([dict(event_id='1', answer='StarShip')])['duplicates'], 1)

    def test_manual_review_is_audited_and_reversible(self):
        config = self.config()
        self.answers([dict(event_id='1', answer='共同名')])
        body = dict(config_id=config['id'], brand_id=config['brands'][0]['id'], source='fixture',
                    event_id='1', included=True, reason='人工复核上下文确认品牌')
        first = self.service.brands.review(self.site, body)
        self.assertEqual(self.service.report(self.site)['brand_mentions']['星舟'], 1)
        self.service.brands.review(self.site, dict(body, included=False, reason='复核更正：属于同名词'))
        self.assertEqual(self.service.report(self.site)['brand_mentions']['星舟'], 0)
        self.assertEqual(len(self.service.brands.reviews(self.site, config['id'])), 2)
        self.assertEqual(first['actor'], 'instance_admin')
        with self.service.db.connect() as conn:
            original = json.loads(conn.execute('SELECT payload FROM events').fetchone()[0])
        self.assertEqual(original['answer'], '共同名')
        self.assertEqual(original['mentions'], [])

    def test_review_validation_and_site_isolation(self):
        config = self.config()
        self.answers([dict(event_id='1', answer='星舟')])
        body = dict(config_id=config['id'], brand_id=config['brands'][0]['id'], source='fixture', event_id='1', included=True, reason='checked')
        other = self.service.create_site(dict(name='other', url='https://other.example', brands=['other']))['id']
        with self.assertRaises(LookupError):
            self.service.brands.review(other, body)
        for bad in (dict(body, included='true'), dict(body, reason=''), dict(body, brand_id='missing')):
            with self.assertRaises(ValueError):
                self.service.brands.review(self.site, bad)
        with self.assertRaises(LookupError):
            self.service.brands.review(self.site, dict(body, event_id='missing'))

    def test_filter_report_events_csv_agree(self):
        self.answers([dict(event_id='1', answer='Go', platform='Kimi', surface='manual'),
                      dict(event_id='2', answer='Go', platform='Kimi', surface='model_api'),
                      dict(event_id='3', answer='Go', platform='Other', surface='manual')])
        filters = dict(platform='Kimi', surface='manual', source='fixture')
        report = self.service.report(self.site, filters=filters)
        events = self.service.events(self.site, filters=filters)
        rows = list(csv.DictReader(io.StringIO(events_csv(events))))
        self.assertEqual(report['answer_samples'], len(events))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['event_id'], '1')
        self.assertIsNone(report['log_requests'])
        self.assertIsNone(report['coverage_rate'])
        self.assertEqual(report['evidence_counts_by_surface'], {'manual': 1})
        self.assertEqual(self.service.report(self.site, filters={'platform': 'Missing'})['answer_samples'], 0)
        for bad in ({'kind': 'logs', 'platform': 'Kimi'}, {'surface': 'typo'}, {'wrong': 'x'}):
            with self.assertRaises(ValueError):
                self.service.events(self.site, filters=bad)

    def test_csv_formula_and_unicode_roundtrip(self):
        self.answers([dict(event_id='1', prompt='=1+1', answer='星舟,\n"quoted"')])
        rows = list(csv.DictReader(io.StringIO(events_csv(self.service.events(self.site)))))
        self.assertEqual(rows[0]['prompt'], "'=1+1")
        self.assertEqual(rows[0]['answer'], '星舟,\n"quoted"')
        for value in ('=1', '+1', '-1', '@a', '\t=1', '  =1', '\n=1'):
            self.assertTrue(safe_cell(value).startswith("'"))

    def test_duplicate_brand_and_foreign_identity_rejected(self):
        for rows in ([{'name': 'Go'}, {'name': 'Ｇｏ'}], [{'id': 'foreign', 'name': 'Go'}], [{'name': 'Go', 'role': 'typo'}]):
            with self.assertRaises(ValueError):
                self.service.brands.configure(self.site, {'brands': rows})

    def test_negative_mentions_are_not_sentiment_predictions(self):
        self.config()
        self.answers([dict(event_id='1', answer='不推荐星舟，优先比较竞品。')])
        report = self.service.report(self.site)
        self.assertEqual(report['brand_mentions'], {'星舟': 1, '竞品': 1})
        self.assertNotIn('sentiment', report)

    def test_v3_upgrade_preserves_answer_and_legacy_brands(self):
        self.answers([dict(event_id='1', answer='Go')])
        with self.service.db.connect() as conn:
            conn.execute('DROP TABLE brand_configs')
            conn.execute('DROP TABLE mention_reviews')
            conn.execute('PRAGMA user_version=3')
        upgraded = Service(Database(Path(self.tmp.name) / 'db.sqlite3'))
        self.assertEqual(upgraded.brands.configuration(self.site)['id'], 'legacy')
        self.assertEqual(upgraded.report(self.site)['brand_mentions'], {'Go': 1})
        with upgraded.db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 8)

    def test_http_export_and_brand_review_routes(self):
        self.answers([dict(event_id='1', answer='Go', prompt='=1+1')])
        app = server(self.service, self.token, port=0)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        prefix = f'http://127.0.0.1:{app.server_port}/api/v1/sites/{self.site}'
        def call(path, body=None, token=None):
            req = Request(prefix + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization': 'Bearer ' + (token or self.token)})
            return urlopen(req, timeout=3)
        try:
            with call('/brands') as response:
                config = json.load(response)
            with call('/mention-reviews', dict(config_id=config['id'], brand_id=config['brands'][0]['id'],
                source='fixture', event_id='1', included=False, reason='test review')) as response:
                self.assertEqual(response.status, 201)
            with call('/export.csv?platform=Kimi&surface=manual') as response:
                self.assertIn('text/csv', response.headers['Content-Type'])
                rows = list(csv.DictReader(io.StringIO(response.read().decode('utf-8-sig'))))
                self.assertEqual(json.loads(rows[0]['mentions']), [])
                self.assertEqual(rows[0]['prompt'], "'=1+1")
            for path in ('/export.csv', '/brands', '/mention-reviews'):
                with self.assertRaises(HTTPError) as ctx:
                    call(path, token='invalid')
                self.assertEqual(ctx.exception.code, 401)
            for query in ('?platform=', '?platform=Kimi&platform=Other', '?surface=typo'):
                with self.assertRaises(HTTPError) as ctx:
                    call('/report' + query)
                self.assertEqual(ctx.exception.code, 400)
        finally:
            app.shutdown()
            thread.join()
            app.server_close()
