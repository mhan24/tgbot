import sqlite3
import unittest
from bot import Bot
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI();self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,sqlite3.connect(':memory:'))
        self.bot.blacklist_member(2,reason='test')
        self.bot.appeals.owner=lambda:1
    def msg(self,text):
        return {'chat':{'id':2,'type':'private'},'from':{'id':2},'text':text}
    def submit(self):self.bot.appeals.command(self.msg('/appeal 希望重新加入'))
    def click(self,uid=1,action='approve'):
        self.bot.appeals.callback({'id':'q','from':{'id':uid},'data':f'appeal:{action}:1'})
    def test_approve_and_duplicate(self):
        self.submit();self.submit()
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM appeals').fetchone()[0],1)
        self.click(3);self.assertTrue(self.bot.blacklisted(2))
        self.click();self.assertFalse(self.bot.blacklisted(2))
        self.click()
        self.assertEqual(sum(m=='unbanChatMember' for m,d in self.api.calls),1)
    def test_reject_retains_ban(self):
        self.submit();self.click(action='reject');self.assertTrue(self.bot.blacklisted(2))
        self.submit();self.assertEqual(self.bot.db.execute('SELECT count(*) FROM appeals').fetchone()[0],1)
    def test_failure_keeps_pending_and_blacklist(self):
        self.submit();self.api.failure='unbanChatMember';self.click()
        self.assertTrue(self.bot.blacklisted(2))
        self.assertEqual(self.bot.db.execute('SELECT state FROM appeals').fetchone()[0],'pending')
        self.api.failure=None;self.click();self.assertFalse(self.bot.blacklisted(2))
    def test_wizard_and_delivery_retry(self):
        self.bot.appeals.command(self.msg('/appeal'))
        self.bot.appeals.owner=lambda: (_ for _ in ()).throw(RuntimeError())
        self.bot.appeals.command(self.msg('请求解封'))
        self.assertEqual(self.bot.db.execute('SELECT notified FROM appeals').fetchone()[0],0)
        self.bot.appeals.owner=lambda:1
        with self.bot.db:self.bot.db.execute('UPDATE appeals SET retry_at=0')
        self.bot.appeals.tick()
        self.assertEqual(self.bot.db.execute('SELECT notified FROM appeals').fetchone()[0],1)
    def test_stale_ban_not_unbanned(self):
        self.submit()
        with self.bot.db:self.bot.db.execute('UPDATE blacklist SET at=at+1')
        self.click();self.assertTrue(self.bot.blacklisted(2))
        self.assertEqual(self.bot.db.execute('SELECT state FROM appeals').fetchone()[0],'expired')
    def test_nonblacklisted_cannot_submit(self):
        with self.bot.db:self.bot.db.execute('DELETE FROM blacklist')
        self.submit();self.assertEqual(self.bot.db.execute('SELECT count(*) FROM appeals').fetchone()[0],0)

    def test_notification_mentions_applicant_and_escapes_name(self):
        msg = self.msg('/appeal 希望重新加入')
        msg['from'].update(username='Alice', first_name='<Alice>', last_name='& Co')
        self.bot.appeals.command(msg)
        row = self.bot.db.execute('SELECT * FROM appeals').fetchone()
        self.bot.appeals.notify(row)
        text = [d['text'] for m,d in self.api.calls if m == 'sendMessage' and d['chat_id'] == 1][-1]
        self.assertIn('<a href="tg://user?id=2">&lt;Alice&gt; &amp; Co</a>', text)
        self.assertIn('用户名：@Alice', text)
        self.assertIn('用户 ID：<code>2</code>', text)

    def test_no_username_still_has_clickable_mention(self):
        self.submit()
        row = self.bot.db.execute('SELECT * FROM appeals').fetchone()
        self.assertIn('tg://user?id=2', self.bot.appeals.applicant(row))
        self.assertIn('用户名：未设置', self.bot.appeals.applicant(row))
