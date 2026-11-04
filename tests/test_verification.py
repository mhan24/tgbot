# coding: utf-8
"""入群验证测试：频道校验、算术题、超时/答错拒绝、失败恢复与卡片清理。"""
import sqlite3
import time
import unittest
from unittest.mock import patch

from bot import Bot
from botapi import APIError
from test_bot import FakeAPI

CFG = {'GROUP_ID': '-1', 'CHANNEL_ID': '-2', 'VERIFY_SECONDS': '240',
       'DELETE_AFTER_SECONDS': '30', 'TELEGRAM_CHANNEL': '@setup0de'}


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot(dict(CFG), self.api, sqlite3.connect(':memory:'))
        self.bot.bot_id = 99
        self.bot.username = 'testbot'
        self.addCleanup(self.bot.db.close)

    # --- 辅助 -------------------------------------------------------------

    def request(self, uid=2):
        """申请人提交入群申请：题目通过私聊发送。"""
        self.bot.handle({'update_id': 1, 'chat_join_request': {
            'chat': {'id': -1, 'type': 'supergroup'},
            'from': {'id': uid, 'first_name': 'A'}, 'user_chat_id': uid}})
        return self.row(uid)

    def join(self, uid=2, inviter=500, is_bot=False):
        """成员被拉入群：触发 chat_member 更新。"""
        user = {'id': uid, 'first_name': 'A', 'is_bot': is_bot}
        self.bot.handle({'update_id': 5, 'chat_member': {
            'chat': {'id': -1}, 'from': {'id': inviter, 'first_name': 'Inv'},
            'old_chat_member': {'user': user, 'status': 'left'},
            'new_chat_member': {'user': user, 'status': 'member'}}})

    def leave(self, uid=2):
        self.bot.handle({'update_id': 70, 'chat_member': {
            'chat': {'id': -1}, 'from': {'id': uid},
            'old_chat_member': {'user': {'id': uid}, 'status': 'member'},
            'new_chat_member': {'user': {'id': uid}, 'status': 'left'}}})

    def row(self, uid=2):
        return self.bot.db.execute('SELECT * FROM verification WHERE uid=?', (uid,)).fetchone()

    def click(self, row, answer=None, uid=None, nonce=None):
        answer = row['answer'] if answer is None else answer
        chat = {'id': row['chat'], 'type': 'private' if row['chat'] > 0 else 'supergroup'}
        self.bot.handle({'update_id': 90, 'callback_query': {
            'id': 'q', 'from': {'id': row['uid'] if uid is None else uid},
            'data': f'v:{row["uid"]}:{row["nonce"] if nonce is None else nonce}:{answer}',
            'message': {'chat': chat, 'message_id': 1}}})

    def methods(self):
        return self.api.methods()

    def alerts(self):
        return [data.get('text', '') for method, data in self.api.calls
                if method == 'answerCallbackQuery']

    def last_card(self):
        return [data for method, data in self.api.calls if method == 'sendMessage'][-1]

    def buttons(self):
        keyboard = self.last_card()['reply_markup']['inline_keyboard']
        return [button for row in keyboard for button in row]

    # --- 申请路径 ---------------------------------------------------------

    def test_join_request_sends_channel_link_and_math_question(self):
        row = self.request()
        self.assertEqual((row['kind'], row['state']), ('request', 'challenge'))
        self.assertEqual(row['chat'], 2)
        card = self.last_card()
        self.assertEqual(card['chat_id'], 2)
        self.assertIn('关注频道', card['text'])
        self.assertIn('4 分钟', card['text'])
        buttons = self.buttons()
        self.assertEqual(buttons[0], {'text': '① 关注频道', 'url': 'https://t.me/setup0de'})
        numbers = [int(button['text']) for button in buttons[1:]]
        self.assertEqual(len(numbers), 4)
        self.assertIn(row['answer'], numbers)
        self.assertTrue(all(button['callback_data'].startswith('v:2:') for button in buttons[1:]))

    def test_channel_subscription_is_checked_before_approval(self):
        row = self.request()
        self.api.channel_status = 'left'
        self.click(row)
        self.assertNotIn('approveChatJoinRequest', self.methods())
        self.assertEqual(self.row()['attempts'], 0)
        self.assertIn('尚未检测到关注频道', self.alerts()[-1])
        self.api.channel_status = 'member'
        self.click(row)
        self.assertEqual(self.methods().count('approveChatJoinRequest'), 1)
        self.assertEqual(self.row()['state'], 'passed')
        self.assertIn('验证成功', self.alerts()[-1])

    def test_restricted_channel_member_counts_as_subscribed(self):
        row = self.request()
        original = self.api.call

        def restricted_member(method, **data):
            if method == 'getChatMember' and data['chat_id'] == -2:
                return {'status': 'restricted', 'is_member': True}
            return original(method, **data)

        with patch.object(self.api, 'call', side_effect=restricted_member):
            self.click(row)
        self.assertEqual(self.methods().count('approveChatJoinRequest'), 1)

    def test_three_wrong_answers_decline_the_request(self):
        row = self.request()
        for expected in (1, 2):
            self.click(row, answer=row['answer'] + 9)
            self.assertEqual(self.row()['attempts'], expected)
            self.assertNotIn('declineChatJoinRequest', self.methods())
        self.click(row, answer=row['answer'] + 9)
        self.assertEqual(self.methods().count('declineChatJoinRequest'), 1)
        self.assertIsNone(self.row())
        self.assertIn('3 次', self.alerts()[-1])

    def test_other_members_cannot_answer_a_group_challenge(self):
        self.join(2)
        row = self.row()
        self.click(row, uid=3)
        self.assertEqual(self.row()['state'], 'challenge')
        self.assertNotIn('unbanChatMember', self.methods())
        self.assertIn('其他成员', self.alerts()[-1])

    def test_stale_or_expired_challenge_cannot_approve(self):
        row = self.request()
        self.click(row, nonce='deadbeef')
        self.assertIn('已失效', self.alerts()[-1])
        self.assertNotIn('approveChatJoinRequest', self.methods())
        with self.bot.db:
            self.bot.db.execute('UPDATE verification SET expires=0 WHERE uid=2')
        self.click(self.row())
        self.assertIn('已失效', self.alerts()[-1])
        self.assertNotIn('approveChatJoinRequest', self.methods())

    def test_timeout_declines_and_clears_state(self):
        row = self.request()
        with patch('bot.time.time', return_value=row['expires'] + 1):
            self.bot.verification.tick()
        self.assertEqual(self.methods().count('declineChatJoinRequest'), 1)
        self.assertIsNone(self.row())
        self.assertTrue(self.bot.db.execute(
            "SELECT 1 FROM audit WHERE action='verification_timeout'").fetchone())

    def test_approval_failure_lets_the_member_apply_again(self):
        row = self.request()
        original = self.api.call

        def expired_approval(method, **data):
            if method == 'approveChatJoinRequest':
                raise APIError(method, {'error_code': 400,
                                        'description': 'Bad Request: HIDE_REQUESTER_MISSING'})
            return original(method, **data)

        with patch.object(self.api, 'call', side_effect=expired_approval):
            self.click(row)
        self.assertIsNone(self.row())
        self.assertIn('重新申请', self.alerts()[-1])
        self.assertTrue(self.bot.db.execute(
            "SELECT 1 FROM audit WHERE action='verification_approve_failed'").fetchone())

    # --- 直接入群路径 -----------------------------------------------------

    def test_direct_join_is_muted_until_verified(self):
        self.join(2)
        row = self.row()
        self.assertEqual((row['kind'], row['chat']), ('member', -1))
        self.assertEqual(self.last_card()['chat_id'], -1)
        restrictions = [data for method, data in self.api.calls if method == 'restrictChatMember']
        self.assertEqual(len(restrictions), 1)
        self.assertFalse(restrictions[0]['permissions']['can_send_messages'])
        self.click(row)
        restore = [data for method, data in self.api.calls if method == 'restrictChatMember'][-1]
        self.assertTrue(restore['permissions']['can_send_messages'])
        self.assertEqual(self.row()['state'], 'passed')
        self.assertEqual(self.methods().count('unbanChatMember'), 0)

    def test_direct_join_timeout_removes_the_member(self):
        self.join(2)
        row = self.row()
        with patch('bot.time.time', return_value=row['expires'] + 1):
            self.bot.verification.tick()
        kicks = [data for method, data in self.api.calls if method == 'unbanChatMember']
        self.assertEqual(len(kicks), 1)
        self.assertFalse(kicks[0]['only_if_banned'])
        self.assertIsNone(self.row())

    def test_duplicate_join_events_do_not_send_a_second_question(self):
        self.join(2)
        first = self.row()
        self.join(2)
        self.assertEqual(self.row()['nonce'], first['nonce'])
        self.assertEqual(self.methods().count('restrictChatMember'), 1)
        self.assertEqual(self.methods().count('sendMessage'), 1)

    def test_pending_member_messages_do_not_reach_business_logic(self):
        self.join(2)
        before = len(self.api.calls)
        self.bot.handle({'update_id': 60, 'message': {
            'chat': {'id': -1, 'type': 'supergroup'}, 'message_id': 60,
            'from': {'id': 2, 'first_name': 'A'}, 'text': '/checkin'}})
        self.assertEqual(len(self.api.calls), before)

    # --- 放行与重复入群 ---------------------------------------------------

    def test_administrators_are_admitted_without_a_question(self):
        self.api.members[2] = {'user': {'id': 2}, 'status': 'administrator'}
        self.join(2)
        self.assertIsNone(self.row())
        self.assertFalse([m for m in self.methods() if m in ('sendMessage', 'restrictChatMember')])

    def test_bots_are_only_admitted_when_an_administrator_invited_them(self):
        self.join(30, inviter=500, is_bot=True)
        self.assertEqual([data['user_id'] for method, data in self.api.calls
                          if method == 'banChatMember'], [30])
        self.assertIsNone(self.row(30))
        self.api.calls.clear()
        self.api.members[1] = {'user': {'id': 1}, 'status': 'creator'}
        self.join(31, inviter=1, is_bot=True)
        self.assertFalse([m for m in self.methods() if m in ('banChatMember', 'restrictChatMember')])
        self.assertIsNone(self.row(31))
        self.assertTrue(self.bot.db.execute(
            "SELECT 1 FROM audit WHERE action='allow_invited_bot'").fetchone())

    def test_verified_members_are_not_challenged_again_within_a_day(self):
        self.join(2)
        self.click(self.row())
        self.assertEqual(self.row()['state'], 'passed')
        sent_before = len(self.api.texts())
        self.leave(2)
        self.assertEqual(self.row()['state'], 'passed')
        self.join(2)
        self.assertEqual(len(self.api.texts()), sent_before)
        self.assertEqual(self.row()['state'], 'passed')
        expires = self.row()['expires']
        with patch('bot.time.time', return_value=expires + 1):
            self.bot.verification.tick()
        self.assertIsNone(self.row())
        self.join(2)
        self.assertEqual(self.row()['state'], 'challenge')

    # --- 投递与失败恢复 ---------------------------------------------------

    def test_transient_delivery_failure_is_retried(self):
        self.api.failure = 'sendMessage'
        row = self.request()
        self.assertEqual((row['sent'], row['send_attempts']), (0, 1))
        self.api.failure = None
        with patch('bot.time.time', return_value=row['retry_at']):
            self.bot.verification.tick()
        self.assertEqual(self.row()['sent'], 1)

    def test_permanent_delivery_failure_ends_the_request(self):
        self.request()
        original = self.api.call
        error = APIError('sendMessage', {'error_code': 403,
                                         'description': "Forbidden: bot can't initiate conversation"})

        def blocked(method, **data):
            if method == 'sendMessage':
                raise error
            return original(method, **data)

        with self.bot.db:
            self.bot.db.execute('UPDATE verification SET sent=0 WHERE uid=2')
        with patch.object(self.api, 'call', side_effect=blocked):
            self.bot.verification.tick()
        self.assertIsNone(self.row())
        self.assertEqual([data['user_id'] for method, data in self.api.calls
                          if method == 'declineChatJoinRequest'], [2])

    def test_conclusion_permission_failure_keeps_state_for_retry(self):
        row = self.request()
        original = self.api.call

        def forbidden(method, **data):
            if method == 'declineChatJoinRequest':
                raise APIError(method, {'error_code': 403, 'description': 'not enough rights'})
            return original(method, **data)

        with patch.object(self.api, 'call', side_effect=forbidden):
            with patch('bot.time.time', return_value=row['expires'] + 1):
                self.bot.verification.tick()
        self.assertIsNotNone(self.row())
        with patch('bot.time.time', return_value=self.row()['retry_at']):
            self.bot.verification.tick()
        self.assertIsNone(self.row())

    def test_settled_failures_do_not_retry(self):
        row = self.request()
        original = self.api.call

        def deactivated(method, **data):
            if method == 'declineChatJoinRequest':
                raise APIError(method, {'error_code': 403,
                                        'description': 'Forbidden: user is deactivated'})
            return original(method, **data)

        with patch.object(self.api, 'call', side_effect=deactivated):
            with patch('bot.time.time', return_value=row['expires'] + 1):
                self.bot.verification.tick()
        self.assertIsNone(self.row())

    def test_restore_failure_is_retried_in_maintenance(self):
        self.join(2)
        row = self.row()
        original = self.api.call

        def fail_restore(method, **data):
            if method == 'restrictChatMember':
                raise APIError(method, {'error_code': 500, 'description': 'boom'})
            return original(method, **data)

        with patch.object(self.api, 'call', side_effect=fail_restore):
            self.click(row)
        self.assertEqual((self.row()['restored'], self.row()['restore_attempts']), (0, 1))
        with patch('bot.time.time', return_value=self.row()['retry_at']):
            self.bot.verification.tick()
        self.assertEqual(self.row()['restored'], 1)

    def test_restore_failure_is_reported_honestly(self):
        self.join(2)
        row = self.row()
        original = self.api.call

        def fail_restore(method, **data):
            if method == 'restrictChatMember':
                raise APIError(method, {'error_code': 500, 'description': 'boom'})
            return original(method, **data)

        with patch.object(self.api, 'call', side_effect=fail_restore):
            self.click(row)
        self.assertEqual(self.row()['state'], 'passed')
        alert = self.alerts()[-1]
        self.assertIn('恢复发言失败', alert)
        self.assertNotIn('验证成功，欢迎入群', alert)
        edited = [data['text'] for method, data in self.api.calls if method == 'editMessageText'][-1]
        self.assertIn('正在自动重试', edited)

    def test_failed_conclusion_keeps_the_challenge_due(self):
        row = self.request()
        original = self.api.call

        def forbidden(method, **data):
            if method == 'declineChatJoinRequest':
                raise APIError(method, {'error_code': 403, 'description': 'not enough rights'})
            return original(method, **data)

        with patch.object(self.api, 'call', side_effect=forbidden):
            with patch('bot.time.time', return_value=row['expires'] + 1):
                self.bot.verification.tick()
        # 收尾失败也要标记失效，避免答错次数被无限累加。
        self.assertEqual(self.row()['expires'], 0)
        self.click(self.row())
        self.assertIn('已失效', self.alerts()[-1])

    def test_giving_up_on_permission_restore_is_audited(self):
        self.join(2)
        row = self.row()
        original = self.api.call

        def always_fails(method, **data):
            if method == 'restrictChatMember':
                raise APIError(method, {'error_code': 500, 'description': 'boom'})
            return original(method, **data)

        with patch.object(self.api, 'call', side_effect=always_fails):
            self.click(row)
            for _ in range(6):
                with patch('bot.time.time', return_value=self.row()['retry_at']):
                    self.bot.verification.tick()
        self.assertEqual(self.row()['restored'], 0)
        self.assertEqual(self.row()['restore_attempts'], 5)
        self.assertTrue(self.bot.db.execute(
            "SELECT 1 FROM audit WHERE action='verification_restore_gaveup'").fetchone())

    # --- 卡片清理 ---------------------------------------------------------

    def test_group_card_is_cleaned_after_verification_ends(self):
        self.join(2)
        self.assertEqual(self.bot.db.execute(
            'SELECT count(*) FROM verification_prompts').fetchone()[0], 1)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0)
        with patch('bot.time.time', return_value=2000):
            self.click(self.row())
            self.bot.sweep_deletions()
        queued = self.bot.db.execute('SELECT * FROM deletions').fetchone()
        self.assertEqual((queued['message_id'], queued['due']), (1, 2030))
        with patch('bot.time.time', return_value=2030):
            self.bot.sweep_deletions()
        self.assertIn(('deleteMessage', {'chat_id': -1, 'message_id': 1}), self.api.calls)

    def test_expired_card_is_cleaned_after_restart(self):
        self.join(2)
        expires = self.row()['expires']
        restarted = Bot(dict(CFG), self.api, self.bot.db)
        with patch('bot.time.time', return_value=expires + 1):
            restarted.sweep_deletions()
        queued = self.bot.db.execute('SELECT due FROM deletions WHERE message_id=1').fetchone()
        self.assertEqual(queued[0], expires + 1 + 30)

    def test_disabled_cleanup_keeps_cards(self):
        self.join(2)
        self.bot.delete_after = 0
        with patch('bot.time.time', return_value=int(time.time()) + 3600):
            self.bot.sweep_deletions()
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0)

    def test_private_application_card_has_one_channel_button_and_four_answers(self):
        self.request()
        self.assertEqual(len(self.api.texts()), 1)
        self.assertEqual(len(self.buttons()), 5)

