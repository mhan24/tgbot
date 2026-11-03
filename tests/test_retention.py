# coding: utf-8
import sqlite3
import unittest
from unittest.mock import patch
from bot import Bot
from retention import notice_delay
from test_bot import FakeAPI


class Tests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI()
        self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,sqlite3.connect(':memory:'))
        self.addCleanup(self.bot.db.close)
    def test_notice_lifetimes_and_kept_cards(self):
        with patch('bot.time.time',return_value=100):
            success=self.bot.send(-1,'✅ 签到成功，获得 2 积分！')
            failure=self.bot.send(-1,'今天已经签到过了。北京时间零点后可再次签到。')
            penalty=self.bot.send(-1,'⚠️ 该成员 警告 1/3：头像不可见')
            kept=self.bot.send(-1,'❌ 开奖公告测试',keep=True)
            ai=self.bot.send(-1,'普通回答',_delete_after=600)
        due=dict(self.bot.db.execute('SELECT message_id,due FROM deletions'))
        self.assertEqual(due[success['message_id']],130)
        self.assertEqual(due[failure['message_id']],220)
        self.assertEqual(due[penalty['message_id']],220)
        self.assertNotIn(kept['message_id'],due)
        self.assertEqual(due[ai['message_id']],700)
        self.assertEqual(notice_delay('✅ 群管运行中\n警告累计 3 次拉黑并删除消息。',30),30)
    def test_private_only_completed_bot_steps_are_deleted(self):
        with patch('bot.time.time',return_value=100):
            first=self.bot.send(5,'请填写奖品')
            self.bot.track_private_step(5,first['message_id'],'events')
            self.bot.schedule_delete(5,999,seconds=30)  # Ordinary private messages are not auto-deleted.
            self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0],0)
            next_step=self.bot.send(5,'请填写数量')
            self.bot.track_private_step(5,next_step['message_id'],'events')
            preview=self.bot.send(5,'最终预览')
            self.bot.finish_private_steps(5,'events')
            result=self.bot.send(5,'已提交成功')
        queued={r['message_id'] for r in self.bot.db.execute('SELECT * FROM deletions')}
        self.assertEqual(queued,{first['message_id'],next_step['message_id']})
        self.assertNotIn(preview['message_id'],queued)
        self.assertNotIn(result['message_id'],queued)
        with patch('bot.time.time',return_value=131):
            self.bot.sweep_deletions()
        deleted={d['message_id'] for m,d in self.api.calls if m=='deleteMessage'}
        self.assertEqual(deleted,queued)
    def test_unrelated_private_flows_are_preserved(self):
        self.bot.track_private_step(5,10,'events')
        self.bot.track_private_step(5,11,'settings')
        self.bot.finish_private_steps(5,'events')
        self.assertEqual(self.bot.db.execute('SELECT message FROM private_steps').fetchone()[0],11)
        self.assertEqual(self.bot.db.execute('SELECT message_id FROM deletions').fetchone()[0],10)

    def test_one_day_announcement_deletion_survives_restart(self):
        self.bot.send(-1,'开奖公告',keep=True,_delete_after=86400)
        due=self.bot.db.execute('SELECT due FROM deletions WHERE message_id=1').fetchone()[0]
        Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,self.bot.db)
        self.assertEqual(self.bot.db.execute('SELECT due FROM deletions WHERE message_id=1').fetchone()[0],due)
