# coding: utf-8
"""消息保留时长：群默认时长、显式类别时长和关闭清理时的行为。"""
import sqlite3
import unittest
from unittest.mock import patch

from bot import Bot
from retention import NOTICE_SECONDS
from test_bot import FakeAPI


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2', 'DELETE_AFTER_SECONDS': '45'},
                       self.api, sqlite3.connect(':memory:'))
        self.addCleanup(self.bot.db.close)

    def due(self):
        return {row['message_id']: row['due']
                for row in self.bot.db.execute('SELECT * FROM deletions')}

    def test_group_default_follows_configuration(self):
        with patch('bot.time.time', return_value=100):
            sent = self.bot.send(self.bot.group, '普通成功提示')
        self.assertEqual(self.due()[sent['message_id']], 145)

    def test_wording_never_changes_the_lifetime(self):
        # 不按文案关键词猜类别：同一句话在成功与失败路径上由调用方声明时长。
        with patch('bot.time.time', return_value=100):
            default = self.bot.send(self.bot.group, '暂时无法处理，请稍后重试。')
            explicit = self.bot.send(self.bot.group, '同样的文案',
                                     _delete_after=self.bot.notice_seconds())
        due = self.due()
        self.assertEqual(due[default['message_id']], 145)
        self.assertEqual(due[explicit['message_id']], 100 + NOTICE_SECONDS)

    def test_explicit_duration_wins_over_keep(self):
        with patch('bot.time.time', return_value=100):
            sent = self.bot.send(self.bot.group, '交互卡片', keep=True, _delete_after=600)
        self.assertEqual(self.due()[sent['message_id']], 700)

    def test_notice_duration_is_disabled_with_group_cleanup(self):
        self.bot.delete_after = 0
        self.assertEqual(self.bot.notice_seconds(), 0)
        with patch('bot.time.time', return_value=100):
            self.bot.send(self.bot.group, '失败提示', _delete_after=self.bot.notice_seconds())
        self.assertEqual(self.due(), {})

    def test_failure_notice_outlives_success_notice(self):
        self.assertGreater(NOTICE_SECONDS, 30)


if __name__ == '__main__':
    unittest.main()
