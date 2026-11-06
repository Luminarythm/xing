import io
import json
import unittest
from geo.adapters import ManualAnswerAdapter
from geo.samples import validate, main
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch


def sample():
    return dict(source='synthetic-fixture', record=dict(event_id='sample-1', observed_at='2026-10-30T00:00:00Z',
        platform='synthetic', model='unknown', surface='manual', locale='zh-CN', prompt='合成问题',
        answer='合成回答', citations=[], evidence_url=None))


class SamplesTest(unittest.TestCase):
    def check(self, rows, **kwargs):
        return validate(io.BytesIO(('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows)+'\n').encode()), **kwargs)

    def test_false_evidence_values_are_not_silently_discarded(self):
        for value in (False, 0, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ManualAnswerAdapter().normalize(dict(sample()['record'], evidence_url=value))

    def test_valid_record_and_no_citation_are_included(self):
        result = self.check([sample()])
        self.assertTrue(result['valid'])
        self.assertEqual(result['valid_rows'], 1)
        self.assertIn('missing_evidence_url', result['rows'][0]['warnings'])

    def test_duplicate_identity_uses_normalized_source_and_id(self):
        row = sample()
        other = dict(row, source=' synthetic-fixture ', record=dict(row['record'], event_id=' sample-1 '))
        result = self.check([row, other])
        self.assertEqual((result['valid_rows'], result['invalid_rows'], result['duplicate_rows']), (1, 1, 1))
        self.assertTrue(self.check([row, dict(row, source='another')])['valid'])

    def test_schema_types_urls_and_surface(self):
        for change in ({'surface': 'typo'}, {'citations': ['javascript:bad']}, {'observed_at': '2026-10-30'},
                       {'answer': ''}, {'evidence_url': False}):
            row = sample()
            row['record'].update(change)
            self.assertFalse(self.check([row])['valid'])
        raw = json.dumps(dict(sample(), record=dict(sample()['record'], prompt='\ud800'))).encode()
        self.assertFalse(validate(io.BytesIO(raw))['valid'])
        row = sample()
        del row['record']['model']
        self.assertIn('missing_required_fields', self.check([row])['rows'][0]['errors'])
        row = sample()
        row['record']['platfrom'] = 'typo'
        self.assertIn('unknown_fields', self.check([row])['rows'][0]['errors'])

    def test_invalid_json_duplicate_keys_bom_and_line_numbers(self):
        for raw in (b'{"source":"a","source":"b"}', b'{"x":NaN}', b'\xff', b'[]', b'\n'):
            self.assertFalse(validate(io.BytesIO(raw))['valid'])
        raw = b'\xef\xbb\xbf'+json.dumps(sample()).encode()+b'\ninvalid\n'
        result = validate(io.BytesIO(raw))
        self.assertEqual(result['checked_rows'], 2)
        self.assertEqual(result['rows'][1]['line'], 2)

    def test_limits_stop_without_claiming_complete_validation(self):
        raw = json.dumps(sample()).encode()
        self.assertTrue(validate(io.BytesIO(raw), max_bytes=len(raw), max_line=len(raw))['valid'])
        for options in ({'max_bytes': len(raw)-1}, {'max_line': len(raw)-1}, {'max_rows': 0}):
            result = validate(io.BytesIO(raw), **options)
            self.assertFalse(result['complete'])
            self.assertFalse(result['valid'])
        self.assertEqual(validate(io.BytesIO(b''))['fatal'], 'empty_input')

    def test_diagnostics_do_not_echo_sensitive_fields(self):
        row = sample()
        row['record'].update(answer='PRIVATE_ANSWER', citations=['https://PRIVATE_USER:PRIVATE_PASS@example.com'])
        result = self.check([row])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertFalse(result['valid'])

    def test_cli_example_and_read_failure(self):
        path = Path(__file__).resolve().parents[1] / 'examples' / 'manual-samples.jsonl'
        output = io.StringIO()
        with redirect_stdout(output), patch('socket.socket', side_effect=AssertionError('network forbidden')):
            self.assertEqual(main([str(path)]), 0)
        self.assertEqual(json.loads(output.getvalue())['checked_rows'], 2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(path)+'.missing']), 2)

    def test_query_stripping_is_reported(self):
        row = sample()
        row['record']['citations'] = ['https://demo.example/page?token=private']
        result = self.check([row])
        self.assertIn('url_query_or_fragment_removed_on_ingest', result['rows'][0]['warnings'])
