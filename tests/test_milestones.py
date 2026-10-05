import sqlite3
import unittest
from bot import Bot
from milestones import Milestones
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI();self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,sqlite3.connect(':memory:'))
    def points(self,uid,total=50):
        with self.bot.db:self.bot.db.execute('INSERT OR REPLACE INTO point_users VALUES(?,?,?)',(uid,'A<b>',total))
    def messages(self):return [d for m,d in self.api.calls if m=='sendMessage']
    def test_ordinals_survive_restart_and_reset(self):
        self.points(2);self.bot.upgrade_member(2)
        self.points(3);self.bot.upgrade_member(3)
        self.assertIn('第 1 位',self.messages()[0]['text'])
        self.assertIn('第 2 位',self.messages()[1]['text'])
        self.assertIn('A&lt;b&gt;',self.messages()[0]['text'])
        self.bot.points.reset(2);self.points(2)
        Milestones(self.bot).tick()
        self.assertEqual(len(self.messages()),2)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0],2)
    def test_retry_after_failure(self):
        self.points(2);self.api.failure='sendMessage';self.bot.upgrade_member(2)
        self.assertEqual(self.bot.db.execute('SELECT sent FROM point_milestones').fetchone()[0],0)
        self.api.failure=None
        with self.bot.db:self.bot.db.execute('UPDATE point_milestones SET retry_at=0')
        Milestones(self.bot).tick()
        self.assertEqual(self.bot.db.execute('SELECT sent FROM point_milestones').fetchone()[0],1)
        self.assertEqual(self.bot.db.execute('SELECT ordinal FROM point_milestones').fetchone()[0],1)
    def test_existing_custom_upgrade_is_backfilled(self):
        self.points(2)
        self.bot.record_upgrade(2,'custom',50)
        self.bot.milestones.tick()
        self.assertEqual(len(self.messages()),1)
        self.assertIn('第 1 位',self.messages()[0]['text'])
    def test_blacklisted_and_departed_not_announced(self):
        self.points(2);self.bot.blacklist_member(2)
        self.points(3);self.api.members[3]={'status':'left'}
        self.bot.milestones.tick();self.assertEqual(self.messages(),[])
