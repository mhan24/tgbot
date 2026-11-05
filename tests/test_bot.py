# coding: utf-8
"""运行核心测试：单群过滤、消息生命周期、回复文案与传输层错误处理。"""
import io
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from bot import Bot, DEFAULT_VERIFY_SECONDS, config
from binlookup import BinError
from botapi import API, APIError
from exchange import RateError


class FakeAPI:
    """最小 Telegram 模拟；只实现当前核心真正调用的方法。"""

    def __init__(self):
        self.calls = []
        self.failure = None
        self.members = {}
        self.sent = 0
        self.channel_status = 'member'

    def call(self, method, **data):
        self.calls.append((method, data))
        if self.failure == method:
            raise APIError(method, {'error_code': 500, 'description': 'temporary failure'})
        if method == 'sendMessage':
            self.sent += 1
            return {'message_id': self.sent}
        if method == 'getChatMember':
            if data['user_id'] in self.members:
                return self.members[data['user_id']]
            if data['chat_id'] == -2:
                return {'status': self.channel_status}
            return {'status': 'member'}
        if method == 'getChat':
            return {'permissions': {'can_send_messages': True, 'can_send_photos': True,
                                    'can_invite_users': False}}
        return True

    def methods(self):
        return [method for method, _ in self.calls]

    def texts(self):
        return [data['text'] for method, data in self.calls if method == 'sendMessage']


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api, sqlite3.connect(':memory:'))
        self.bot.bot_id = 99
        self.bot.username = 'testbot'
        self.addCleanup(self.bot.db.close)

    def deleted(self):
        return [data for method, data in self.api.calls if method == 'deleteMessage']

    def message(self, mid, text, chat=-1, uid=2, chat_type='supergroup'):
        return {'update_id': mid, 'message': {
            'chat': {'id': chat, 'type': chat_type}, 'message_id': mid,
            'from': {'id': uid, 'first_name': 'U'}, 'text': text}}

    def test_other_groups_and_unrelated_updates_are_ignored(self):
        for update in ({'message': {'chat': {'id': -3, 'type': 'supergroup'}, 'from': {'id': 2},
                                    'message_id': 1, 'text': '/help'}},
                       {'chat_member': {'chat': {'id': -3}, 'from': {'id': 2}}},
                       {'chat_join_request': {'chat': {'id': -3}, 'from': {'id': 2}}},
                       {'edited_message': {'chat': {'id': -1}, 'message_id': 4, 'text': 'x'}}):
            with self.subTest(update=update):
                self.bot.handle(update)
        self.assertEqual(self.api.calls, [])

    def test_help_is_html_and_lists_current_features(self):
        self.bot.handle(self.message(1, '/help'))
        sent = [data for method, data in self.api.calls if method == 'sendMessage'][-1]
        self.assertEqual(sent['parse_mode'], 'HTML')
        for token in ('入群验证', '关联频道', '/start', '/help', '/rate', '/bin'):
            self.assertIn(token, sent['text'])
        for gone in ('原石', '抽卡', '黑名单', '尚未'):
            self.assertNotIn(gone, sent['text'])
        for gone in ('/checkin', '/points', '/rank', '/manage', '/report', '/appeal'):
            self.assertNotIn(gone, sent['text'])

    def test_help_accepts_bot_suffix_and_ignores_other_bot_suffix(self):
        self.bot.handle(self.message(1, '/help@testbot'))
        self.assertIn('/start', self.api.texts()[-1])
        before = len(self.api.calls)
        self.bot.handle(self.message(2, '/help@otherbot'))
        self.assertEqual(len(self.api.calls), before)

    def test_private_chat_explains_available_commands(self):
        self.bot.handle(self.message(1, '/start', chat=2, chat_type='private'))
        self.assertIn('入群申请', self.api.texts()[-1])
        self.bot.handle(self.message(2, '/unknown', chat=2, chat_type='private'))
        self.assertIn('/help', self.api.texts()[-1])

    def test_private_replies_only_to_unknown_commands(self):
        self.bot.handle(self.message(1, '你好', chat=2, chat_type='private'))
        self.assertEqual(self.api.calls, [])
        self.bot.handle(self.message(2, '/unknown', chat=2, chat_type='private'))
        self.assertIn('/help', self.api.texts()[-1])
        before = len(self.api.calls)
        self.bot.handle(self.message(3, '/help@otherbot', chat=2, chat_type='private'))
        self.assertEqual(len(self.api.calls), before)

    def test_unknown_group_command_points_to_help(self):
        self.bot.handle(self.message(1, '/checkin'))
        self.assertIn('/help', self.api.texts()[-1])
        self.assertEqual(self.deleted(), [])

    def test_rate_and_bin_commands_are_dispatched_with_arguments(self):
        with patch.object(self.bot.exchange, 'convert', return_value='RATE_TEXT') as convert, \
                patch.object(self.bot.bin_lookup, 'query', return_value='BIN_TEXT') as query:
            self.bot.handle(self.message(1, '/rate 100 USD CNY'))
            # 指向其他机器人的命令静默忽略。
            self.bot.handle(self.message(2, '/rate@otherbot 100 USD CNY'))
            self.bot.handle(self.message(3, '/bin 45717360'))
            self.assertEqual(convert.call_args_list[0][0][0], ['100', 'USD', 'CNY'])
            self.assertEqual(convert.call_count, 1)
            self.assertEqual(query.call_args_list[0][0][0], ['45717360'])
            self.assertIn('RATE_TEXT', self.api.texts())
            self.assertIn('BIN_TEXT', self.api.texts())
            # 群内与私聊的成功回复都留在聊天里。
            self.bot.handle(self.message(4, '/rate USD CNY', chat=5, chat_type='private'))
            self.assertEqual(self.api.texts().count('RATE_TEXT'), 2)
            self.assertEqual(self.deleted(), [])

    def test_rate_and_bin_failures_are_reported_and_kept(self):
        with patch.object(self.bot.exchange, 'convert',
                          side_effect=RateError('汇率服务暂时不可用，请稍后重试。')), \
                patch.object(self.bot.bin_lookup, 'query',
                             side_effect=BinError('用法：/bin 45717360（6–8 位数字）。')):
            self.bot.handle(self.message(1, '/rate USD CNY'))
            self.bot.handle(self.message(2, '/bin x'))
        self.assertIn('汇率服务暂时不可用，请稍后重试。', self.api.texts())
        self.assertIn('用法：/bin 45717360（6–8 位数字）。', self.api.texts())
        self.assertEqual(self.deleted(), [])

    def test_quick_poll_runs_only_while_verification_needs_work(self):
        self.assertFalse(self.bot.pending_verification())

        def store(**values):
            row = dict(uid=7, chat=-1, kind='request', nonce='n', question='2 + 2', answer=4,
                       options='[]', expires=int(time.time()) + 60, attempts=0,
                       state='passed', sent=1, send_attempts=1, restored=0,
                       restore_attempts=0, approved=1, approve_attempts=0,
                       finish_attempts=0, retry_at=0, updated=0)
            row.update(values)
            with self.bot.db:
                self.bot.db.execute(
                    'INSERT OR REPLACE INTO verification(uid,chat,kind,nonce,question,answer,'
                    'options,expires,attempts,state,sent,send_attempts,restored,restore_attempts,'
                    'approved,approve_attempts,finish_attempts,retry_at,updated) '
                    'VALUES(:uid,:chat,:kind,:nonce,:question,:answer,:options,:expires,:attempts,'
                    ':state,:sent,:send_attempts,:restored,:restore_attempts,:approved,'
                    ':approve_attempts,:finish_attempts,:retry_at,:updated)', row)

        store()  # 已批准、已恢复的通过记录不占用快速轮询
        self.assertFalse(self.bot.pending_verification())
        store(state='challenge')
        self.assertTrue(self.bot.pending_verification())
        store(kind='member', state='passed', restored=0)
        self.assertTrue(self.bot.pending_verification())
        store(kind='member', state='passed', restored=1)
        self.assertFalse(self.bot.pending_verification())
        store(kind='request', state='passed', approved=0)
        self.assertTrue(self.bot.pending_verification())
        store(kind='request', state='passed', approved=1)
        self.assertFalse(self.bot.pending_verification())

    def test_rate_and_bin_still_require_pending_verification_in_group(self):
        with patch.object(self.bot.exchange, 'convert', return_value='RATE_TEXT') as convert:
            self.bot.verification.admit({'id': 7, 'first_name': 'P'}, 'member', self.bot.group)
            self.api.calls.clear()
            self.bot.handle(self.message(1, '/rate USD CNY', uid=7))
        convert.assert_not_called()
        self.assertEqual(self.api.calls, [])

    def test_group_and_private_replies_are_never_deleted(self):
        for chat, chat_type in ((self.bot.group, 'supergroup'), (2, 'private')):
            self.bot.handle(self.message(1, '/help', chat=chat, chat_type=chat_type))
            self.bot.send(chat, '普通提示')
        self.assertEqual(self.deleted(), [])

    def test_other_bot_and_channel_posts_are_left_alone(self):
        self.bot.handle({'update_id': 1, 'message': {
            'chat': {'id': -1, 'type': 'supergroup'}, 'message_id': 11,
            'from': {'id': 500, 'is_bot': True}, 'text': '别的机器人'}})
        self.bot.handle({'update_id': 2, 'message': {
            'chat': {'id': -1, 'type': 'supergroup'}, 'message_id': 12,
            'from': {'id': 99, 'is_bot': True}, 'text': '自己发的'}})
        self.bot.handle({'update_id': 3, 'message': {
            'chat': {'id': -1, 'type': 'supergroup'}, 'message_id': 13,
            'sender_chat': {'id': -55, 'type': 'channel'}, 'from': {'id': 777000}, 'text': '频道帖'}})
        self.bot.maintenance()
        self.assertEqual(self.deleted(), [])

    def test_schema_holds_only_current_tables(self):
        tables = {row[0] for row in self.bot.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'meta', 'processed', 'audit',
                                   'verification', 'rates', 'bin_cache'})
        self.assertFalse(tables & {'point_users', 'point_events', 'checkin_pity', 'checkin_draws',
                                   'mutes', 'upgrades', 'private_steps', 'known_bots',
                                   'user_activity'})

    def test_audit_rows_are_recorded(self):
        self.bot.audit(1, 2, 'verified', 'detail')
        row = self.bot.db.execute('SELECT * FROM audit').fetchone()
        self.assertEqual((row['actor'], row['target'], row['action'], row['detail']),
                         (1, 2, 'verified', 'detail'))

    def test_config_defaults_and_verify_window_bounds(self):
        default = Bot({'CHANNEL_ID': '-2'}, FakeAPI(), sqlite3.connect(':memory:'))
        self.addCleanup(default.db.close)
        self.assertEqual(default.group, -1003014905211)
        self.assertEqual(default.verify_seconds, DEFAULT_VERIFY_SECONDS)
        for value, expected in (('5', 30), ('9999', 300), ('120', 120),
                                ('abc', DEFAULT_VERIFY_SECONDS)):
            with self.subTest(value=value):
                bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2', 'VERIFY_SECONDS': value},
                          FakeAPI(), sqlite3.connect(':memory:'))
                self.addCleanup(bot.db.close)
                self.assertEqual(bot.verify_seconds, expected)

    def test_channel_link_is_derived_from_configuration(self):
        for value, expected in (({'TELEGRAM_CHANNEL': '@setup0de'}, 'https://t.me/setup0de'),
                                ({'TELEGRAM_CHANNEL_URL': 'https://t.me/setup0de'},
                                 'https://t.me/setup0de'),
                                ({'TELEGRAM_CHANNEL': 'setup0de'}, 'https://t.me/setup0de'),
                                ({'TELEGRAM_CHANNEL': 'n/a'}, '')):
            with self.subTest(value=value):
                bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2', **value}, FakeAPI(),
                          sqlite3.connect(':memory:'))
                self.addCleanup(bot.db.close)
                self.assertEqual(bot.channel_url, expected)

    def test_processed_ids_are_pruned_to_a_bounded_window(self):
        with self.bot.db:
            self.bot.db.executemany('INSERT INTO processed(id) VALUES(?)',
                                    [(i,) for i in range(1, self.bot.PROCESSED_LIMIT + 42)])
        self.bot.prune_processed()
        kept = [row[0] for row in self.bot.db.execute('SELECT id FROM processed ORDER BY id')]
        self.assertEqual(len(kept), self.bot.PROCESSED_LIMIT)
        self.assertEqual(kept[-1], self.bot.PROCESSED_LIMIT + 41)

    def test_join_request_without_user_chat_id_falls_back_to_the_user(self):
        self.bot.handle({'update_id': 1, 'chat_join_request': {
            'chat': {'id': -1, 'type': 'supergroup'},
            'from': {'id': 2, 'first_name': 'A'}}})
        card = [data for method, data in self.api.calls if method == 'sendMessage'][-1]
        self.assertEqual(card['chat_id'], 2)
        self.assertIsNotNone(self.bot.db.execute(
            'SELECT 1 FROM verification WHERE uid=2').fetchone())

    def test_maintenance_isolates_task_failures_with_cooldown(self):
        seen = []

        def failure():
            seen.append('failed')
            raise RuntimeError('temporary')

        def tick():
            seen.append('tick')

        with patch.object(self.bot.verification, 'tick', side_effect=failure), \
                patch.object(self.bot, 'prune_processed', side_effect=tick), \
                patch('bot.time.time', return_value=100):
            self.bot.maintenance()
            self.assertEqual(seen, ['failed', 'tick'])
            # 冷却期内两项任务都不再执行。
            self.bot.maintenance()
            self.assertEqual(seen, ['failed', 'tick'])
            with patch('bot.time.time', return_value=131):
                self.bot.maintenance()
            self.assertEqual(seen, ['failed', 'tick', 'failed'])
            with patch('bot.time.time', return_value=701):
                self.bot.maintenance()
            self.assertEqual(seen.count('tick'), 2)


