# coding: utf-8
"""卡 BIN 查询测试：输入校验、字段展示、24 小时缓存、404/429 与冷却。"""
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

import binlookup as binmod
from binlookup import BinLookup, BinError, PAYLOAD_VERSION, USAGE, UNAVAILABLE
from tests.support import http_status, json_response, network_down

UPSTREAM = {'scheme': 'visa', 'brand': 'PLATINUM DEBIT', 'type': 'debit', 'prepaid': False,
            'country': {'name': 'Taiwan, Province of China', 'emoji': '🇹🇼', 'alpha2': 'TW',
                        'currency': 'TWD'},
            'bank': {'name': 'CTBC', 'url': 'https://ctbc.example', 'phone': '0800-123-456'}}


class Tests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.lookup = BinLookup(self.db, timeout=1)
        self.addCleanup(self.db.close)
        patcher = patch('httpjson.urllib.request.urlopen', side_effect=json_response(UPSTREAM))
        patcher.start()
        self.addCleanup(patcher.stop)

    def query(self, *args):
        return self.lookup.query(list(args))

    def cache_count(self):
        return self.db.execute('SELECT count(*) FROM bin_cache').fetchone()[0]

    # --- 输入 -------------------------------------------------------------

    def test_usage_is_reported_for_wrong_length(self):
        for args in ((), ('45717',), ('457173600',), ('abc',)):
            with self.subTest(args=args):
                with self.assertRaises(BinError) as caught:
                    self.query(*args)
                self.assertEqual(str(caught.exception), USAGE)

    def test_grouped_and_punctuated_input_is_accepted(self):
        self.assertEqual(self.lookup.query(['4571', '7380']),
                         self.lookup.query(['4571-7380']))
        self.assertEqual(self.cache_count(), 1)

    # --- 展示 -------------------------------------------------------------

    def test_all_fields_are_formatted(self):
        text = self.query('45717360')
        self.assertIn('卡头检测', text)
        self.assertIn('45717360', text)
        self.assertIn('Visa', text)
        self.assertIn('借记', text)
        self.assertIn('Platinum', text)
        self.assertIn('台湾', text)
        self.assertIn('🇹🇼', text)
        self.assertIn('新台币', text)
        self.assertIn('CTBC', text)
        self.assertIn('预付卡：×', text)
        self.assertIn('商业卡：×', text)
        self.assertIn('发卡行网址：https://ctbc.example', text)
        self.assertIn('发卡行电话：0800-123-456', text)

    def test_missing_fields_fall_back_to_unknown(self):
        sparse = {'scheme': 'mastercard', 'brand': '', 'type': '', 'country': {}, 'bank': {}}
        with patch('httpjson.urllib.request.urlopen', side_effect=json_response(sparse)):
            text = self.query('545454')
        self.assertIn('Mastercard', text)
        self.assertIn('未知', text)
        self.assertNotIn('发卡行网址', text)

    def test_bank_name_is_html_escaped(self):
        risky = dict(UPSTREAM, bank={'name': '<b>Bank</b> & Co'})
        with patch('httpjson.urllib.request.urlopen', side_effect=json_response(risky)):
            text = self.query('457173')
        self.assertIn('&lt;b&gt;Bank&lt;/b&gt; &amp; Co', text)
        self.assertNotIn('<b>', text)

    def test_commercial_and_prepaid_flags(self):
        payload = dict(UPSTREAM, brand='BUSINESS', prepaid=True)
        with patch('httpjson.urllib.request.urlopen', side_effect=json_response(payload)):
            text = self.query('457173')
        self.assertIn('商业卡：✓', text)
        self.assertIn('预付卡：✓', text)

    # --- 缓存 -------------------------------------------------------------

    def test_successful_lookup_is_cached_for_a_day(self):
        self.query('45717360')
        with patch('httpjson.urllib.request.urlopen', side_effect=AssertionError('不该回源')):
            self.assertIn('CTBC', self.query('45717360'))

    def test_expired_cache_is_refetched(self):
        self.query('45717360')
        with self.db:
            self.db.execute('UPDATE bin_cache SET fetched=?', (int(time.time()) - 90000,))
        fresh = dict(UPSTREAM, bank={'name': 'Fresh Bank'})
        with patch('httpjson.urllib.request.urlopen', side_effect=json_response(fresh)):
            self.assertIn('Fresh Bank', self.query('45717360'))

    def test_stale_payload_version_is_rebuilt(self):
        with self.db:
            self.db.execute('INSERT INTO bin_cache(bin,fetched,payload) VALUES(?,?,?)',
                            ('45717360', int(time.time()),
                             json.dumps({'v': PAYLOAD_VERSION - 1, 'scheme': 'old'})))
        self.assertIn('CTBC', self.query('45717360'))
        stored = json.loads(self.db.execute(
            'SELECT payload FROM bin_cache WHERE bin=?', ('45717360',)).fetchone()['payload'])
        self.assertEqual(stored['v'], PAYLOAD_VERSION)

    def test_unreadable_cache_row_is_rebuilt(self):
        with self.db:
            self.db.execute('INSERT INTO bin_cache(bin,fetched,payload) VALUES(?,?,?)',
                            ('45717360', int(time.time()), 'not-json'))
        self.assertIn('CTBC', self.query('45717360'))

    def test_prune_drops_rows_that_were_not_asked_for_again(self):
        now = int(time.time())
        with self.db:
            self.db.executemany('INSERT INTO bin_cache(bin,fetched,payload) VALUES(?,?,?)',
                                [('411111', now - 30 * 86400, '{}'), ('422222', now, '{}')])
        self.lookup.prune()
        left = [row['bin'] for row in self.db.execute('SELECT bin FROM bin_cache')]
        self.assertEqual(left, ['422222'])

    # --- 上游失败 ---------------------------------------------------------

    def test_not_found_and_empty_payload_both_mean_not_listed(self):
        for handler in (http_status(404), json_response({'scheme': None})):
            with self.subTest(handler=handler), \
                    patch('httpjson.urllib.request.urlopen', side_effect=handler):
                with self.assertRaises(BinError) as caught:
                    self.query('457173')
                self.assertIn('未收录', str(caught.exception))
        # 未收录不触发全局冷却。
        self.assertEqual(self.lookup._failed_until, 0)

    def test_rate_limited_upstream_cools_down(self):
        calls = []
        with patch('httpjson.urllib.request.urlopen', side_effect=http_status(429, calls=calls)):
            with self.assertRaises(BinError) as caught:
                self.query('457173')
            self.assertIn('限流', str(caught.exception))
            with self.assertRaises(BinError) as caught:
                self.query('411111')
            self.assertEqual(str(caught.exception), UNAVAILABLE)
        self.assertEqual(len(calls), 1)

    def test_server_error_and_network_failure_cool_down(self):
        for name, handler in (('500', http_status(500)), ('network', network_down())):
            with self.subTest(case=name):
                self.lookup._failed_until = 0
                with patch('httpjson.urllib.request.urlopen', side_effect=handler):
                    with self.assertRaises(BinError) as caught:
                        self.query('457173')
                self.assertEqual(str(caught.exception), UNAVAILABLE)
                self.assertGreater(self.lookup._failed_until, time.time())

    def test_cooldown_does_not_block_cached_bins(self):
        self.query('45717360')
        self.lookup._failed_until = time.time() + 60
        self.assertIn('CTBC', self.query('45717360'))


if __name__ == '__main__':
    unittest.main()
