# coding: utf-8
"""运行核心测试：单群过滤、消息分发、回复文案、传输层错误与运维命令。"""
import io
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

import bot as botmod
from bot import Bot, DEFAULT_VERIFY_SECONDS, check, config
from botapi import API, APIError
from binlookup import BinError
from exchange import RateError
from tests.support import (BOT_ID, BOT_USERNAME, CHANNEL, GROUP, FakeAPI, group_message,
                           make_bot, mention)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.bot, self.api = make_bot()
        self.addCleanup(self.bot.db.close)

    # --- 辅助 -------------------------------------------------------------

    def message(self, mid, text, chat=GROUP, uid=2, chat_type='supergroup'):
        return group_message(mid, text, uid=uid) if chat == GROUP and chat_type == 'supergroup' \
            else {'update_id': mid, 'message': {
                'chat': {'id': chat, 'type': chat_type}, 'message_id': mid,
                'from': {'id': uid, 'first_name': 'U'}, 'text': text}}

    def deleted(self):
        return self.api.find('deleteMessage')

    def row(self, uid):
        return self.bot.db.execute('SELECT * FROM verification WHERE uid=?', (uid,)).fetchone()

    # --- 分发 -------------------------------------------------------------

    def test_other_groups_and_unrelated_updates_are_ignored(self):
        for update in ({'message': {'chat': {'id': -3, 'type': 'supergroup'}, 'from': {'id': 2},
                                    'message_id': 1, 'text': '/help'}},
                       {'chat_member': {'chat': {'id': -3}, 'from': {'id': 2}}},
                       {'chat_join_request': {'chat': {'id': -3}, 'from': {'id': 2}}},
                       {'edited_message': {'chat': {'id': GROUP}, 'message_id': 4, 'text': 'x'}}):
            with self.subTest(update=update):
                self.bot.handle(update)
        self.assertEqual(self.api.calls, [])

    def test_help_is_html_and_lists_current_features(self):
        self.bot.handle(self.message(1, '/help'))
        sent = self.api.find('sendMessage')[-1]
        self.assertEqual(sent['parse_mode'], 'HTML')
        for token in ('入群验证', '关联频道', '/start', '/help', '/rate', '/bin', '提问'):
            self.assertIn(token, sent['text'])
        for gone in ('原石', '抽卡', '黑名单', '/ai'):
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

    def test_group_start_points_at_private_chat(self):
        self.bot.handle(self.message(1, '/start'))
        self.assertIn('私聊', self.api.texts()[-1])

    def test_rate_and_bin_commands_are_dispatched_with_arguments(self):
        with patch.object(self.bot.exchange, 'convert', return_value='RATE_TEXT') as convert, \
                patch.object(self.bot.bin_lookup, 'query', return_value='BIN_TEXT') as query:
            self.bot.handle(self.message(1, '/rate 100 USD CNY'))
            # 指向其他机器人的命令静默忽略。
            before = len(self.api.calls)
            self.bot.handle(self.message(2, '/rate@otherbot 100 USD CNY'))
            self.assertEqual(len(self.api.calls), before)
            self.bot.handle(self.message(3, '/bin 45717360', uid=3))
            self.assertEqual(convert.call_args_list[0][0][0], ['100', 'USD', 'CNY'])
            self.assertEqual(convert.call_count, 1)
            self.assertEqual(query.call_args_list[0][0][0], ['45717360'])
            self.assertIn('RATE_TEXT', self.api.texts())
            self.assertIn('BIN_TEXT', self.api.texts())
            # 群内与私聊的成功回复都留在聊天里。
            self.bot.handle(self.message(4, '/rate USD CNY', chat=5, chat_type='private', uid=9))
            self.assertEqual(self.api.texts().count('RATE_TEXT'), 2)
            self.assertEqual(self.deleted(), [])

    def test_rate_and_bin_failures_are_reported_and_kept(self):
        with patch.object(self.bot.exchange, 'convert',
                          side_effect=RateError('汇率服务暂时不可用，请稍后重试。')), \
                patch.object(self.bot.bin_lookup, 'query',
                             side_effect=BinError('用法：/bin 45717360（6–8 位数字）。')):
            self.bot.handle(self.message(1, '/rate USD CNY'))
            self.bot.handle(self.message(2, '/bin x', uid=3))
        self.assertIn('汇率服务暂时不可用，请稍后重试。', self.api.texts())
        self.assertIn('用法：/bin 45717360（6–8 位数字）。', self.api.texts())
        self.assertEqual(self.deleted(), [])

    def test_tool_commands_are_cooled_down_per_user(self):
        with patch.object(self.bot.exchange, 'convert', return_value='RATE_TEXT') as convert:
            self.bot.handle(self.message(1, '/rate USD CNY'))
            self.bot.handle(self.message(2, '/rate USD CNY'))
            self.assertEqual(convert.call_count, 1)
            # 换个用户不受影响。
            self.bot.handle(self.message(3, '/rate USD CNY', uid=3))
            self.assertEqual(convert.call_count, 2)
            # 冷却过去后恢复。
            with patch.object(botmod.time, 'time', return_value=time.time() + 10):
                self.bot.handle(self.message(4, '/rate USD CNY'))
            self.assertEqual(convert.call_count, 3)

    def test_rate_and_bin_still_require_pending_verification_in_group(self):
        with patch.object(self.bot.exchange, 'convert', return_value='RATE_TEXT') as convert:
            self.bot.verification.admit({'id': 7, 'first_name': 'P'}, 'member', self.bot.group)
            self.api.clear()
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
            'chat': {'id': GROUP, 'type': 'supergroup'}, 'message_id': 11,
            'from': {'id': 500, 'is_bot': True}, 'text': '别的机器人'}})
        self.bot.handle({'update_id': 2, 'message': {
            'chat': {'id': GROUP, 'type': 'supergroup'}, 'message_id': 12,
            'from': {'id': BOT_ID, 'is_bot': True}, 'text': '自己发的'}})
        self.bot.handle({'update_id': 3, 'message': {
            'chat': {'id': GROUP, 'type': 'supergroup'}, 'message_id': 13,
            'sender_chat': {'id': -55, 'type': 'channel'}, 'from': {'id': 777000}, 'text': '频道帖'}})
        self.assertEqual(self.api.calls, [])

    # --- 提及与上下文 -----------------------------------------------------

    def test_mention_question_handles_text_caption_and_utf16_offsets(self):
        self.assertEqual(self.bot.mention_question(mention(1, '这是什么')['message']), '这是什么')
        # emoji 占两个 UTF-16 码元，实体偏移仍要算对。
        text = '😀 @testbot 卖多少'
        self.assertEqual(self.bot.mention_question(
            {'text': text, 'entities': [{'type': 'mention', 'offset': 3, 'length': 8}]}), '😀 卖多少')
        # 图片说明里的提及同样有效。
        self.assertEqual(self.bot.mention_question(
            {'caption': '看图 @testbot', 'caption_entities': [
                {'type': 'mention', 'offset': 3, 'length': 8}]}), '看图')
        # 没提及、提及但没有正文。
        self.assertIsNone(self.bot.mention_question({'text': '普通聊天'}))
        self.assertEqual(self.bot.mention_question(
            {'text': '@testbot  ', 'entities': [{'type': 'mention', 'offset': 0, 'length': 8}]}), '')

    def test_mention_question_ignores_other_users_text_mention(self):
        self.assertIsNone(self.bot.mention_question({
            'text': 'hi Bob', 'entities': [{'type': 'text_mention', 'offset': 3, 'length': 3,
                                           'user': {'id': 42, 'first_name': 'Bob'}}]}))

    def test_remember_keeps_only_recent_group_text(self):
        self.bot.remember(self.message(1, '第一条')['message'])
        self.bot.remember(self.message(2, '/help')['message'])
        self.bot.remember(self.message(3, '第二条')['message'])
        self.bot.remember({'chat': {'id': 5, 'type': 'private'}, 'message_id': 4,
                           'from': {'id': 5}, 'text': '私聊'})
        self.assertEqual([item['text'] for item in self.bot._recent[GROUP]], ['第一条', '第二条'])
        self.assertEqual(len(self.bot._recent), 1)
        for mid in range(100, 140):
            self.bot.remember(self.message(mid, f'消息{mid}')['message'])
        self.assertLessEqual(len(self.bot._recent[GROUP]), self.bot.AI_HISTORY * 2)

    def test_ai_context_carries_quoted_message_and_recent_chat(self):
        self.bot.remember(self.message(1, '上文一')['message'])
        self.bot.remember(self.message(2, '上文二')['message'])
        quoted = {'message_id': 9, 'from': {'id': 42, 'first_name': 'Bob'}, 'text': '被引用的句子'}
        lines = self.bot.ai_context({'chat': {'id': GROUP}, 'message_id': 10,
                                     'from': {'id': 2}, 'reply_to_message': quoted})
        self.assertIn('[引用] Bob：被引用的句子', lines)
        self.assertIn('[聊天] U：上文一', lines)

    def test_ai_image_id_prefers_largest_photo_and_limits_documents(self):
        quoted = {'photo': [{'file_id': 'small'}, {'file_id': 'large'}]}
        self.assertEqual(self.bot.ai_image_id(quoted), 'large')
        good = {'document': {'file_id': 'doc', 'mime_type': 'image/png', 'file_size': 1024}}
        self.assertEqual(self.bot.ai_image_id(good), 'doc')
        big = {'document': {'file_id': 'doc', 'mime_type': 'image/png',
                            'file_size': self.bot.AI_IMAGE_BYTES + 1}}
        self.assertEqual(self.bot.ai_image_id(big), '')
        other = {'document': {'file_id': 'doc', 'mime_type': 'application/zip', 'file_size': 10}}
        self.assertEqual(self.bot.ai_image_id(other), '')
        self.assertEqual(self.bot.ai_image_id({}), '')

    # --- 权限 -------------------------------------------------------------

    def test_restore_uses_chat_defaults_and_falls_back_to_sending_rights(self):
        self.api.chat = {'permissions': {}}
        self.bot.restore(7)
        permissions = self.api.find('restrictChatMember')[-1]['permissions']
        self.assertTrue(permissions['can_send_messages'])
        self.assertFalse(permissions['can_change_info'])
        # 群默认权限有缓存：第二次不再打 getChat。
        before = len(self.api.find('getChat'))
        self.bot.restore(8)
        self.assertEqual(len(self.api.find('getChat')), before)

    def test_restore_follows_chat_defaults_when_present(self):
        self.api.chat = {'permissions': {'can_send_messages': False, 'can_pin_messages': True}}
        self.bot.restore(7)
        permissions = self.api.find('restrictChatMember')[-1]['permissions']
        self.assertFalse(permissions['can_send_messages'])
        self.assertTrue(permissions['can_pin_messages'])

    def test_ban_always_requests_message_revocation(self):
        self.bot.call('banChatMember', chat_id=self.bot.group, user_id=2)
        self.assertTrue(self.api.find('banChatMember')[0]['revoke_messages'])

    def test_flood_control_is_retried_once(self):
        self.api.flood = 1
        with patch.object(botmod.time, 'sleep') as sleep:
            self.assertEqual(self.bot.send(GROUP, 'hi'), {'message_id': 1})
        sleep.assert_called_once()
        self.assertEqual(self.api.find('sendMessage')[0]['text'], 'hi')

    def test_persistent_flood_control_raises_after_one_retry(self):
        self.api.flood = 5
        with patch.object(botmod.time, 'sleep'):
            with self.assertRaises(APIError) as caught:
                self.bot.send(GROUP, 'hi')
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(len(self.api.find('sendMessage')), 2)

    # --- 更新分发 ---------------------------------------------------------

    def test_repeated_failures_abandon_the_update_instead_of_blocking_the_queue(self):
        self.bot.handle = lambda update: (_ for _ in ()).throw(ValueError('boom'))
        for _ in range(self.bot.UPDATE_ATTEMPTS - 1):
            with self.assertRaises(ValueError):
                self.bot.dispatch({'update_id': 7})
        self.bot.dispatch({'update_id': 7})     # 达到上限：放弃，不再抛
        actions = [row['action'] for row in self.bot.db.execute('SELECT action FROM audit')]
        self.assertIn('update_abandoned', actions)
        self.assertNotIn(7, self.bot._update_failures)

    def test_permanent_failures_are_audited_without_retrying(self):
        error = APIError('sendMessage', {'error_code': 400, 'description': 'Bad Request'})
        self.bot.handle = lambda update: (_ for _ in ()).throw(error)
        self.bot.dispatch({'update_id': 8})     # 不抛
        self.assertEqual(self.api.calls, [])
        detail = self.bot.db.execute('SELECT detail FROM audit').fetchone()['detail']
        self.assertIn('8', detail)

    def test_successful_updates_clear_the_failure_counter(self):
        self.bot.handle = lambda update: self.bot._update_failures.__setitem__(update['update_id'], 3)
        self.bot.dispatch({'update_id': 9})
        self.assertNotIn(9, self.bot._update_failures)

    # --- 运维 -------------------------------------------------------------

    def test_quick_poll_runs_only_while_verification_needs_work(self):
        self.assertFalse(self.bot.pending_verification())

        def store(**values):
            row = dict(uid=7, chat=GROUP, kind='request', nonce='n', question='2 + 2', answer=4,
                       options='[]', expires=int(time.time()) + 60, attempts=0,
                       state='passed', sent=1, send_attempts=1, card=5, restored=0,
                       restore_attempts=0, approved=1, approve_attempts=0,
                       finish_attempts=0, retry_at=0, updated=0)
            row.update(values)
            columns = ', '.join(row)
            placeholders = ', '.join(':' + name for name in row)
            with self.bot.db:
                self.bot.db.execute(f'INSERT OR REPLACE INTO verification({columns}) '
                                    f'VALUES({placeholders})', row)

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

    def test_join_request_without_user_chat_id_falls_back_to_the_user(self):
        self.bot.handle({'update_id': 1, 'chat_join_request': {
            'chat': {'id': GROUP, 'type': 'supergroup'},
            'from': {'id': 2, 'first_name': 'A'}}})
        self.assertEqual(self.api.find('sendMessage')[-1]['chat_id'], 2)
        self.assertIsNotNone(self.row(2))

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

    def test_audit_rows_are_pruned_by_age(self):
        now = int(time.time())
        with self.bot.db:
            self.bot.db.execute('INSERT INTO audit(at,actor,target,action,detail) VALUES(?,0,0,?,?)',
                                (now - (self.bot.AUDIT_DAYS + 1) * 86400, 'old', ''))
            self.bot.db.execute('INSERT INTO audit(at,actor,target,action,detail) VALUES(?,0,0,?,?)',
                                (now, 'new', ''))
        self.bot.prune_audit()
        actions = [row['action'] for row in self.bot.db.execute('SELECT action FROM audit')]
        self.assertEqual(actions, ['new'])

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

    def test_processed_ids_are_pruned_to_a_bounded_window(self):
        with self.bot.db:
            self.bot.db.executemany('INSERT INTO processed(id) VALUES(?)',
                                    [(i,) for i in range(1, self.bot.PROCESSED_LIMIT + 42)])
        self.bot.prune_processed()
        kept = [row[0] for row in self.bot.db.execute('SELECT id FROM processed ORDER BY id')]
        self.assertEqual(len(kept), self.bot.PROCESSED_LIMIT)
        self.assertEqual(kept[-1], self.bot.PROCESSED_LIMIT + 41)

    def test_check_reports_configuration_and_enforces_permissions(self):
        self.api.group_status = 'administrator'
        self.api.members[BOT_ID] = {'status': 'administrator', 'can_delete_messages': True,
                                    'can_restrict_members': True, 'can_invite_users': True}
        lines = []
        check(self.bot, out=lines.append)
        self.assertTrue(lines[0].startswith(f'ok @{BOT_USERNAME}'), lines)
        self.assertIn(f'group={GROUP}', lines[0])

        self.api.members[BOT_ID] = {'status': 'member'}
        with self.assertRaises(SystemExit) as caught:
            check(self.bot)
        self.assertIn('administrator', str(caught.exception))

    # --- 配置 -------------------------------------------------------------

    def test_config_defaults_and_verify_window_bounds(self):
        default = Bot({'CHANNEL_ID': str(CHANNEL)}, FakeAPI(), sqlite3.connect(':memory:'))
        self.addCleanup(default.db.close)
        self.assertEqual(default.group, -1003014905211)
        self.assertEqual(default.verify_seconds, DEFAULT_VERIFY_SECONDS)
        for value, expected in (('5', 30), ('9999', 300), ('120', 120),
                                ('abc', DEFAULT_VERIFY_SECONDS)):
            with self.subTest(value=value):
                bot = Bot({'GROUP_ID': str(GROUP), 'CHANNEL_ID': str(CHANNEL),
                           'VERIFY_SECONDS': value}, FakeAPI(), sqlite3.connect(':memory:'))
                self.addCleanup(bot.db.close)
                self.assertEqual(bot.verify_seconds, expected)

    def test_channel_link_is_derived_from_configuration(self):
        for value, expected in (({'TELEGRAM_CHANNEL': '@setup0de'}, 'https://t.me/setup0de'),
                                ({'TELEGRAM_CHANNEL_URL': 'https://t.me/setup0de'},
                                 'https://t.me/setup0de'),
                                ({'TELEGRAM_CHANNEL': 'setup0de'}, 'https://t.me/setup0de'),
                                ({'TELEGRAM_CHANNEL': 'n/a'}, '')):
            with self.subTest(value=value):
                bot = Bot({'GROUP_ID': str(GROUP), 'CHANNEL_ID': str(CHANNEL), **value},
                          FakeAPI(), sqlite3.connect(':memory:'))
                self.addCleanup(bot.db.close)
                self.assertEqual(bot.channel_url, expected)


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
        bot, api = make_bot()
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


class ConfigTests(unittest.TestCase):
    def test_environment_values_win_over_file(self):
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'env-token', 'GROUP_ID': '-1',
                                       'CHANNEL_ID': '-2'}, clear=False):
            self.assertEqual(config('/nonexistent.env')['TELEGRAM_BOT_TOKEN'], 'env-token')

    def test_missing_configuration_reads_file(self):
        import tempfile
        from pathlib import Path
        with patch.dict('os.environ', {}, clear=True), tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('TELEGRAM_BOT_TOKEN=file-token\nGROUP_ID=-1\nCHANNEL_ID=-2\n')
            values = config(str(path))
        self.assertEqual(values['TELEGRAM_BOT_TOKEN'], 'file-token')
        self.assertEqual(values['GROUP_ID'], '-1')

    def test_required_values_are_enforced(self):
        with self.assertRaises(RuntimeError):
            Bot({'GROUP_ID': str(GROUP)}, FakeAPI(), sqlite3.connect(':memory:'))
        with self.assertRaises(RuntimeError):
            Bot({'CHANNEL_ID': str(CHANNEL)}, None, sqlite3.connect(':memory:'))


if __name__ == '__main__':
    unittest.main()
