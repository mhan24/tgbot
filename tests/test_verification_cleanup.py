import sqlite3
import unittest
from unittest.mock import patch

from bot import Bot
from test_bot import FakeAPI


class VerificationCleanupTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.db = sqlite3.connect(':memory:')
        self.cfg = {'GROUP_ID': '-20', 'CHANNEL_ID': '-2',
                    'DELETE_AFTER_SECONDS': '30', 'VERIFY_SECONDS': '240'}
        self.bot = Bot(self.cfg, self.api, self.db)
        with patch('bot.time.time', return_value=1000):
            self.bot.start_verification({'id': 2}, 'member', -20)
        self.row = self.db.execute('SELECT * FROM verification').fetchone()
        self.mid = self.api.sent

    def click(self, answer):
        self.bot.callback({'id': 'q', 'from': {'id': 2},
                           'data': f'v:2:{self.row["nonce"]}:{answer}',
                           'message': {'chat': {'id': -20}, 'message_id': self.mid}})

    def assert_deleted_after_30_seconds(self, ended):
        with patch('bot.time.time', return_value=ended):
            self.bot.sweep_deletions()
        queued = self.db.execute('SELECT * FROM deletions').fetchone()
        self.assertEqual((queued['chat'], queued['message_id'], queued['due']),
                         (-20, self.mid, ended + 30))
        with patch('bot.time.time', return_value=ended + 29):
            self.bot.sweep_deletions()
        self.assertFalse(any(m == 'deleteMessage' for m, _ in self.api.calls))
        with patch('bot.time.time', return_value=ended + 30):
            self.bot.sweep_deletions()
        self.assertIn(('deleteMessage', {'chat_id': -20, 'message_id': self.mid}), self.api.calls)

    def test_success_in_second_group_starts_cleanup_timer(self):
        with patch('bot.time.time', return_value=1010):
            self.click(self.row['answer'])
        self.assert_deleted_after_30_seconds(1010)

    def test_expired_challenge_is_cleaned_after_restart(self):
        self.bot = Bot(self.cfg, self.api, self.db)
        with patch('bot.time.time', return_value=1239):
            self.bot.sweep_deletions()
        self.assertIsNone(self.db.execute('SELECT 1 FROM deletions').fetchone())
        self.assertTrue(self.bot.pending_deletions())
        self.assert_deleted_after_30_seconds(1240)

    def test_three_wrong_answers_end_retention_early(self):
        with patch('bot.time.time', return_value=1010):
            for _ in range(3):
                self.click(-1)
        self.assert_deleted_after_30_seconds(1010)

    def test_active_prompt_and_disabled_cleanup_are_retained(self):
        with patch('bot.time.time', return_value=1031):
            self.bot.sweep_deletions()
        self.assertIsNone(self.db.execute('SELECT 1 FROM deletions').fetchone())
        self.bot.delete_after = 0
        with patch('bot.time.time', return_value=1300):
            self.bot.sweep_deletions()
        self.assertIsNone(self.db.execute('SELECT 1 FROM deletions').fetchone())
