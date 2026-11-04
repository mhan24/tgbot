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
        self.addCleanup(self.bot.db.close)

    def panel(self, target=2, private=False):
        self.bot.command({'text': f'/manage {target}', 'from': {'id': 1},
                          'chat': {'id': 1 if private else -1}})
        return self.bot.db.execute('SELECT * FROM moderation_panels ORDER BY rowid DESC LIMIT 1').fetchone()

    def click(self, row, action, actor=1, gid=-1, chat=None):
        return self.bot.moderation.callback({'id': 'q', 'from': {'id': actor},
            'data': f'mod:{gid}:{row["token"]}:{action}',
            'message': {'chat': {'id': row['chat'] if chat is None else chat}, 'message_id': row['message']}})

    def test_old_commands_removed(self):
        for cmd in ('warn', 'warnings', 'resetwarn', 'mute', 'unmute', 'ban', 'unban', 'white', 'unwhite', 'whitelist'):
            self.assertFalse(self.bot.command({'text': f'/{cmd} 2', 'from': {'id': 1}}))
        self.assertFalse(any(m in ('banChatMember', 'restrictChatMember') for m, _ in self.api.calls))

    def test_duplicate_click_warns_only_once(self):
        row = self.panel()
        self.click(row, 'warn'); self.click(row, 'warn')
        self.assertEqual(self.bot.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone()[0], 1)

    def test_invalid_or_stale_panel_cannot_warn(self):
        row = self.panel()
        for case, kwargs in (
            ('group', {'gid':-2}), ('actor', {'actor':2}), ('chat', {'chat':-2}),
            ('lost_admin', {}), ('promoted_target', {}), ('expired', {})):
            with self.subTest(case=case):
                self.api.members[1]={'status':'member' if case == 'lost_admin' else 'creator'}
                self.api.members[2]={'status':'administrator' if case == 'promoted_target' else 'member'}
                if case == 'expired':
                    with self.bot.db:self.bot.db.execute('UPDATE moderation_panels SET expires=0')
                self.click(row, 'warn', **kwargs)
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

    def test_completed_ban_panel_replaces_stale_normal_status(self):
        row=self.panel()
        self.click(row,'confirm');self.click(row,'ban')
        edits=[d for m,d in self.api.calls if m=='editMessageText' and d.get('message_id')==row['message']]
        self.assertIn('状态：黑名单',edits[-1]['text'])
        self.assertNotIn('确认拉黑？',edits[-1]['text'])
        self.assertTrue(edits[-1]['reply_markup']['inline_keyboard'])

    def test_operation_updates_same_card_and_rotates_token(self):
        row=self.panel()
        sends=sum(m=='sendMessage' for m,d in self.api.calls)
        self.click(row,'resetwarn')
        current=self.bot.db.execute('SELECT * FROM moderation_panels ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(current['message'],row['message'])
        self.assertNotEqual(current['token'],row['token'])
        # The existing action feedback is the only new message; no second panel.
        self.assertEqual(sum(m=='sendMessage' for m,d in self.api.calls),sends+1)
        self.click(row,'warn')
        self.assertIsNone(self.bot.db.execute('SELECT * FROM warnings').fetchone())
    def test_more_menu_keeps_optional_operations(self):
        row=self.panel()
        self.click(row,'more')
        current=self.bot.db.execute('SELECT * FROM moderation_panels WHERE token=?',(row['token'],)).fetchone()
        text,keyboard=self.bot.moderation.render(current)
        labels=[b['text'] for line in keyboard for b in line]
        self.assertIn('加入白名单',labels)
        self.assertIn('返回',labels)

    def test_bot_list_filters_departed_members_and_preserves_ownership(self):
        self.bot.members.remember({'id':30,'is_bot':True,'first_name':'Helper','username':'example_bot'})
        self.bot.members.remember({'id':31,'is_bot':True,'first_name':'Gone'})
        self.api.members[30]={'status':'member','user':{'id':30,'is_bot':True}}
        self.api.members[31]={'status':'left','user':{'id':31,'is_bot':True}}
        self.bot.moderation.open(-1,1)
        row=self.bot.db.execute('SELECT * FROM moderation_panels ORDER BY rowid DESC LIMIT 1').fetchone()
        self.click(row,'bots0',actor=2)
        self.assertFalse(any(m=='editMessageText' for m,d in self.api.calls))
        self.click(row,'bots0')
        text=[d['text'] for m,d in self.api.calls if m=='editMessageText'][-1]
        self.assertIn('Helper',text);self.assertNotIn('Gone',text)
