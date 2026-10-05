import sqlite3
import time
import unittest
from bot import Bot, APIError
from test_bot import FakeAPI


class Tests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api, sqlite3.connect(':memory:'))
        self.bot.bot_id, self.bot.username = 99, 'testbot'

    def panel(self, target=2, private=False):
        self.bot.command({'text': f'/manage {target}', 'from': {'id': 1},
                          'chat': {'id': 1 if private else -1}}, 1)
        return self.bot.db.execute('SELECT * FROM moderation_panels ORDER BY rowid DESC LIMIT 1').fetchone()

    def click(self, row, action, actor=1, gid=-1, chat=None):
        return self.bot.moderation.callback({'id': 'q', 'from': {'id': actor},
            'data': f'mod:{gid}:{row["token"]}:{action}',
            'message': {'chat': {'id': row['chat'] if chat is None else chat}, 'message_id': row['message']}})

    def test_old_commands_removed(self):
        for cmd in ('warn', 'warnings', 'resetwarn', 'mute', 'unmute', 'ban', 'unban', 'white', 'unwhite', 'whitelist'):
            self.assertFalse(self.bot.command({'text': f'/{cmd} 2', 'from': {'id': 1}}, 1))
        self.assertFalse(any(m in ('banChatMember', 'restrictChatMember') for m, _ in self.api.calls))

    def test_duplicate_click_warns_only_once(self):
        row = self.panel()
        self.click(row, 'warn'); self.click(row, 'warn')
        self.assertEqual(self.bot.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone()[0], 1)

    def test_cross_group_and_wrong_actor_cannot_warn(self):
        row = self.panel()
        for kwargs in ({'gid': -2}, {'actor': 2}, {'chat': -2}):
            self.click(row, 'warn', **kwargs)
        self.assertIsNone(self.bot.db.execute('SELECT 1 FROM warnings').fetchone())

    def test_admin_rights_rechecked(self):
        row = self.panel()
        self.api.members[1] = {'status': 'member'}
        self.click(row, 'warn')
        self.assertIsNone(self.bot.db.execute('SELECT 1 FROM warnings').fetchone())

    def test_promoted_target_protected(self):
        row = self.panel()
        self.api.members[2] = {'status': 'administrator'}
        self.click(row, 'warn')
        self.assertIsNone(self.bot.db.execute('SELECT 1 FROM warnings').fetchone())

    def test_expired_panel(self):
        row = self.panel()
        self.bot.db.execute('UPDATE moderation_panels SET expires=0')
        self.click(row, 'warn')
        self.assertIsNone(self.bot.db.execute('SELECT 1 FROM warnings').fetchone())

    def test_ban_needs_confirmation_removes_white_and_messages(self):
        self.bot.db.execute('INSERT INTO whitelist VALUES(2,0,1,?)', ('test',))
        row = self.panel()
        self.click(row, 'ban')
        self.assertFalse(self.bot.blacklisted(2))
        self.click(row, 'confirm'); self.click(row, 'ban')
        self.assertTrue(self.bot.blacklisted(2))
        self.assertIsNone(self.bot.db.execute('SELECT 1 FROM whitelist WHERE uid=2').fetchone())
        self.assertTrue([d for m, d in self.api.calls if m == 'banChatMember'][0]['revoke_messages'])

    def test_unban_clears_warning_without_white(self):
        self.bot.db.execute('INSERT INTO warnings VALUES(2,3,0)')
        self.bot.blacklist_member(2)
        self.click(self.panel(), 'unban')
        self.assertFalse(self.bot.blacklisted(2))
        self.assertEqual(self.bot.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone()[0], 0)
        self.assertFalse(self.bot.whitelisted(2))

    def test_failed_unban_preserves_state(self):
        self.bot.blacklist_member(2)
        self.bot.db.execute('INSERT INTO warnings VALUES(2,3,0)')
        self.api.failure = 'unbanChatMember'
        with self.assertRaises(APIError):
            self.bot.unblacklist_member(2)
        self.assertTrue(self.bot.blacklisted(2))
        self.assertEqual(self.bot.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone()[0], 3)

    def test_manual_mute_is_separate_from_warning(self):
        self.click(self.panel(), 'hour')
        row = self.bot.db.execute('SELECT * FROM warnings WHERE uid=2').fetchone()
        self.assertEqual(row['count'], 0)
        self.assertGreater(row['mute_until'], time.time())
        self.assertFalse(self.bot.blacklisted(2))

    def test_private_command_dispatched(self):
        self.bot.handle({'update_id': 1, 'message': {'chat': {'id': 1, 'type': 'private'},
                         'from': {'id': 1}, 'message_id': 20, 'text': '/manage 2'}})
        row = self.bot.db.execute('SELECT * FROM moderation_panels').fetchone()
        self.assertEqual(row['chat'], 1)
        self.click(row, 'warn')
        self.assertEqual(self.bot.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone()[0], 1)

    def test_third_warning_delivery_retry_does_not_repeat_ban(self):
        self.bot.warn(2, 'first', 'test')
        self.bot.warn(2, 'second', 'test')
        self.api.failure = 'sendMessage'
        with self.assertRaises(APIError):
            self.bot.warn(2, 'third', 'test')
        self.api.failure = None
        self.bot.warn(2, 'third', 'test')
        self.assertEqual(sum(method == 'banChatMember' for method, _ in self.api.calls), 1)

    def test_failed_unmute_keeps_local_mute(self):
        self.bot.db.execute('INSERT INTO warnings VALUES(2,1,?)', (int(time.time()) + 3600,))
        row = self.panel()
        self.api.failure = 'restrictChatMember'
        with self.assertRaises(APIError):
            self.click(row, 'unmute')
        self.assertGreater(self.bot.db.execute('SELECT mute_until FROM warnings WHERE uid=2').fetchone()[0], time.time())

    def test_panel_survives_normal_delete_window(self):
        row = self.panel()
        due = self.bot.db.execute('SELECT due FROM deletions WHERE message_id=?', (row['message'],)).fetchone()[0]
        self.assertGreater(due, time.time() + 290)
