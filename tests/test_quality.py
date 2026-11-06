import json
from pathlib import Path
import tempfile
import unittest
import threading
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from geo.db import Database
from geo.service import Service
from geo.imports import Imports
from geo.quality import report
from geo.api import server
from geo.limits import Limits, CapacityExceeded
from geo.retention import Retention
from geo.samples import validate


class QualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(Database(Path(self.tmp.name) / 'db.sqlite3'))
        self.sid = self.service.create_site(dict(name='demo', url='https://demo.example', brands=['Go']))['id']

    def record(self, event_id='one', **updates):
        return dict(dict(event_id=event_id, observed_at='2026-10-30T00:00:00Z', platform='synthetic',
            model='unknown', surface='manual', locale='zh-CN', prompt='fixture', answer='Go', citations=[], evidence_url=None), **updates)

    def test_unknown_model_case_is_not_a_known_model(self):
        path = Path(self.tmp.name) / 'sample.jsonl'
        path.write_text(json.dumps(dict(source='fixture', record=self.record(model=' UNKNOWN '))), encoding='utf-8')
        plan = Imports(self.service).preview(self.sid, path)
        self.assertEqual(plan['quality']['unknown_model'], 1)
        with path.open('rb') as stream:
            self.assertIn('unknown_model', validate(stream)['rows'][0]['warnings'])

    def ingest(self, records, source='fixture'):
        return self.service.ingest(self.sid, 'answers', dict(adapter='manual-v1', source=source, records=records))

    def test_counts_rates_synthetic_and_unclassified(self):
        self.ingest([self.record(), self.record('two', platform='Kimi', model='model-x', surface='consumer_ui',
            citations=['https://demo.example/page'], evidence_url='https://evidence.example/item'),
            self.record('three', platform='SYNTHETIC', surface='model_api')])
        result = report(self.service, self.sid)
        self.assertEqual((result['samples'], result['synthetic_samples'], result['unclassified_samples']), (3, 2, 1))
        self.assertEqual(result['without_citations'], dict(count=2, evaluated_samples=3, unknown_samples=0, rate=2/3))
        self.assertEqual(result['without_evidence_url']['count'], 2)
        self.assertEqual(result['unknown_model']['count'], 2)
        self.assertEqual(report(self.service, self.sid, dataset='synthetic')['samples'], 2)
        self.assertEqual(report(self.service, self.sid, dataset='unclassified')['samples'], 1)

    def test_empty_and_legacy_unknown_denominators(self):
        self.assertIsNone(report(self.service, self.sid)['without_citations']['rate'])
        self.ingest([self.record(), self.record('legacy')])
        with self.service.db.connect() as conn:
            conn.execute("UPDATE events SET payload=json_remove(payload,'$.citations','$.evidence_url','$.model','$.surface') WHERE event_id='legacy'")
        result = report(self.service, self.sid)
        self.assertEqual(result['without_citations'], dict(count=1, evaluated_samples=1, unknown_samples=1, rate=1.0))
        self.assertEqual(result['without_evidence_url']['unknown_samples'], 1)
        self.assertEqual(result['by_surface']['unknown'], 1)
        self.assertEqual(result['unknown_model']['count'], 2)

    def test_time_filter_and_existing_report_agree(self):
        self.ingest([self.record('before', observed_at='2026-10-29T23:59:59Z'), self.record('start'),
                     self.record('end', observed_at='2026-10-31T00:00:00Z')])
        start, end = '2026-10-30T08:00:00+08:00', '2026-10-31T00:00:00Z'
        filters = dict(source=' fixture ', platform='synthetic', surface='manual', model='unknown')
        result = report(self.service, self.sid, start, end, filters)
        self.assertEqual(result['samples'], 1)
        self.assertEqual(result['samples'], self.service.report(self.sid, start, end, filters=filters)['answer_samples'])
        self.assertEqual(result['window']['start'], '2026-10-30T00:00:00+00:00')

    def test_quality_does_not_require_brand_projection_or_inventory(self):
        self.ingest([self.record(answer='PRIVATE_BODY', prompt='PRIVATE_PROMPT')])
        with (patch.object(self.service.brands, 'project', side_effect=AssertionError('no projection')),
              patch.object(self.service, 'inventory', side_effect=AssertionError('no inventory'))):
            result = report(self.service, self.sid)
        encoded = json.dumps(result)
        self.assertNotIn('PRIVATE', encoded)
        self.assertNotIn('event_id', result)

    def test_capacity_after_dataset_filter_and_invalid_queries(self):
        self.ingest([self.record(), self.record('two', platform='Other')])
        self.service.limits = Limits(query_events=1)
        with self.assertRaises(CapacityExceeded):
            report(self.service, self.sid)
        self.assertEqual(report(self.service, self.sid, dataset='synthetic')['samples'], 1)
        for kwargs in (dict(dataset='real'), dict(filters={'kind': 'logs'}), dict(filters={'platfrom': 'x'}),
                       dict(start='2026-10-31T00:00:00Z', end='2026-10-30T00:00:00Z')):
            with self.assertRaises(ValueError):
                report(self.service, self.sid, **kwargs)
        with self.assertRaises(LookupError):
            report(self.service, 'missing')

    def test_import_snapshot_replay_and_retention_difference(self):
        file = Path(__file__).resolve().parents[1] / 'examples' / 'manual-samples.jsonl'
        imports = Imports(self.service)
        plan = imports.preview(self.sid, file)
        imports.execute(self.sid, file, plan['id'], 'import-previewed-samples')
        current = report(self.service, self.sid)
        self.assertEqual(current['samples'], plan['quality']['samples'])
        self.assertEqual(current['without_citations']['count'], plan['quality']['without_citations'])
        self.assertEqual(current['without_evidence_url']['count'], plan['quality']['without_evidence_url'])
        imports.execute(self.sid, file, plan['id'], 'import-previewed-samples')
        self.assertEqual(report(self.service, self.sid)['samples'], 2)
        retention = Retention(self.service)
        cleanup = retention.preview(self.sid, '2026-10-31T00:00:00Z')
        retention.execute(self.sid, cleanup['id'], 'delete-previewed-events')
        self.assertEqual(report(self.service, self.sid)['samples'], 0)
        self.assertEqual(imports.result(self.sid, plan['id'])['quality']['samples'], 2)

    def test_http_permissions_parameters_and_capacity(self):
        self.ingest([self.record(), self.record('two')])
        token = 'synthetic-quality-test-token-at-least-24'
        app = server(self.service, token, port=0)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        def call(query='', auth=True):
            req = Request(f'http://127.0.0.1:{app.server_port}/api/v1/sites/{self.sid}/quality'+query,
                          headers={'Authorization': 'Bearer '+token} if auth else {})
            try:
                with urlopen(req, timeout=5) as response:
                    return response.status, json.load(response)
            except HTTPError as exc:
                with exc:
                    return exc.code, json.load(exc)
        try:
            self.assertEqual(call()[1]['samples'], 2)
            self.assertEqual(call(auth=False)[0], 401)
            for query in ('?dataset=real', '?dataset=', '?dataset=all&dataset=all', '?brand_config_id=legacy', '?kind=logs'):
                self.assertEqual(call(query)[0], 400)
            self.service.limits = Limits(query_events=1)
            self.assertEqual(call()[0], 422)
        finally:
            app.shutdown()
            app.server_close()
            thread.join(3)
