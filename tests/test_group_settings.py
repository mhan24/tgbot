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

    def test_settings_command_opens_button_panel_and_owner_changes_int_by_reply(self):
        prompt_id = self.begin_edit('MESSAGE_POINTS')
        self.assertTrue(self.bot.group_settings_reply(self.message(1, '3')))
        self.assertEqual(self.bot.message_points, 3)
        self.assertEqual(self.db.execute('SELECT value FROM group_settings WHERE key="MESSAGE_POINTS"').fetchone()[0], '3')

        reloaded = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api, self.db)
        self.assertEqual(reloaded.points.message_points, 3)

    def test_cancel_and_commands_do_not_save_values(self):
        self.begin_edit('MESSAGE_POINTS')
        self.bot.group_settings_reply(self.message(1, '/help'))
        self.assertEqual(self.bot.message_points, 1)
        self.assertTrue(self.bot.group_settings_reply(self.message(1, '/cancel')))
        self.assertFalse(self.bot.group_settings_reply(self.message(1, '3')))
        self.assertEqual(self.bot.message_points, 1)

    def test_obsolete_or_invalid_stored_settings_are_deleted_on_startup(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO group_settings VALUES('AI_MODEL','gpt')")
            self.db.execute("INSERT OR REPLACE INTO group_settings VALUES('CHECKIN_MIN','1')")
            self.db.execute("INSERT OR REPLACE INTO group_settings VALUES('MESSAGE_POINTS','abc')")
            self.db.execute("INSERT OR REPLACE INTO group_settings VALUES('JEV_ENABLED','yes')")
        reloaded = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api, self.db)
        keys = {row[0] for row in self.db.execute('SELECT key FROM group_settings')}
        self.assertEqual(keys, set())
        self.assertEqual(reloaded.message_points, 1)
        self.assertEqual(reloaded.upgrade_points, 50)
        self.assertTrue(reloaded.setting_values['JEV_ENABLED'])

    def test_bool_settings_accept_only_canonical_tokens(self):
        for token, expected in (('1', True), ('on', True), ('0', False), ('off', False)):
            self.assertEqual(self.bot.parse_group_setting('JEV_ENABLED', token), expected, token)
        for token in ('yes', 'true', '开启', '关闭', 'no'):
            with self.assertRaises(ValueError):
                self.bot.parse_group_setting('JEV_ENABLED', token)

    def test_model_setting_removed_and_old_button_rejected(self):
        for key in ('AI_MODEL', 'CHECKIN_MIN', 'CHECKIN_MAX', 'AI_ENABLED', 'AI_COOLDOWN_SECONDS',
                    'HISTORY_MESSAGES', 'HISTORY_CONTEXT'):
            self.assertNotIn(key, self.bot.GROUP_SETTING_SPECS)
        self.click('gs:e:-1:1:AI_MODEL')
        self.click('gs:t:-1:1:AI_ENABLED')
        self.assertIsNone(self.db.execute('SELECT * FROM group_setting_sessions').fetchone())

    def test_only_creator_can_open_or_use_settings_buttons(self):
        self.bot.group_settings_command(self.message(2, '/settings'))
        self.assertIn('只有本群群主', self.api.calls[-1][1]['text'])
        data = self.panel_button('MESSAGE_POINTS')
        before = len(self.api.calls)
        self.click(data, uid=2)
        self.assertFalse(any(method == 'sendMessage' and data.get('reply_markup', {}).get('force_reply')
                             for method, data in self.api.calls[before:]))

    def test_invalid_value_prompts_again_and_int_can_reset(self):
        prompt_id = self.begin_edit('MESSAGE_POINTS')
        self.bot.group_settings_reply(self.message(1, '0', prompt_id))
        self.assertEqual(self.bot.message_points, 1)
        self.bot.group_settings_reply(self.message(1, '3', self.api.sent))
        self.assertEqual(self.bot.message_points, 3)

        data = self.panel_button('MESSAGE_POINTS', action='r')
        panel = next(data for method, data in reversed(self.api.calls)
                     if method == 'sendMessage' and 'reply_markup' in data)
        panel = dict(panel, message_id=self.api.sent)
        self.click(data, message_id=panel['message_id'])
        self.assertEqual(self.bot.message_points, 1)

    def test_group_channel_id_and_verification_link_are_group_specific(self):
        prompt_id = self.begin_edit('CHANNEL_ID')
        self.bot.group_settings_reply(self.message(1, '@secondchannel', prompt_id))
        self.assertEqual((self.bot.channel, self.bot.channel_url), (-20, 'https://t.me/secondchannel'))

        prompt_id = self.begin_edit('CHANNEL_URL')
        self.bot.group_settings_reply(self.message(1, 'https://t.me/private_invite', prompt_id))
        self.assertEqual(self.bot.channel_url, 'https://t.me/private_invite')

        reloaded = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2',
                        'TELEGRAM_CHANNEL': '@mainchannel'}, self.api, self.db)
        reloaded.bot_id = 99
        self.assertEqual((reloaded.channel, reloaded.channel_url),
                         (-20, 'https://t.me/private_invite'))

    def test_channel_link_rejects_non_telegram_or_http_urls(self):
        for value in ('http://t.me/channel', 'https://evil.example/channel',
                      'https://t.me/channel?start=x', 'https://user@t.me/channel'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bot._valid_channel_url(value)


if __name__ == '__main__':
    unittest.main()
