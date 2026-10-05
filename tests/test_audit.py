import sqlite3
import unittest
from bot import Bot
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI();self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,sqlite3.connect(':memory:'))
        with self.bot.db:
            for i in range(105):
                self.bot.db.execute('INSERT INTO audit(at,actor,target,action,detail) VALUES(?,?,?,?,?)',(1,1,2 if i%2 else 3,f'event{i:03}', '<>&😀'*25))
    def run_command(self,text):
        self.bot.command({'chat':{'id':-1},'from':{'id':1},'text':text,'reply_to_message':{'from':{'id':2}}},1)
        return [d['text'] for m,d in self.api.calls if m=='sendMessage']
    def test_all_latest_100_split_even_when_replying(self):
        texts=self.run_command('/audit');combined='\n'.join(texts)
        self.assertEqual(combined.count('执行了'),100)
        self.assertIn('event104',combined);self.assertNotIn('event004',combined)
        self.assertLess(combined.index('event104'),combined.index('event103'))
        self.assertTrue(all(len(t.encode('utf-16-le'))//2<4096 for t in texts))
        self.assertIn('&lt;',combined)
    def test_target_retains_twenty(self):
        texts=self.run_command('/audit 2');combined='\n'.join(texts)
        self.assertEqual(combined.count('执行了'),20)
        self.assertNotIn('对 ID:3',combined)
    def test_invalid_target_does_not_fall_back_to_all(self):
        texts=self.run_command('/audit invalid');self.assertNotIn('执行了',''.join(texts))
