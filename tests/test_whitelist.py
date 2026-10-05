import sqlite3
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from bot import Bot
from test_bot import FakeAPI
from points import CST

class Tests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2', 'AI_BASE_URL': 'https://x.test/v1',
                        'AI_API_KEY': 'k', 'AI_MODEL': 'm'}, self.api, sqlite3.connect(':memory:'))
        self.bot.bot_id, self.bot.username = 99, 'testbot'
        self.bot.can_manage_tags = True
        self.group = self.bot.group

    def msg(self, mid, text, uid=2):
        return {'chat': {'id': self.group, 'type': 'supergroup'}, 'message_id': mid,
                'from': {'id': uid, 'first_name': 'Vip'}, 'text': text, 'date': int(time.time())}

    def white(self, uid=2, mid=1):
        self.bot._moderation_action({'chat': {'id': self.group}, 'text': f'/white {uid} 测试',
                          'from': {'id': 1}, 'message_id': mid}, mid)

    def test_whitelisted_member_cannot_be_submitted_to_jev(self):
        self.bot.cfg['JEV_API_KEY'] = 'test'
        self.white()
        self.bot.jev.submit_target(self.msg(2, '日赚1000，私聊我'))
        self.assertIsNone(self.bot.db.execute('SELECT 1 FROM jev_cases').fetchone())

    def test_whitelisted_member_skips_daily_group_limit(self):
        self.white()
        for i in range(1, 8):
            self.bot.handle({'update_id': i, 'message': self.msg(i, '/rank')})
        self.assertIsNone(self.bot.db.execute("SELECT 1 FROM sqlite_master WHERE name='command_usage'").fetchone())

    def test_whitelisted_member_is_exempt_from_three_day_blacklist(self):
        self.white()
        self.bot.handle({'update_id': 1, 'message': self.msg(1, '大家好啊')})
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM activity_days').fetchone()[0], 0)

    def test_blacklist_wins_over_whitelist(self):
        self.white()
        with self.bot.db:
            self.bot.db.execute("INSERT INTO blacklist(uid,at,actor,reason) VALUES(2,0,0,'t')")
        self.assertFalse(self.bot.whitelisted(2))
        self.assertFalse(self.bot.exempt(2))
        self.bot.cfg['JEV_API_KEY'] = 'test'
        self.bot.jev.submit_target(self.msg(1, '日赚1000，私聊我'))
        self.assertIsNotNone(self.bot.db.execute('SELECT 1 FROM jev_cases').fetchone())

    def test_cannot_whitelist_blacklisted_member(self):
        with self.bot.db:
            self.bot.db.execute("INSERT INTO blacklist(uid,at,actor,reason) VALUES(2,0,0,'t')")
        self.white()
        self.assertFalse(self.bot.whitelisted(2))
        self.assertIn('黑名单优先', self.api.calls[-1][1]['text'])

    def test_remove_from_whitelist_restores_limits(self):
        self.white()
        self.bot._moderation_action({'chat': {'id': self.group}, 'text': '/unwhite 2',
                          'from': {'id': 1}, 'message_id': 2}, 2)
        self.assertFalse(self.bot.whitelisted(2))
        for i in range(1, 5):
            self.bot.handle({'update_id': i, 'message': self.msg(i, '/rank')})
        self.assertIsNone(self.bot.db.execute("SELECT 1 FROM sqlite_master WHERE name='command_usage'").fetchone())

    def test_manage_lists_whitelist_entries(self):
        self.white()
        self.api.calls.clear()
        self.bot.command({'chat': {'id': self.group}, 'text': '/manage',
                          'from': {'id': 1}, 'message_id': 3}, 3)
        text = [d['text'] for method, d in self.api.calls if method == 'sendMessage'][-1]
        self.assertIn('白名单', text)
        self.assertIn('2', text)

    def test_admin_is_exempt_without_whitelist(self):
        self.assertTrue(self.bot.exempt(1))
        self.assertFalse(self.bot.whitelisted(1))

    def test_non_admin_cannot_manage_whitelist(self):
        self.bot._moderation_action({'chat': {'id': self.group}, 'text': '/white 2',
                          'from': {'id': 2}, 'message_id': 4}, 4)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM whitelist').fetchone()[0], 0)

    def test_ai_cooldown_and_verification_skipped(self):
        self.white()
        self.api.calls.clear()
        with self.bot.db:
            self.bot.db.execute("INSERT INTO verification(uid,kind) VALUES(2,'member')")
            self.bot.db.execute('INSERT OR REPLACE INTO ai_cooldown VALUES(2,?)', (time.time(),))
        self.bot.handle({'update_id': 1, 'message': self.msg(1, '/ai 你好')})
        job = self.bot.db.execute('SELECT count(*) FROM ai_jobs').fetchone()[0]
        self.assertEqual(job, 1)
        self.assertFalse(any('请先完成入群验证' in str(d.get('text', '')) for m, d in self.api.calls if m == 'sendMessage'))

    def test_ai_cooldown_still_applies_without_whitelist(self):
        with self.bot.db:
            self.bot.db.execute('INSERT OR REPLACE INTO ai_cooldown VALUES(2,?)', (time.time(),))
        self.bot.handle({'update_id': 1, 'message': self.msg(1, '/ai 你好')})
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM ai_jobs').fetchone()[0], 0)

    def test_whitelisted_join_is_approved_without_challenge(self):
        self.white()
        self.bot.handle({'update_id': 1, 'chat_join_request': {
            'chat': {'id': self.group}, 'from': {'id': 2, 'is_bot': False}, 'user_chat_id': 2}})
        self.assertTrue(any(m == 'approveChatJoinRequest' for m, _ in self.api.calls))
        self.assertFalse(any(m == 'restrictChatMember' for m, _ in self.api.calls))
        self.assertIsNone(self.bot.db.execute('SELECT 1 FROM verification WHERE uid=2').fetchone())

    def test_whitelist_clears_pending_group_verification(self):
        self.bot.start_verification({'id': 2, 'is_bot': False}, 'member', self.group)
        self.assertTrue(any(m == 'restrictChatMember' for m, _ in self.api.calls))
        self.api.calls.clear()
        self.white()
        self.assertEqual(self.bot.db.execute('SELECT kind FROM verification WHERE uid=2').fetchone()[0], 'approved')
        self.assertTrue(any(m == 'restrictChatMember' for m, _ in self.api.calls))
        self.bot.handle({'update_id': 2, 'message': self.msg(2, '大家好')})
        self.assertFalse(any(m == 'deleteMessage' for m, _ in self.api.calls))

    def test_whitelist_approves_pending_join_request(self):
        self.bot.start_verification({'id': 2, 'is_bot': False}, 'request', 2)
        self.api.calls.clear()
        self.white()
        self.assertTrue(any(m == 'approveChatJoinRequest' for m, _ in self.api.calls))
        self.assertEqual(self.bot.db.execute('SELECT kind FROM verification WHERE uid=2').fetchone()[0], 'approved')

    def test_whitelisted_member_is_exempt_from_silent_checkin_blacklist(self):
        self.white()
        today = datetime.fromtimestamp(time.time(), CST).date()
        days = [(today - timedelta(days=n)).isoformat() for n in (1, 2, 3)]
        with self.bot.db:
            self.bot.db.execute("UPDATE meta SET value=? WHERE key='activity_started'", (str(time.time() - 40 * 86400),))
            self.bot.db.execute("INSERT OR REPLACE INTO meta VALUES('activity_heartbeat',?)", (str(time.time()),))
            self.bot.db.execute('DELETE FROM activity_gaps')
            self.bot.db.execute('INSERT OR REPLACE INTO point_users VALUES(2,?,60)', ('Vip',))
            for day in days:
                self.bot.db.execute('INSERT INTO point_events VALUES(?,?,?,?,?)', (f'c:{day}', 2, day, 'checkin', 3))
        self.bot.attention.tick(force=True)
        self.assertFalse(self.bot.blacklisted(2))
        self.assertEqual(self.bot.points.stats(2)[0], 60)


if __name__ == '__main__':
    unittest.main()
