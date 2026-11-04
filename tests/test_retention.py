# coding: utf-8
import sqlite3
import unittest
from unittest.mock import patch
from bot import Bot
from retention import NOTICE_SECONDS
from test_bot import FakeAPI


class Tests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI()
        self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,sqlite3.connect(':memory:'))
        self.addCleanup(self.bot.db.close)
    def due(self):
        return {row['message_id']: row['due'] for row in self.bot.db.execute('SELECT * FROM deletions')}
    def test_group_defaults_and_explicit_overrides(self):
        with patch('bot.time.time',return_value=100):
            success=self.bot.send(-1,'✅ 签到成功，获得 2 积分！')
            failure=self.bot.send(-1,'今天已经签到过了。',_delete_after=self.bot.notice_seconds())
            penalty=self.bot.send(-1,'⚠️ 该成员 警告 1/3：头像不可见',_delete_after=self.bot.notice_seconds())
            kept=self.bot.send(-1,'开奖公告测试',keep=True)
            long_lived=self.bot.send(-1,'自定义时长',_delete_after=600)
        queued=self.due()
        self.assertEqual(queued[success['message_id']],130)
        self.assertEqual(queued[failure['message_id']],100+NOTICE_SECONDS)
        self.assertEqual(queued[penalty['message_id']],100+NOTICE_SECONDS)
        self.assertNotIn(kept['message_id'],queued)
        self.assertEqual(queued[long_lived['message_id']],700)
    def test_wording_never_changes_the_lifetime(self):
        # No keyword guessing: identical texts keep their own explicit category.
        with patch('bot.time.time',return_value=100):
            failed=self.bot.send(-1,'暂时无法处理，请稍后重试。')
            plain=self.bot.send(-1,'普通提示',_delete_after=self.bot.notice_seconds())
        queued=self.due()
        self.assertEqual(queued[failed['message_id']],130)   # group default, not 120
        self.assertEqual(queued[plain['message_id']],220)    # explicit failure notice
    def test_duplicate_checkin_notice_has_explicit_failure_lifetime(self):
        self.bot.username='testbot'
        self.bot.avatar_visible=lambda uid, **kwargs: True
        msg={'chat':{'id':-1,'type':'supergroup'},'from':{'id':2,'first_name':'A'},
             'message_id':70,'date':1000,'text':'/checkin'}
        with patch('bot.time.time',return_value=100):
            self.bot.points_command(msg)
            result_id=self.api.sent
            self.bot.points_command(dict(msg, message_id=71))
            duplicate_id=self.api.sent
        queued=self.due()
        self.assertEqual(queued[result_id],130)
        self.assertEqual(queued[duplicate_id],100+NOTICE_SECONDS)
        self.assertIn('今天已经签到过了',
                      [d['text'] for m,d in self.api.calls if m=='sendMessage'][-1])

    def test_notice_disabled_with_group_cleanup(self):
        self.bot.delete_after=0
        self.assertEqual(self.bot.notice_seconds(),0)
        with patch('bot.time.time',return_value=100):
            self.bot.send(-1,'失败提示',_delete_after=self.bot.notice_seconds())
        self.assertEqual(self.due(),{})
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
