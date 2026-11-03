import sqlite3
import unittest
from bot import Bot
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api, sqlite3.connect(':memory:'))
        self.bot.bot_id = 99
        self.bot.username = 'testbot'
        self.bot.can_manage_tags = True
        self.addCleanup(self.bot.db.close)

    def points(self, uid, total):
        with self.bot.db:
            self.bot.db.execute('INSERT OR REPLACE INTO point_users VALUES(?,?,?)', (uid, 'Name', total))

    def tags(self):
        return [d for m, d in self.api.calls if m == 'setChatMemberTag']

    def stored(self, uid):
        row = self.bot.db.execute('SELECT tag FROM upgrades WHERE uid=?', (uid,)).fetchone()
        return row[0] if row else None

    def test_ineligible_members_are_not_tagged(self):
        for uid, points, status, permission, stored in (
            (2,49,'member',True,None), (3,90,'administrator',True,'skipped'),
            (4,90,'member',False,None)):
            with self.subTest(points=points,status=status,permission=permission):
                self.points(uid,points)
                self.api.members[uid]={'user':{'id':uid},'status':status}
                self.bot.can_manage_tags=permission
                self.bot.upgrade_member(uid)
                self.assertEqual(self.tags(), [])
                self.assertEqual(self.stored(uid), stored)
                if points < 50:self.assertFalse(any(m == 'sendMessage' for m,d in self.api.calls))

    def test_threshold_upgrade_announces_and_is_applied_once(self):
        self.points(2, 50)
        self.bot.upgrade_member(2)
        self.bot.upgrade_member(2)
        self.assertEqual(self.tags(), [{'chat_id': -1, 'user_id': 2, 'tag': '家人'}])
        self.assertEqual(self.stored(2), '家人')
        messages = [d['text'] for m,d in self.api.calls if m == 'sendMessage']
        self.assertEqual(len(messages), 1)
        for text in ('恭喜', 'tg://user?id=2', '第 1 位', 'Name', '50'):
            self.assertIn(text, messages[0])

    def test_announcement_failure_keeps_tag(self):
        self.points(2, 60)
        self.api.failure = 'sendMessage'
        self.bot.upgrade_member(2)
        self.assertEqual(self.tags(), [{'chat_id': -1, 'user_id': 2, 'tag': '家人'}])
        self.assertEqual(self.stored(2), '家人')

    def test_custom_tag_is_not_overwritten(self):
        self.points(2, 80)
        self.api.members[2] = {'user': {'id': 2}, 'status': 'member', 'tag': '老铁'}
        self.bot.upgrade_member(2)
        self.assertEqual(self.tags(), [])
        self.assertEqual(self.stored(2), 'custom')
        self.assertEqual(sum(m == 'sendMessage' for m,d in self.api.calls), 1)

    def test_message_points_trigger_upgrade(self):
        self.points(2, 49)
        msg = {'chat': {'id': -1, 'type': 'supergroup'}, 'message_id': 1,
               'from': {'id': 2, 'first_name': 'Alice'}, 'text': 'hello'}
        self.bot.handle({'update_id': 1, 'message': msg})
        self.assertEqual(self.tags(), [{'chat_id': -1, 'user_id': 2, 'tag': '家人'}])

    def test_api_failure_leaves_no_record(self):
        self.points(2, 60)
        self.api.failure = 'setChatMemberTag'
        self.bot.upgrade_member(2)
        self.assertIsNone(self.stored(2))

if __name__ == '__main__':
    unittest.main()
