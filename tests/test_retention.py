# coding: utf-8
"""消息保留：机器人不自动删除任何消息，也没有删除队列与清理时长配置。"""
import sqlite3
import time
import unittest
from unittest.mock import patch

from bot import Bot
from test_bot import FakeAPI


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api,
                       sqlite3.connect(':memory:'))
        self.bot.bot_id = 99
        self.bot.username = 'testbot'
        self.addCleanup(self.bot.db.close)

    def deleted(self):
        return [data for method, data in self.api.calls if method == 'deleteMessage']

    def tables(self):
        return {row[0] for row in self.bot.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def test_group_success_and_failure_notices_stay(self):
        for text in ('普通提示', '尚未提供这个命令，发送 /help 查看可用功能。'):
            self.bot.send(self.bot.group, text)
        with patch('bot.time.time', return_value=time.time() + 30 * 86400):
            self.bot.maintenance()
        self.assertEqual(self.deleted(), [])

    def test_group_and_private_replies_share_one_send_path(self):
        self.bot.send(self.bot.group, '群内提示')
        self.bot.send(2, '私聊提示')
        chats = [data['chat_id'] for method, data in self.api.calls if method == 'sendMessage']
        self.assertEqual(chats, [self.bot.group, 2])
        self.assertEqual(self.deleted(), [])

    def test_verification_card_is_sent_without_cleanup_booking(self):
        self.bot.verification.admit({'id': 7, 'first_name': 'P'}, 'member', self.bot.group)
        self.assertIn('sendMessage', self.api.methods())
        with patch('bot.time.time', return_value=time.time() + 30 * 86400):
            self.bot.maintenance()
        self.assertEqual(self.deleted(), [])

    def test_cleanup_configuration_is_no_longer_read(self):
        # DELETE_AFTER_SECONDS 已移除：旧配置既不让消息被删，也不会阻止发送。
        bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2', 'DELETE_AFTER_SECONDS': '0'},
                  FakeAPI(), sqlite3.connect(':memory:'))
        self.addCleanup(bot.db.close)
        self.assertFalse(hasattr(bot, 'delete_after'))
        bot.send(bot.group, '提示')
        self.assertNotIn('deletions', {row[0] for row in bot.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")})

    def test_no_deletion_or_prompt_tables_are_created(self):
        self.assertNotIn('deletions', self.tables())
        self.assertNotIn('verification_prompts', self.tables())
        self.assertFalse(hasattr(self.bot, 'schedule_delete'))
        self.assertFalse(hasattr(self.bot, 'sweep_deletions'))

    def test_only_punishment_revokes_member_messages(self):
        # 唯一会动消息的仍是处罚：移出成员时按 Telegram 语义撤回其发言。
        self.bot.call('banChatMember', chat_id=self.bot.group, user_id=5)
        bans = [data for method, data in self.api.calls if method == 'banChatMember']
        self.assertTrue(bans[0]['revoke_messages'])
        self.assertEqual(self.deleted(), [])


if __name__ == '__main__':
    unittest.main()
