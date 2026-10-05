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

    def points(self, uid, total):
        with self.bot.db:
            self.bot.db.execute('INSERT OR REPLACE INTO point_users VALUES(?,?,?)', (uid, 'Name', total))

    def tags(self):
        return [d for m, d in self.api.calls if m == 'setChatMemberTag']

    def stored(self, uid):
        row = self.bot.db.execute('SELECT tag FROM upgrades WHERE uid=?', (uid,)).fetchone()
        return row[0] if row else None

    def test_below_threshold_does_not_tag(self):
        self.points(2, 49)
        self.bot.upgrade_member(2)
        self.assertEqual(self.tags(), [])
        self.assertIsNone(self.stored(2))

    def test_threshold_sets_family_tag(self):
        self.points(2, 50)
        self.bot.upgrade_member(2)
        self.assertEqual(self.tags(), [{'chat_id': -1, 'user_id': 2, 'tag': '家人'}])
        self.assertEqual(self.stored(2), '家人')

    def test_group_congratulation_mentions_member(self):
        self.points(2, 50)
        self.api.members[2] = {'user': {'id': 2, 'is_bot': False, 'first_name': 'A<l>ice'}, 'status': 'member'}
        self.bot.upgrade_member(2)
        group = [d for m, d in self.api.calls if m == 'sendMessage' and d['chat_id'] == -1]
        self.assertEqual(len(group), 1)
        text = group[0]['text']
        self.assertIn('恭喜', text)
        self.assertIn('tg://user?id=2', text)
        self.assertIn('第 1 位', text)
        self.assertIn('Name', text)
        self.assertIn('50', text)

    def test_congratulation_when_tag_kept(self):
        self.points(2, 80)
        self.api.members[2] = {'user': {'id': 2, 'is_bot': False, 'first_name': 'Bob'}, 'status': 'member', 'tag': '老铁'}
        self.bot.upgrade_member(2)
        self.assertEqual([m for m, _ in self.api.calls if m == 'sendMessage'], ['sendMessage'])

    def test_no_congratulation_below_threshold(self):
        self.points(2, 49)
        self.bot.upgrade_member(2)
        self.assertEqual([m for m, _ in self.api.calls if m == 'sendMessage'], [])

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

    def test_upgrade_is_applied_once(self):
        self.points(2, 120)
        self.bot.upgrade_member(2)
        self.bot.upgrade_member(2)
        self.assertEqual(len(self.tags()), 1)

    def test_administrator_is_skipped(self):
        self.points(2, 90)
        self.api.members[2] = {'user': {'id': 2}, 'status': 'administrator'}
        self.bot.upgrade_member(2)
        self.assertEqual(self.tags(), [])
        self.assertEqual(self.stored(2), 'skipped')

    def test_disabled_without_manage_tags_right(self):
        self.bot.can_manage_tags = False
        self.points(2, 90)
        self.bot.upgrade_member(2)
        self.assertEqual(self.tags(), [])
        self.assertIsNone(self.stored(2))

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
