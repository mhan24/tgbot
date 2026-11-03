import io
import sqlite3
import unittest
import urllib.error
from unittest.mock import patch
from binlookup import BinLookup, LookupError, format_result
from bot import Bot
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self): self.client=BinLookup(sqlite3.connect(':memory:'))
    def test_validation_no_network(self):
        with patch('binlookup.urllib.request.urlopen') as request:
            for x in ['123','4571736012345678','../../x','１２３４５６','abc123']:
                with self.assertRaises(LookupError): self.client.lookup(x)
            request.assert_not_called()
    def test_header_and_persistent_cache(self):
        with patch('binlookup.urllib.request.urlopen', return_value=io.BytesIO(b'{"scheme":"visa"}')) as req:
            self.assertEqual(self.client.lookup('45717360')['scheme'],'visa')
            self.assertEqual(self.client.lookup('45717360')['scheme'],'visa')
            self.assertEqual(req.call_count,1)
            self.assertEqual(req.call_args[0][0].get_header('Accept-version'),'3')
    def test_upstream_failures_return_no_result(self):
        cases = ({'side_effect': urllib.error.HTTPError('url',404,'',{},None)},
                 {'side_effect': TimeoutError}, {'return_value': io.BytesIO(b'not json')})
        for response in cases:
            with self.subTest(response=response), patch('binlookup.urllib.request.urlopen', **response):
                with self.assertRaises(LookupError) as error:self.client.lookup('45717360')
                if isinstance(response.get('side_effect'), urllib.error.HTTPError):
                    self.assertIn('未找到', str(error.exception))
    def test_rate_limit_backoff(self):
        with patch('binlookup.urllib.request.urlopen',side_effect=urllib.error.HTTPError('url',429,'',{'Retry-After':'120'},None)) as req:
            for _ in range(2):
                with self.assertRaises(LookupError): self.client.lookup('45717360')
            self.assertEqual(req.call_count,1)
    def test_null_boolean_and_escape(self):
        text=format_result('45717360',{'bank':{'name':'<test>'},'country':None,'prepaid':False,'number':{'luhn':None}})
        self.assertIn('&lt;test&gt;',text);self.assertIn('预付卡：否',text);self.assertIn('Luhn 校验：未知',text)
    def test_regular_user_group_and_private_commands(self):
        bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api=FakeAPI(),db=sqlite3.connect(':memory:'))
        bot.username='testbot'
        with patch.object(bot.bin_lookup,'lookup',return_value={'scheme':'visa'}) as lookup, patch.object(bot,'send') as send:
            for chat in ({'id':-1,'type':'supergroup'}, {'id':2,'type':'private'}):
                bot.handle({'update_id':1,'message':{'chat':chat,'message_id':1,'from':{'id':2},'text':'/bin 45717360'}})
            self.assertEqual(lookup.call_count,2); self.assertEqual(send.call_count,2)
            self.assertFalse(bot.bin_command({'text':'/bin@otherbot 45717360'}))
