import unittest
from geo.contracts import url
from geo.adapters import classify


class RegressionTest(unittest.TestCase):
    def test_ipv6_url_remains_valid(self):
        self.assertEqual(url('https://[2001:db8::1]:8443/a?q=x'), 'https://[2001:db8::1]:8443/a')

    def test_ua_substring_is_not_a_bot_token(self):
        self.assertIsNone(classify('NotGPTBotFake/1.0')['declared_bot'])

    def test_url_rejects_control_characters(self):
        with self.assertRaises(ValueError):
            url('https://exam\nple.com/')
