import sqlite3
import unittest

from bot import Bot
from test_bot import FakeAPI


class SettingsAPI(FakeAPI):
    def call(self, method, **data):
        if method == 'getChat' and data.get('chat_id') == '@secondchannel':
            self.calls.append((method, data))
            return {'id': -20, 'type': 'channel', 'username': 'secondchannel'}
        if method == 'getChat' and data.get('chat_id') == '@privatechannel':
            self.calls.append((method, data))
            return {'id': -21, 'type': 'channel'}
        if method == 'getChat' and data.get('chat_id') in (-2, -20, -21):
            self.calls.append((method, data))
            channel_id = data['chat_id']
            return {'id': channel_id, 'type': 'channel',
                    'username': 'mainchannel' if channel_id == -2 else
                    ('secondchannel' if channel_id == -20 else None)}
        return super().call(method, **data)


class OwnerAPI(SettingsAPI):
    def call(self, method, **data):
        if method == 'getChatMember' and data.get('user_id') == 1:
            self.calls.append((method, data))
            return {'status': 'creator', 'user': {'id': 1}}
        if method == 'getChatMember' and data.get('user_id') == 99:
            self.calls.append((method, data))
            return {'status': 'administrator', 'user': {'id': 99}}
        return super().call(method, **data)


class GroupSettingsTests(unittest.TestCase):
    def setUp(self):
        self.api = OwnerAPI()
        self.db = sqlite3.connect(':memory:')
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2',
                        'TELEGRAM_CHANNEL': '@mainchannel'}, self.api, self.db)
        self.bot.bot_id = 99
        self.bot._init_group_manager()

    def message(self, uid, text, reply_id=None):
        msg = {'chat': {'id': uid, 'type': 'private'}, 'from': {'id': uid}, 'text': text}
        if reply_id is not None:
            msg['reply_to_message'] = {'message_id': reply_id}
        return msg

    def panel_button(self, key, action='e', gid=-1, uid=1):
        self.bot.group_settings_command(self.message(uid, '/settings'))
        payload = next(data for method, data in reversed(self.api.calls)
                       if method == 'sendMessage' and 'reply_markup' in data)
        payload = dict(payload, message_id=self.api.sent)
        buttons = [button for row in payload['reply_markup']['inline_keyboard'] for button in row]
        return next(button['callback_data'] for button in buttons
                    if button['callback_data'] == f'gs:{action}:{gid}:{uid}:{key}')

    def click(self, data, uid=1, message_id=1):
        self.bot.group_settings_callback({
            'id': 'query', 'from': {'id': uid}, 'data': data,
            'message': {'chat': {'id': uid, 'type': 'private'}, 'message_id': message_id},
        })

    def begin_edit(self, key, action='e', gid=-1, uid=1):
        data = self.panel_button(key, action, gid, uid)
        panel = next(data for method, data in reversed(self.api.calls)
                     if method == 'sendMessage' and 'reply_markup' in data)
        panel = dict(panel, message_id=self.api.sent)
        self.click(data, uid, panel['message_id'])
        prompt = next(data for method, data in reversed(self.api.calls)
                      if method == 'sendMessage')
        self.assertNotIn('reply_markup', prompt)
        self.assertIn('/cancel', prompt['text'])
        self.assertFalse(self.api.calls[-1][1].get('show_alert'))
        return self.api.sent

    def test_settings_command_opens_button_panel_and_owner_changes_range_by_reply(self):
        prompt_id = self.begin_edit('CHECKIN_RANGE')
        self.assertTrue(self.bot.group_settings_reply(self.message(1, '5-10')))
        self.assertEqual((self.bot.checkin_min, self.bot.checkin_max), (5, 10))
        self.assertEqual(self.db.execute('SELECT value FROM group_settings WHERE key="CHECKIN_MIN"').fetchone()[0], '5')
        self.assertEqual(self.db.execute('SELECT value FROM group_settings WHERE key="CHECKIN_MAX"').fetchone()[0], '10')

        reloaded = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api, self.db)
        self.assertEqual((reloaded.points.checkin_min, reloaded.points.checkin_max), (5, 10))

    def test_cancel_and_commands_do_not_save_values(self):
        self.begin_edit('CHECKIN_RANGE')
        self.bot.group_settings_reply(self.message(1, '/help'))
        self.assertEqual(self.bot.checkin_min, 1)
        self.assertTrue(self.bot.group_settings_reply(self.message(1, '/cancel')))
        self.assertFalse(self.bot.group_settings_reply(self.message(1, '5-10')))
        self.assertEqual(self.bot.checkin_min, 1)

    def test_model_setting_removed_and_old_button_rejected(self):
        self.assertNotIn('AI_MODEL', self.bot.GROUP_SETTING_SPECS)
        self.click('gs:e:-1:1:AI_MODEL')
        self.assertIsNone(self.db.execute('SELECT * FROM group_setting_sessions').fetchone())

    def test_only_creator_can_open_or_use_settings_buttons(self):
        self.bot.group_settings_command(self.message(2, '/settings'))
        self.assertIn('只有已登记管理群的群主', self.api.calls[-1][1]['text'])
        data = self.panel_button('CHECKIN_RANGE')
        before = len(self.api.calls)
        self.click(data, uid=2)
        self.assertFalse(any(method == 'sendMessage' and data.get('reply_markup', {}).get('force_reply')
                             for method, data in self.api.calls[before:]))

    def test_invalid_value_prompts_again_and_range_can_reset(self):
        prompt_id = self.begin_edit('CHECKIN_RANGE')
        self.bot.group_settings_reply(self.message(1, '12-5', prompt_id))
        self.assertEqual((self.bot.checkin_min, self.bot.checkin_max), (1, 5))
        self.bot.group_settings_reply(self.message(1, '5-10', self.api.sent))
        self.assertEqual((self.bot.checkin_min, self.bot.checkin_max), (5, 10))

        data = self.panel_button('CHECKIN_RANGE', action='r')
        panel = next(data for method, data in reversed(self.api.calls)
                     if method == 'sendMessage' and 'reply_markup' in data)
        panel = dict(panel, message_id=self.api.sent)
        self.click(data, message_id=panel['message_id'])
        self.assertEqual((self.bot.checkin_min, self.bot.checkin_max), (1, 5))

    def test_group_channel_id_and_verification_link_are_group_specific(self):
        prompt_id = self.begin_edit('CHANNEL_ID')
        self.bot.group_settings_reply(self.message(1, '@secondchannel', prompt_id))
        row = self.db.execute('SELECT channel,channel_url FROM managed_groups WHERE chat=-1').fetchone()
        self.assertEqual((row['channel'], row['channel_url']), (-20, 'https://t.me/secondchannel'))
        self.assertEqual((self.bot.channel, self.bot.channel_url), (-20, 'https://t.me/secondchannel'))

        prompt_id = self.begin_edit('CHANNEL_URL')
        self.bot.group_settings_reply(self.message(1, 'https://t.me/private_invite', prompt_id))
        self.assertEqual(self.bot.channel_url, 'https://t.me/private_invite')

        reloaded = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2',
                        'TELEGRAM_CHANNEL': '@mainchannel'}, self.api, self.db)
        reloaded.bot_id = 99
        reloaded._init_group_manager()
        self.assertEqual((reloaded.channel, reloaded.channel_url),
                         (-20, 'https://t.me/private_invite'))

    def test_tenant_channel_link_does_not_leak_to_other_group(self):
        other_db = sqlite3.connect(':memory:')
        other = Bot({'GROUP_ID': '-2', 'CHANNEL_ID': '-3',
                     'TELEGRAM_CHANNEL_URL': 'https://t.me/secondchannel'}, self.api, other_db)
        other.manager_root = self.bot
        self.bot.managed_bots[-2] = other
        with self.db:
            self.db.execute('INSERT INTO managed_groups(chat,channel,title,db_path,enabled,channel_url) '
                            'VALUES(-2,-3,?,?,1,?)',
                            ('Second group', '/tmp/second.sqlite3', 'https://t.me/secondchannel'))

        other.start_verification({'id': 2, 'is_bot': False}, 'request', 2)
        self.assertIn({'text': '① 关注频道', 'url': 'https://t.me/secondchannel'},
                      self.api.calls[-1][1]['reply_markup']['inline_keyboard'][0])
        self.assertEqual(self.bot.channel_url, 'https://t.me/mainchannel')

    def test_channel_link_rejects_non_telegram_or_http_urls(self):
        for value in ('http://t.me/channel', 'https://evil.example/channel',
                      'https://t.me/channel?start=x', 'https://user@t.me/channel'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bot._valid_channel_url(value)


if __name__ == '__main__':
    unittest.main()
