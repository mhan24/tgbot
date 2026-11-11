# coding: utf-8
"""汇率换算测试：参数校验、交叉换算、缓存与上游失败反馈。"""
import io
import json
import sqlite3
import time
import unittest
import urllib.error
from unittest.mock import patch

from exchange import Exchange, RateError, UNAVAILABLE, USAGE
from tests.support import http_status, network_down

RATES = {'USD': 1, 'CNY': 7.2, 'EUR': 0.9, 'JPY': 149.123456789,
         'BTC': 0.000012640079, 'USDT': 1.0001}


def payload(stamp=1700000000):
    return json.dumps({'timestamp': stamp, 'base': 'USD', 'rates': RATES}).encode()


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return io.BytesIO(self.data)

    def __exit__(self, *args):
        return False


def respond(data):
    return lambda request, timeout: FakeResponse(data)


class Tests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.exchange = Exchange(self.db)
        self.addCleanup(self.db.close)
        # 默认上游返回固定汇率表；个别测试再临时覆盖。
        patcher = patch('httpjson.urllib.request.urlopen', side_effect=respond(payload()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def convert(self, *args):
        return self.exchange.convert(list(args))

    def test_amount_and_cross_rate_conversion(self):
        text = self.convert('100', 'USD', 'CNY')
        self.assertIn('100 USD = 720 CNY', text)
        self.assertIn('1 USD = 7.2 CNY', text)
        cross = self.convert('2', 'EUR', 'CNY')
        self.assertIn('2 EUR = 16 CNY', cross)
        self.assertIn('1 EUR = 8 CNY', cross)
        self.assertIn('exchangerate.fun', text)

    def test_two_arguments_default_to_one_unit(self):
        self.assertIn('1 USD = 7.2 CNY', self.convert('USD', 'CNY').splitlines()[0])

    def test_long_cross_rates_are_trimmed_to_significant_digits(self):
        text = self.convert('USD', 'JPY')
        self.assertIn('1 USD = 149.12346 JPY', text)

    def test_output_carries_data_time(self):
        text = self.convert('USD', 'CNY')
        self.assertIn('数据时间', text)
        self.assertIn('北京时间', text)

    def test_success_result_is_cached_for_an_hour(self):
        self.convert('USD', 'CNY')
        self.convert('50', 'USD', 'CNY')
        self.assertEqual(self.db.execute('SELECT count(*) FROM rates').fetchone()[0], 1)

    def test_cache_expires_and_refetches(self):
        self.convert('USD', 'CNY')
        with self.db:
            self.db.execute('UPDATE rates SET fetched=?', (int(time.time()) - 7200,))
        with patch('httpjson.urllib.request.urlopen',
                   side_effect=respond(payload(stamp=1800000000))):
            self.convert('USD', 'CNY')
        self.assertEqual(self.db.execute('SELECT stamp FROM rates').fetchone()[0], 1800000000)

    def test_invalid_amounts_and_usage_are_rejected(self):
        for args in (('abc', 'USD', 'CNY'), ('-1', 'USD', 'CNY'), ('1e5', 'USD', 'CNY'),
                     ('1.123456789', 'USD', 'CNY'), ('9' * 16, 'USD', 'CNY'),
                     ('USD',), (), ('1', 'USD'), ('1', 'USD', 'CNY', 'JPY')):
            with self.subTest(args=args):
                with self.assertRaises(RateError):
                    self.convert(*args)

    def test_usage_error_names_both_forms(self):
        with self.assertRaises(RateError) as caught:
            self.convert()
        self.assertEqual(str(caught.exception), USAGE)

    def test_unknown_currency_is_reported(self):
        self.convert('USD', 'CNY')
        with self.assertRaises(RateError) as caught:
            self.convert('XXX', 'CNY')
        self.assertIn('XXX', str(caught.exception))

    def test_usdt_is_a_real_code_and_not_treated_as_usd(self):
        # USDT 在接口返回列表里就是独立币种，按真实汇率换算，不替换成 USD。
        text = self.convert('1', 'USD', 'USDT')
        self.assertIn('1.0001', text)

    def test_upstream_failure_is_explicit_and_stale_cache_is_not_used(self):
        self.convert('USD', 'CNY')
        with self.db:
            self.db.execute('UPDATE rates SET fetched=?', (int(time.time()) - 7200,))
        with patch('httpjson.urllib.request.urlopen',
                   side_effect=urllib.error.URLError('down')):
            with self.assertRaises(RateError) as caught:
                self.convert('USD', 'CNY')
        self.assertEqual(str(caught.exception), UNAVAILABLE)

    def test_broken_payload_is_reported_as_unavailable(self):
        for data in (b'not-json', b'[]', json.dumps({'error': 'currency is error', 'data': None}).encode(),
                     json.dumps({'timestamp': 1, 'base': 'USD', 'rates': {}}).encode()):
            with self.subTest(data=data):
                with patch('httpjson.urllib.request.urlopen', side_effect=respond(data)):
                    with self.assertRaises(RateError) as caught:
                        self.convert('USD', 'CNY')
                self.assertEqual(str(caught.exception), UNAVAILABLE)


    def test_thousands_separators_and_spaces_are_accepted(self):
        self.assertIn('1000 USD = 7200 CNY', self.convert('1,000', 'USD', 'CNY'))
        self.assertIn('1000 USD = 7200 CNY', self.convert('1_000', 'USD', 'CNY'))

    def test_unknown_currency_is_html_escaped(self):
        self.convert('USD', 'CNY')
        with self.assertRaises(RateError) as caught:
            self.convert('USD', '<b>x</b>')
        self.assertIn('&lt;B&gt;X&lt;/B&gt;', str(caught.exception))
        self.assertNotIn('<b>', str(caught.exception))

    def test_upstream_failure_cools_down_before_hitting_it_again(self):
        calls = []
        patch.stopall()
        with patch('httpjson.urllib.request.urlopen', side_effect=network_down(calls)):
            for _ in range(3):
                with self.assertRaises(RateError) as caught:
                    self.convert('USD', 'CNY')
                self.assertEqual(str(caught.exception), UNAVAILABLE)
        self.assertEqual(len(calls), 1)

    def test_cooldown_expires(self):
        self.convert('USD', 'CNY')
        with self.db:
            self.db.execute('UPDATE rates SET fetched=?', (int(time.time()) - 7200,))
        self.exchange._failed_until = time.time() + 1
        with self.assertRaises(RateError):
            self.convert('USD', 'CNY')
        self.exchange._failed_until = 0
        self.assertIn('1 USD = 7.2 CNY', self.convert('USD', 'CNY'))


if __name__ == '__main__':
    unittest.main()
