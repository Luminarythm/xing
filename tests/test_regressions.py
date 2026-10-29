import unittest
from geo.contracts import url
from geo.adapters import classify
from geo.adapters import JsonLogAdapter


class RegressionTest(unittest.TestCase):
    def test_ipv6_url_remains_valid(self):
        self.assertEqual(url('https://[2001:db8::1]:8443/a?q=x'), 'https://[2001:db8::1]:8443/a')

    def test_ua_substring_is_not_a_bot_token(self):
        self.assertIsNone(classify('NotGPTBotFake/1.0')['declared_bot'])

    def test_url_rejects_control_characters(self):
        with self.assertRaises(ValueError):
            url('https://exam\nple.com/')

    def test_port_zero_is_not_silently_changed_to_default(self):
        with self.assertRaises(ValueError):
            url('https://example.com:0/a')

    def test_empty_user_agent_keeps_valid_request(self):
        record = JsonLogAdapter().normalize(dict(event_id='no-ua', observed_at='2026-10-29T00:00:00Z',
            url='https://example.com/', status=200, user_agent=''))
        self.assertIsNone(classify(record['user_agent'])['declared_bot'])
