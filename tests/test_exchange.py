import io
import json
import sqlite3
import unittest
from unittest.mock import patch
from exchange import Exchange, RateError
from bot import Bot
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.exchange = Exchange(self.db)
        self.body = json.dumps({'base':'USD','timestamp':1791608403,'rates':{'USD':1,'CNY':7,'EUR':.8,'BTC':.00001}}).encode()
    def tearDown(self):
        self.db.close()
    def test_cross_conversion_and_cache(self):
        with patch('exchange.urllib.request.urlopen', return_value=io.BytesIO(self.body)) as call:
            self.assertIn('875 CNY', self.exchange.convert(['100','eur','cny']))
            self.assertIn('7 CNY', self.exchange.convert(['USD','CNY']))
            self.assertIn('0.00001 BTC', self.exchange.convert(['USD','BTC']))
            self.assertEqual(call.call_count,1)
    def test_invalid_inputs_no_network(self):
        with patch('exchange.urllib.request.urlopen') as call:
            for args in [[],['nan','USD','CNY'],['-1','USD','CNY'],['1','<x>','CNY'],['1e3','USD','CNY']]:
                with self.assertRaises(RateError): self.exchange.convert(args)
            call.assert_not_called()
    def test_unknown_currency(self):
        with patch('exchange.urllib.request.urlopen', return_value=io.BytesIO(self.body)):
            with self.assertRaises(RateError): self.exchange.convert(['ABC','USD'])
    def test_bad_response_not_cached(self):
        with patch('exchange.urllib.request.urlopen', return_value=io.BytesIO(b'{"base":"USD","rates":{"CNY":-1}}')):
            with self.assertRaises(RateError): self.exchange.convert(['USD','CNY'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM exchange_cache').fetchone()[0],0)
    def test_group_private_dispatch_and_cleanup(self):
        api=FakeAPI()
        bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.username='testbot'
        with patch.object(bot.exchange,'convert',return_value='100 USD = 700 CNY'):
            for chat,kind,mid in [(-1,'supergroup',10),(2,'private',11)]:
                bot.handle({'update_id':mid,'message':{'chat':{'id':chat,'type':kind},'from':{'id':2},'message_id':mid,'text':'/rate 100 USD CNY'}})
        self.assertEqual(len([d for m,d in api.calls if m=='sendMessage']),2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM deletions').fetchone()[0],1)
        self.assertFalse(bot.rate_command({'text':'/rate@other USD CNY'}))
