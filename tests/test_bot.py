import sqlite3
import time
import unittest
from unittest.mock import patch
from bot import Bot, APIError


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.subscribed = True
        self.failure = None
        self.members = {}
        self.sent = 0
        self.photos = {'total_count': 1, 'photos': [[{'file_id': 'test'}]]}

    def call(self, method, **data):
        self.calls.append((method, data))
        if self.failure == method:
            raise APIError(method, {'error_code': 500, 'description': 'temporary failure'})
        if method == 'sendMessage':
            self.sent += 1
            return {'message_id': self.sent}
        if method == 'setChatMemberTag':
            self.members.setdefault(data['user_id'], {'user': {'id': data['user_id']}, 'status': 'member'})['tag'] = data['tag']
            return True
        if method == 'unbanChatMember':
            return True
        if method == 'getUserProfilePhotos':
            return self.photos
        if method == 'getChatAdministrators':
            return [m for m in self.members.values() if m['status'] == 'administrator']
        if method == 'getChatMember':
            if data['user_id'] in self.members:
                return self.members[data['user_id']]
            if data['chat_id'] == -2:
                return {'status': 'member' if self.subscribed else 'left'}
            return {'status': 'administrator' if data['user_id'] == 1 else 'member'}
        if method == 'getChat':
            return {'permissions': {'can_send_messages': True, 'can_send_photos': True, 'can_invite_users': False}}
        return True