class SchemaTests(unittest.TestCase):
    """不做迁移、不做兼容：旧结构直接拒绝启动，新库按当前结构创建。"""

    OLD_SCHEMA = """
    CREATE TABLE verification(uid INTEGER PRIMARY KEY, chat INTEGER, kind TEXT,
      nonce TEXT, answer INTEGER, expires INTEGER, attempts INTEGER DEFAULT 0,
      passed INTEGER DEFAULT 0);
    """

    def test_previous_schema_is_rejected_instead_of_migrated(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript(self.OLD_SCHEMA)
        with self.assertRaises(RuntimeError) as caught:
            Bot(dict(CFG), FakeAPI(), db)
        self.assertIn('incompatible schema', str(caught.exception))
        self.assertIn('Drop verification', str(caught.exception))
        columns = {row[1] for row in db.execute('PRAGMA table_info(verification)')}
        self.assertEqual(columns, {'uid', 'chat', 'kind', 'nonce', 'answer', 'expires',
                                   'attempts', 'passed'})

    def test_fresh_database_creates_the_current_schema(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        api = FakeAPI()
        bot = Bot(dict(CFG), api, db)
        columns = {row['name'] for row in db.execute('PRAGMA table_info(verification)')}
        self.assertEqual(columns, set(bot.verification.REQUIRED_COLUMNS))


if __name__ == '__main__':
    unittest.main()
