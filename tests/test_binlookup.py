# coding: utf-8
"""卡 BIN 查询测试：输入校验、字段展示、密钥头、缓存与上游失败反馈。"""
import io
import json
import sqlite3
import unittest
import urllib.error
from unittest.mock import patch

from binlookup import BinLookup, BinError, USAGE, UNAVAILABLE, BAD_KEY

PAYLOAD = {'success': True, 'data': {'bin': '45717360', 'scheme': 'Visa', 'funding': 'Debit',
                                     'segment': 'Dankort', 'issuer': 'Jyske Bank',
                                     'country_code': 'DK', 'country_name': 'Denmark',
                                     'bank_url': 'www.jyskebank.dk', 'bank_phone': '89 89 89 89'}}


def ok(data=PAYLOAD):
    return lambda request, timeout: io.BytesIO(json.dumps(data).encode())


def respond(data):
    return lambda request, timeout: io.BytesIO(data)


class Tests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.lookup = BinLookup(self.db, api_key='ipn_test')
        self.addCleanup(self.db.close)
        # 默认上游返回固定数据；个别测试再临时覆盖。
        patcher = patch('binlookup.urllib.request.urlopen', side_effect=ok())
        patcher.start()
        self.addCleanup(patcher.stop)

    def query(self, *args):
        return self.lookup.query(list(args))

    def test_valid_bin_is_formatted_with_all_fields(self):
        text = self.query('45717360')
        for line in ('BIN 45717360', '卡组织：Visa', '类型：借记卡', '卡种：Dankort',
                     '发卡行：Jyske Bank', '国家/地区：Denmark（DK）',
                     '发卡行网址：www.jyskebank.dk', '发卡行电话：89 89 89 89',
                     '来源：CardBinCheck.com'):
            self.assertIn(line, text)

    def test_unknown_fields_show_unknown(self):
        sparse = {'success': True, 'data': {'bin': '424242', 'scheme': 'Visa',
                                            'funding': 'Credit', 'segment': 'Unknown',
                                            'issuer': 'Unknown', 'country_code': 'US',
                                            'country_name': 'United States'}}
        with patch('binlookup.urllib.request.urlopen', side_effect=ok(sparse)):
            text = self.query('424242')
        self.assertIn('卡种：未知', text)
        self.assertIn('发卡行：未知', text)
        self.assertNotIn('发卡行网址', text)
        self.assertNotIn('发卡行电话', text)

    def test_funding_values_map_to_chinese(self):
        cases = (('45717360', 'Credit', '信用卡'), ('45717361', 'Debit', '借记卡'),
                 ('45717362', 'Prepaid', '预付卡'), ('45717363', 'Charge', '签账卡'))
        for bin_number, funding, word in cases:
            with self.subTest(funding=funding):
                data = dict(PAYLOAD['data'], funding=funding)
                with patch('binlookup.urllib.request.urlopen',
                           side_effect=ok({'success': True, 'data': data})):
                    self.assertIn(f'类型：{word}', self.query(bin_number))

    def test_api_key_header_is_sent_only_when_configured(self):
        captured = []

        def record(request, timeout):
            captured.append({k.lower(): v for k, v in request.header_items()})
            return io.BytesIO(json.dumps(PAYLOAD).encode())

        with patch('binlookup.urllib.request.urlopen', side_effect=record):
            self.query('45717360')
            BinLookup(self.db).query(['424242'])
        self.assertEqual(captured[0].get('x-api-key'), 'ipn_test')
        self.assertIsNone(captured[1].get('x-api-key'))

    def test_success_is_cached_for_a_day(self):
        self.query('45717360')
        self.query('45717360')
        self.assertEqual(self.db.execute('SELECT count(*) FROM bin_cache').fetchone()[0], 1)

    def test_input_must_be_six_to_eight_digits(self):
        for args in ((), ('3702',), ('457173601',), ('abc',), ('45717a',),
                     ('45717360', '45717361')):
            with self.subTest(args=args):
                with self.assertRaises(BinError):
                    self.query(*args)

    def test_usage_error_is_actionable(self):
        with self.assertRaises(BinError) as caught:
            self.query()
        self.assertEqual(str(caught.exception), USAGE)

    def test_unknown_bin_is_reported_and_not_cached(self):
        def missing(request, timeout):
            raise urllib.error.HTTPError('url', 404, 'Not Found', None, None)
        with patch('binlookup.urllib.request.urlopen', side_effect=missing):
            with self.assertRaises(BinError) as caught:
                self.query('999999')
        self.assertIn('未收录', str(caught.exception))
        self.assertEqual(self.db.execute('SELECT count(*) FROM bin_cache').fetchone()[0], 0)

    def test_bad_key_limit_and_failures_are_explicit(self):
        def respond_with(code):
            def raise_it(request, timeout):
                raise urllib.error.HTTPError('url', code, 'err', None, None)
            return raise_it
        for code, message in ((401, BAD_KEY), (403, BAD_KEY), (429, '限流'), (500, UNAVAILABLE)):
            with self.subTest(code=code):
                with patch('binlookup.urllib.request.urlopen', side_effect=respond_with(code)):
                    with self.assertRaises(BinError) as caught:
                        self.query('45717360')
                self.assertIn(message, str(caught.exception))
        for side_effect in (urllib.error.URLError('down'), ValueError('bad json')):
            with self.subTest(side_effect=side_effect):
                with patch('binlookup.urllib.request.urlopen', side_effect=side_effect):
                    with self.assertRaises(BinError) as caught:
                        self.query('45717360')
                self.assertEqual(str(caught.exception), UNAVAILABLE)

    def test_malformed_success_payload_is_unavailable(self):
        for data in (b'not-json', b'[]', json.dumps({'success': False, 'error': True,
                                                     'reason': 'backend', 'message': 'x'}).encode(),
                     json.dumps({'success': True}).encode()):
            with self.subTest(data=data):
                with patch('binlookup.urllib.request.urlopen', side_effect=respond(data)):
                    with self.assertRaises(BinError) as caught:
                        self.query('45717360')
                self.assertEqual(str(caught.exception), UNAVAILABLE)


if __name__ == '__main__':
    unittest.main()