class TransportTests(unittest.TestCase):
    def test_invalid_payload_is_retryable_and_hides_token(self):
        for payload in (b'invalid', b'[]', b'{"ok":true}'):
            with self.subTest(payload=payload), \
                    patch('botapi.urllib.request.urlopen', return_value=io.BytesIO(payload)):
                with self.assertRaises(APIError) as caught:
                    API('fake-token').call('getMe')
                self.assertEqual(caught.exception.code, 503)
                self.assertNotIn('fake-token', str(caught.exception))

    def test_valid_payload_is_returned(self):
        payload = json.dumps({'ok': True, 'result': {'id': 99}}).encode()
        with patch('botapi.urllib.request.urlopen', return_value=io.BytesIO(payload)):
            self.assertEqual(API('fake-token').call('getMe'), {'id': 99})

    def test_permanent_flag_follows_error_code(self):
        self.assertTrue(APIError('x', {'error_code': 400, 'description': 'bad'}).permanent)
        self.assertTrue(APIError('x', {'error_code': 403, 'description': 'denied'}).permanent)
        self.assertFalse(APIError('x', {'error_code': 500, 'description': 'boom'}).permanent)

    def test_expired_callback_acknowledgement_is_swallowed_but_others_raise(self):
        bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, FakeAPI(), sqlite3.connect(':memory:'))
        self.addCleanup(bot.db.close)
        for description, swallowed in (('Bad Request: query is too old', True),
                                       ('Bad Request: query id is invalid', True),
                                       ('Bad Request: chat not found', False)):
            with self.subTest(description=description):
                error = APIError('answerCallbackQuery',
                                 {'error_code': 400, 'description': description})
                with patch.object(bot.api, 'call', side_effect=error):
                    if swallowed:
                        self.assertFalse(bot.call('answerCallbackQuery', callback_query_id='q'))
                    else:
                        with self.assertRaises(APIError):
                            bot.call('answerCallbackQuery', callback_query_id='q')

    def test_ban_always_requests_message_revocation(self):
        api = FakeAPI()
        bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, api, sqlite3.connect(':memory:'))
        self.addCleanup(bot.db.close)
        bot.call('banChatMember', chat_id=bot.group, user_id=2)
        bans = [data for method, data in api.calls if method == 'banChatMember']
        self.assertTrue(bans[0]['revoke_messages'])


class ConfigTests(unittest.TestCase):
    def test_environment_values_win_over_file(self):
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'env-token', 'GROUP_ID': '-1',
                                       'CHANNEL_ID': '-2'}, clear=False):
            self.assertEqual(config('/nonexistent.env')['TELEGRAM_BOT_TOKEN'], 'env-token')

    def test_missing_configuration_reads_file(self):
        from pathlib import Path
        import tempfile
        with patch.dict('os.environ', {}, clear=True), tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('TELEGRAM_BOT_TOKEN=file-token\nGROUP_ID=-1\nCHANNEL_ID=-2\n')
            values = config(str(path))
        self.assertEqual(values['TELEGRAM_BOT_TOKEN'], 'file-token')
        self.assertEqual(values['GROUP_ID'], '-1')


if __name__ == '__main__':
    unittest.main()