class Tests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api, sqlite3.connect(':memory:'))
        self.bot.bot_id = 99
        self.bot.username = 'testbot'

    def verify(self):
        self.bot.start_verification({'id': 2, 'is_bot': False}, 'request', 2)
        return self.bot.db.execute('SELECT * FROM verification WHERE uid=2').fetchone()

    def click(self, row, uid=2, answer=None):
        self.bot.callback({'id': 'q', 'from': {'id': uid}, 'data': f'v:2:{row["nonce"]}:{row["answer"] if answer is None else answer}'})

    def test_subscription_required(self):
        row = self.verify()
        self.api.subscribed = False
        self.click(row)
        self.assertFalse(any(m == 'approveChatJoinRequest' for m, _ in self.api.calls))
        self.api.subscribed = True
        self.click(row)
        self.assertTrue(any(m == 'approveChatJoinRequest' for m, _ in self.api.calls))

    def test_cannot_answer_for_another_user(self):
        self.click(self.verify(), uid=3)
        self.assertFalse(any(m == 'approveChatJoinRequest' for m, _ in self.api.calls))

    def test_three_wrong_answers_decline(self):
        row = self.verify()
        for _ in range(3):
            self.click(row, answer=-1)
        self.assertTrue(any(m == 'declineChatJoinRequest' for m, _ in self.api.calls))

    def test_timeout_declines(self):
        self.verify()
        self.bot.db.execute('UPDATE verification SET expires=0')
        self.bot.expire()
        self.assertTrue(any(m == 'declineChatJoinRequest' for m, _ in self.api.calls))

    def test_only_third_warning_mutes_and_deduplicates(self):
        for i in range(2):
            self.bot.warn(2, str(i), 'test')
        self.assertFalse(any(m == 'restrictChatMember' for m, _ in self.api.calls))
        self.bot.warn(2, '2', 'test')
        self.bot.warn(2, '2', 'test')
        self.assertEqual(self.bot.db.execute('SELECT count FROM warnings').fetchone()[0], 3)
        self.assertEqual(sum(m == 'restrictChatMember' for m, _ in self.api.calls), 1)

    def test_warning_failure_retry_does_not_double_count(self):
        self.api.failure = 'sendMessage'
        with self.assertRaises(APIError):
            self.bot.warn(2, 'same', 'test')
        self.api.failure = None
        self.bot.warn(2, 'same', 'test')
        self.assertEqual(self.bot.db.execute('SELECT count FROM warnings').fetchone()[0], 1)

    def test_non_admin_cannot_mute(self):
        self.bot.command({'text': '/mute 3', 'from': {'id': 2}}, 10)
        self.assertFalse(any(m == 'restrictChatMember' for m, _ in self.api.calls))

    def test_admin_can_mute_but_not_admin_target(self):
        self.bot.command({'text': '/mute 2 60', 'from': {'id': 1}}, 10)
        restrict = [d for m, d in self.api.calls if m == 'restrictChatMember']
        self.assertEqual(len(restrict), 1)
        self.assertAlmostEqual(restrict[0]['until_date'], time.time() + 3600, delta=3)
        self.bot.command({'text': '/mute 1', 'from': {'id': 1}}, 11)
        self.assertEqual(sum(m == 'restrictChatMember' for m, _ in self.api.calls), 1)

    def test_direct_join_restricts_and_approval_not_challenged_twice(self):
        self.bot.joined({'id': 2})
        self.assertTrue(any(m == 'restrictChatMember' for m, _ in self.api.calls))
        self.bot.db.execute('DELETE FROM verification')
        self.click(self.verify())
        before = len(self.api.calls)
        self.bot.joined({'id': 2})
        self.assertEqual(before, len(self.api.calls))

    def test_restore_preserves_existing_mute_and_default_permissions(self):
        self.bot.db.execute('INSERT INTO warnings VALUES(2,3,?)', (int(time.time()) + 1000,))
        self.bot.restore(2)
        self.assertFalse(self.api.calls[-1][1]['permissions']['can_send_messages'])
        self.bot.db.execute('UPDATE warnings SET mute_until=0')
        self.bot.restore(2)
        self.assertTrue(self.api.calls[-1][1]['permissions']['can_send_messages'])
        self.assertFalse(self.api.calls[-1][1]['permissions']['can_invite_users'])

    def test_caption_edits_do_not_trigger_automatic_moderation(self):
        self.bot.handle({'update_id': 1, 'edited_message': {'chat': {'id': -1}, 'message_id': 10, 'from': {'id': 2}, 'caption': '日赚1000，私聊我'}})
        self.assertFalse(any(m == 'deleteMessage' for m, _ in self.api.calls))
        self.assertIsNone(self.bot.db.execute('SELECT 1 FROM warnings').fetchone())

    def test_bio_keywords_no_longer_reject_admission(self):
        self.bot.handle({'update_id': 12, 'chat_join_request': {'chat': {'id': -1}, 'from': {'id': 2}, 'user_chat_id': 2, 'bio': '日赚1000，私聊我'}})
        self.assertFalse(any(m == 'declineChatJoinRequest' for m, _ in self.api.calls))
        self.assertFalse(any(m == 'approveChatJoinRequest' for m, _ in self.api.calls))

    def test_channel_posts_are_not_automatically_classified(self):
        for i in range(3):
            self.bot.handle({'update_id': 30+i, 'message': {'chat': {'id': -1}, 'message_id': 30+i, 'from': {'id': 777000}, 'sender_chat': {'id': -55}, 'text': '日赚1000，私聊我'}})
        self.assertEqual(sum(m == 'banChatSenderChat' for m, _ in self.api.calls), 0)

    def test_third_warning_kicks_and_blacklists(self):
        for i in range(3):
            self.bot.warn(2, str(i), 'test')
        self.assertEqual([d['user_id'] for m, d in self.api.calls if m == 'banChatMember'], [2])
        self.assertTrue(self.bot.blacklisted(2))
        self.assertFalse(self.bot.blacklisted(3))

    def test_ban_command_removes_and_blacklists(self):
        msg = {'chat': {'id': -1}, 'text': '/ban 广告', 'from': {'id': 1},
               'reply_to_message': {'from': {'id': 2, 'is_bot': False}}}
        self.bot.command(msg, 1)
        self.assertEqual(self.bot.db.execute('SELECT uid FROM blacklist').fetchone()[0], 2)
        self.assertEqual([d['user_id'] for m, d in self.api.calls if m == 'banChatMember'], [2])

    def test_ban_refuses_admin_and_self(self):
        for target in (self.bot.bot_id, 1):
            msg = {'chat': {'id': -1}, 'text': '/ban', 'from': {'id': 1},
                   'reply_to_message': {'from': {'id': target, 'is_bot': False}}}
            self.bot.command(msg, 1)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM blacklist').fetchone()[0], 0)
        self.assertFalse(any(m == 'banChatMember' for m, _ in self.api.calls))

    def test_blacklisted_join_request_declined(self):
        with self.bot.db:
            self.bot.db.execute("INSERT INTO blacklist(uid,at,actor,reason) VALUES(2,0,0,'t')")
        self.bot.handle({'update_id': 1, 'chat_join_request': {'chat': {'id': -1}, 'from': {'id': 2}, 'user_chat_id': 2}})
        self.assertEqual([d['user_id'] for m, d in self.api.calls if m == 'declineChatJoinRequest'], [2])

    def test_blacklisted_member_rejoin_is_kicked(self):
        with self.bot.db:
            self.bot.db.execute("INSERT INTO blacklist(uid,at,actor,reason) VALUES(2,0,0,'t')")
        self.bot.handle({'update_id': 1, 'chat_member': {'chat': {'id': -1},
            'old_chat_member': {'user': {'id': 2}, 'status': 'left'},
            'new_chat_member': {'user': {'id': 2, 'is_bot': False}, 'status': 'member'}}})
        self.assertEqual([d['user_id'] for m, d in self.api.calls if m == 'banChatMember'], [2])

    def test_unban_allows_rejoin(self):
        with self.bot.db:
            self.bot.db.execute("INSERT INTO blacklist(uid,at,actor,reason) VALUES(2,0,0,'t')")
        msg = {'chat': {'id': -1}, 'text': '/unban', 'from': {'id': 1},
               'reply_to_message': {'from': {'id': 2, 'is_bot': False}}}
        self.bot.command(msg, 1)
        self.assertFalse(self.bot.blacklisted(2))
        self.assertEqual([d['user_id'] for m, d in self.api.calls if m == 'unbanChatMember'], [2])

    def op(self, mid, text='/rank', uid=2):
        return {'update_id': mid, 'message': {'chat': {'id': self.bot.group, 'type': 'supergroup'},
                'message_id': mid, 'from': {'id': uid, 'first_name': 'U'}, 'text': text}}

    def group_sends(self):
        return [d['text'] for m, d in self.api.calls if m == 'sendMessage' and d['chat_id'] == self.bot.group]

    def test_three_group_operations_allowed_then_redirected(self):
        for i in range(1, 4):
            self.bot.handle(self.op(i))
        self.assertEqual(self.bot.db.execute('SELECT count FROM command_usage').fetchone()[0], 3)
        self.api.calls.clear()
        self.bot.handle(self.op(4, '/bin 45717360'))
        self.assertTrue(any('请私聊机器人' in t for t in self.group_sends()))
        self.api.calls.clear()
        self.bot.handle(self.op(5, '/rank'))
        self.assertEqual(self.group_sends(), [])

    def test_admin_is_exempt_from_group_limit(self):
        for i in range(1, 6):
            self.bot.handle(self.op(i, '/rank', uid=1))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM command_usage').fetchone()[0], 0)

    def test_plain_chat_is_not_counted(self):
        self.bot.handle(self.op(1, '普通聊天'))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM command_usage').fetchone()[0], 0)

    def test_other_bot_commands_are_not_group_operations(self):
        self.assertFalse(self.bot.is_bot_operation({'text': '/rank@otherbot'}))
        self.assertFalse(self.bot.is_bot_operation({'text': '/ai@otherbot 你好'}))
        self.assertTrue(self.bot.is_bot_operation({'text': '/rank@testbot'}))
        self.assertTrue(self.bot.is_bot_operation({'text': '/ai@testbot 你好'}))

    def test_limit_is_per_day(self):
        msg = {'chat': {'id': self.bot.group, 'type': 'supergroup'}, 'message_id': 1,
               'from': {'id': 2, 'first_name': 'U'}, 'text': '/rank'}
        for _ in range(3):
            self.assertTrue(self.bot.group_command_allowed(msg))
        self.assertFalse(self.bot.group_command_allowed(msg))
        with self.bot.db:
            self.bot.db.execute('UPDATE command_usage SET day=?', ('2000-01-01',))
        self.assertTrue(self.bot.group_command_allowed(msg))

    def test_private_chat_is_not_limited(self):
        for i in range(1, 6):
            self.bot.handle({'update_id': i, 'message': {'chat': {'id': 2, 'type': 'private'},
                            'message_id': i, 'from': {'id': 2, 'first_name': 'U'}, 'text': '/rank'}})
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM command_usage').fetchone()[0], 0)

    def test_group_messages_are_queued_private_are_not(self):
        self.bot.send(self.bot.group, 'group hello')
        rows = self.bot.db.execute('SELECT * FROM deletions').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['message_id'], 1)
        self.assertGreater(rows[0]['due'], int(time.time()) + 25)
        self.bot.send(2, 'private hello')
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 1)

    def test_second_group_queues_and_deletes_its_own_bot_messages(self):
        second = Bot({'GROUP_ID': '-2', 'CHANNEL_ID': '-3', 'DELETE_AFTER_SECONDS': '30'},
                     self.api, sqlite3.connect(':memory:'))
        second.send(second.group, 'second group bot reply')
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0)
        row = second.db.execute('SELECT chat,message_id,due FROM deletions').fetchone()
        self.assertEqual((row['chat'], row['message_id']), (-2, self.api.sent))
        self.assertGreater(row['due'], int(time.time()) + 25)
        with second.db:
            second.db.execute('UPDATE deletions SET due=0')
        second.sweep_deletions()
        self.assertIn(('deleteMessage', {'chat_id': -2, 'message_id': self.api.sent}), self.api.calls)

    def test_sweep_removes_due_messages_only(self):
        self.bot.send(self.bot.group, 'first')
        self.bot.schedule_delete(self.bot.group, 99)
        with self.bot.db:
            self.bot.db.execute('UPDATE deletions SET due=0 WHERE message_id=1')
            self.bot.db.execute('UPDATE deletions SET due=? WHERE message_id=99', (int(time.time()) + 3600,))
        self.bot.sweep_deletions()
        deleted = [d['message_id'] for m, d in self.api.calls if m == 'deleteMessage']
        self.assertEqual(deleted, [1])
        self.assertEqual([r['message_id'] for r in self.bot.db.execute('SELECT message_id FROM deletions')], [99])

    def test_other_bot_group_message_is_queued(self):
        self.bot.handle({'update_id': 1, 'message': {
            'chat': {'id': self.bot.group, 'type': 'supergroup'}, 'message_id': 77,
            'from': {'id': 500, 'is_bot': True, 'first_name': 'Other'}, 'text': 'bot says hi'}})
        self.assertEqual([r['message_id'] for r in self.bot.db.execute('SELECT message_id FROM deletions')], [77])

    def test_channel_identity_posts_are_never_queued_for_deletion(self):
        # Telegram may provide a bot-like from field for channel identity posts.
        for cid in (self.bot.group, self.bot.channel, -999999):
            with self.bot.db:
                self.bot.db.execute('DELETE FROM deletions')
            self.bot.handle({'update_id': cid & 0xffff, 'message': {
                'chat': {'id': self.bot.group, 'type': 'supergroup'}, 'message_id': abs(cid) % 1000 + 1,
                'sender_chat': {'id': cid, 'type': 'channel', 'title': 'Channel'},
                'from': {'id': cid, 'is_bot': True, 'first_name': 'Channel'},
                'text': '频道身份的普通内容'}})
            self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0, cid)

    def test_real_bot_message_still_queued_with_sender_chat_absent(self):
        self.bot.handle({'update_id': 1, 'message': {
            'chat': {'id': self.bot.group, 'type': 'supergroup'}, 'message_id': 42,
            'from': {'id': 555, 'is_bot': True, 'first_name': 'OtherBot'}, 'text': 'bot'}})
        self.assertEqual([r['message_id'] for r in self.bot.db.execute('SELECT message_id FROM deletions')], [42])

    def test_human_group_message_is_not_queued(self):
        self.bot.handle({'update_id': 1, 'message': {
            'chat': {'id': self.bot.group, 'type': 'supergroup'}, 'message_id': 78,
            'from': {'id': 2, 'is_bot': False, 'first_name': 'Human'}, 'text': 'hello'}})
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0)

    def test_undeletable_message_is_dropped(self):
        self.bot.schedule_delete(self.bot.group, 7)
        with self.bot.db:
            self.bot.db.execute('UPDATE deletions SET due=0')
        with patch.object(self.api, 'call', side_effect=APIError('deleteMessage', {'error_code': 400, 'description': 'message to delete not found'})):
            self.bot.sweep_deletions()
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0)

    def test_transient_delete_failure_is_retried_then_dropped(self):
        self.bot.schedule_delete(self.bot.group, 7)
        self.api.failure = 'deleteMessage'
        for _ in range(3):
            with self.bot.db:
                self.bot.db.execute('UPDATE deletions SET due=0')
            self.bot.sweep_deletions()
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0)

    def test_interactive_messages_are_exempt(self):
        self.bot.send(self.bot.group, 'kept buttons', keep=True)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0)
        row = self.verify()
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0)

    def test_deletion_can_be_disabled(self):
        self.bot.delete_after = 0
        self.bot.send(self.bot.group, 'kept')
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0)

    def test_help_lists_everyone_and_admin_features(self):
        self.bot.handle({'update_id': 1, 'message': {'chat': {'id': -1, 'type': 'supergroup'}, 'from': {'id': 2}, 'message_id': 5, 'text': '/help'}})
        text = self.api.calls[-1][1]['text']
        self.assertIn('全员可用', text)
        self.assertIn('仅管理员', text)
        self.assertNotIn('/menu', text)
        self.assertNotIn('/cleaninactive', text)
        for removed in ('原神启动', '/blockedwords', '/flagged', '/unflag'):
            self.assertNotIn(removed, text)
        for token in ('/checkin', '/points', '/rank', '/bin', '/ai', '/warn', '/mute', '/airdrop', '/status'):
            self.assertIn(token, text)
        self.assertEqual(self.api.calls[-1][1]['parse_mode'], 'HTML')

    def test_help_available_in_private_chat(self):
        self.bot.handle({'update_id': 2, 'message': {'chat': {'id': 2, 'type': 'private'}, 'from': {'id': 2}, 'message_id': 6, 'text': '/help'}})
        self.assertIn('功能菜单', self.api.calls[-1][1]['text'])

    def test_help_is_counted_as_a_group_operation(self):
        self.assertTrue(self.bot.is_bot_operation({'text': '/help'}))
        self.assertTrue(self.bot.is_bot_operation({'text': '/help@testbot'}))
        self.assertFalse(self.bot.is_bot_operation({'text': '/menu'}))

    def test_removed_admin_commands_are_not_registered(self):
        for command in ('/flagged', '/unflag 2'):
            with self.subTest(command=command):
                self.assertFalse(self.bot.command({'text': command, 'from': {'id': 1}}, 1))



if __name__ == '__main__':
    unittest.main()


class BanPurgeTests(unittest.TestCase):
    def test_purge_parameter_and_context_only_after_success(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,sqlite3.connect(':memory:'))
        bot.history.record({'chat':{'id':-1},'message_id':42,'from':{'id':2},'text':'history'})
        bot.history.record({'chat':{'id':-1},'message_id':43,'from':{'id':3},'text':'keep'})
        api.failure='banChatMember'
        with self.assertRaises(APIError):bot.blacklist_member(2)
        self.assertEqual(bot.db.execute('SELECT count(*) FROM chat_history').fetchone()[0],2)
        api.failure=None;bot.blacklist_member(2)
        bans=[d for m,d in api.calls if m=='banChatMember']
        self.assertTrue(all(d['revoke_messages'] is True for d in bans))
        self.assertEqual([r['uid'] for r in bot.db.execute('SELECT uid FROM chat_history')],[3])

    def test_warning_ban_revokes_messages(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,sqlite3.connect(':memory:'))
        for i in range(3):bot.warn(2,str(i),'spam')
        bans=[d for m,d in api.calls if m=='banChatMember']
        self.assertEqual(len(bans),1)
        self.assertTrue(bans[0]['revoke_messages'])
