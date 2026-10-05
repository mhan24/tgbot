import sqlite3
import unittest
from bot import Bot
from test_bot import FakeAPI

class RankingTests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI()
        self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,sqlite3.connect(':memory:'))
        with self.bot.db:
            self.bot.db.executemany('INSERT INTO point_users VALUES(?,?,?)',
                [(i,'@User <>& '*16,102-i) for i in range(1,102)])
    def click(self,page,uid=2,chat=2):
        self.bot.handle({'callback_query':{'id':'q','from':{'id':uid},'data':f'rank:2:{page}',
            'message':{'message_id':8,'chat':{'id':chat,'type':'private'}}}})
    def test_pages_and_boundary_clamping(self):
        for page,count,buttons in [(0,50,1),(1,50,2),(2,1,1),(99,1,1),(-1,50,1)]:
            text,markup=self.bot.ranking_page(2,page)
            self.assertEqual(text.count(' 分'),count)
            self.assertEqual(len(markup['inline_keyboard'][0]),buttons)
            self.assertNotIn('@',text)
            self.assertNotIn('tg://',text)
            self.assertLess(len(text),4096)
        self.assertIn('101.',self.bot.ranking_page(2,2)[0])
    def test_tie_across_page(self):
        with self.bot.db:self.bot.db.execute('UPDATE point_users SET total=52 WHERE uid=51')
        self.assertIn('\n50.',self.bot.ranking_page(2,1)[0])
    def test_callback_edits_existing_message(self):
        self.click(1)
        edits=[d for m,d in self.api.calls if m=='editMessageText']
        self.assertEqual(len(edits),1)
        self.assertEqual(edits[0]['message_id'],8)
        self.assertIn('第 2/3 页',edits[0]['text'])
        self.assertFalse(any(m=='sendMessage' for m,d in self.api.calls))
        self.assertEqual(self.api.calls[-1][0],'answerCallbackQuery')
    def test_callback_access_control(self):
        self.click(1,uid=3)
        self.click(1,chat=-1)
        self.api.members[2]={'status':'left'}
        self.click(1)
        self.assertFalse(any(m=='editMessageText' for m,d in self.api.calls))
    def test_empty_and_exact_page(self):
        with self.bot.db:self.bot.db.execute('DELETE FROM point_users WHERE uid>50')
        text,markup=self.bot.ranking_page(2,0)
        self.assertIn('第 1/1 页',text)
        self.assertEqual(markup['inline_keyboard'],[])
        with self.bot.db:self.bot.db.execute('DELETE FROM point_users')
        self.assertIn('暂无积分记录',self.bot.ranking_page(2,0)[0])
