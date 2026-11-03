import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bot import Bot, APIError
from points import Points
from test_bot import FakeAPI


class InvitesTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api, self.db)
        self.bot.bot_id = 99
        self.user = {'id': 2, 'first_name': 'Inviter'}
        self.addCleanup(self.db.close)

    def arrival(self, uid=10, **extra):
        event = {'chat': {'id': -1}, 'date': 100000, 'from': self.user,
                 'old_chat_member': {'status': 'left', 'user': {'id': uid}},
                 'new_chat_member': {'status': 'member', 'user': {'id': uid}}}
        event.update(extra)
        return {'update_id': uid, 'chat_member': event}

    def test_cap_persists_across_restart_reset_and_midnight(self):
        before = datetime(2026, 10, 30, 15, 59, 59, tzinfo=timezone.utc).timestamp()
        for uid in range(10, 21):
            self.bot.points.invite(self.user, uid, before)
        self.assertEqual(self.bot.points.stats(2, before)[0], 10)
        points = Points(self.db)
        self.assertEqual(points.invite(self.user, 20, before + 1), (0, 'replay'))
        self.assertEqual(points.invite(self.user, 21, before + 1), (1, 'added'))
        self.assertEqual(points.invite_daily(2, before + 1), 1)
        points.reset(2)
        self.assertEqual(points.invite(self.user, 21, before + 1), (0, 'replay'))
        self.assertEqual(points.invite_daily(2, before + 1), 1)

    def test_direct_join_duplicate_service_event_and_rejoin(self):
        event = self.arrival()
        self.bot.handle(event)
        self.bot.handle(event)
        self.bot.handle({'update_id': 80, 'message': {'chat': {'id': -1}, 'message_id': 80,
                         'from': self.user, 'new_chat_members': [{'id': 10}]}})
        self.bot.handle(self.arrival(**{'old_chat_member': {'status': 'kicked'}}))
        self.assertEqual(self.bot.points.stats(2)[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM invite_arrivals').fetchone()[0], 1)

    def test_shared_links_approvals_self_and_bots_are_not_rewarded(self):
        for uid, extra in enumerate(({'invite_link': {'creator': self.user}},
                                      {'via_join_request': True},
                                      {'via_chat_folder_invite_link': True},
                                      {'from': {'id': 13}},
                                      {'from': {'id': 99, 'is_bot': True}}), 10):
            self.bot.handle(self.arrival(uid, **extra))
        self.bot.handle(self.arrival(20, new_chat_member={'status': 'member', 'user': {'id': 20, 'is_bot': True}}))
        self.assertEqual(self.bot.points.stats(2)[0], 0)
        # A later direct rejoin cannot turn an unattributed arrival into a reward.
        self.bot.handle(self.arrival(10))
        self.assertEqual(self.bot.points.stats(2)[0], 0)

    def test_lookup_failure_is_retryable_without_consuming_claim(self):
        self.api.failure = 'getChatMember'
        with self.assertRaises(APIError):
            self.bot.handle(self.arrival())
        self.assertEqual(self.db.execute('SELECT count(*) FROM invite_arrivals').fetchone()[0], 0)
        self.api.failure = None
        self.bot.handle(self.arrival())
        self.assertEqual(self.bot.points.stats(2)[0], 1)

    def test_nonmember_or_blacklisted_inviter_not_rewarded(self):
        self.api.members[2] = {'status': 'left'}
        self.bot.handle(self.arrival())
        self.api.members[2] = {'status': 'member'}
        with patch.object(self.bot, 'blacklisted', side_effect=lambda uid: uid == 2):
            self.bot.handle(self.arrival(11))
        self.assertEqual(self.bot.points.stats(2)[0], 0)

    def test_transaction_rolls_back_claim_when_ledger_write_fails(self):
        self.db.execute("CREATE TRIGGER fail_invite BEFORE INSERT ON point_events BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.bot.points.invite(self.user, 10)
        self.assertEqual(self.db.execute('SELECT count(*) FROM invite_arrivals').fetchone()[0], 0)
        self.assertEqual(self.bot.points.stats(2)[0], 0)

    def test_current_points_storage_and_group_isolation(self):
        other = sqlite3.connect(':memory:')
        self.addCleanup(other.close)
        other.execute('CREATE TABLE point_users(uid INTEGER PRIMARY KEY,name TEXT,total INTEGER NOT NULL DEFAULT 0)')
        other.execute("INSERT INTO point_users VALUES(2,'Existing',7)")
        other.commit()
        points = Points(other)
        Points(other)
        self.bot.points.invite(self.user, 10)
        points.invite(self.user, 10)
        self.assertEqual(points.stats(2)[0], 8)
        self.assertEqual(self.bot.points.stats(2)[0], 1)
        self.bot.handle(self.arrival(11, chat={'id': -3}))
        self.assertEqual(self.bot.points.stats(2)[0], 1)

    def test_private_points_shows_invites_without_expiring_result(self):
        self.bot.points.invite(self.user, 10)
        self.bot.points_command({'chat': {'id': 2, 'type': 'private'}, 'from': self.user,
                                 'message_id': 1, 'text': '/points'})
        text = [d['text'] for m, d in self.api.calls if m == 'sendMessage'][-1]
        self.assertIn('今日邀请积分：1/10', text)
        self.assertEqual(self.db.execute('SELECT count(*) FROM deletions').fetchone()[0], 0)
