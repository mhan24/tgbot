import sqlite3
import time
import unittest
from unittest.mock import patch
from bot import Bot
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api, sqlite3.connect(':memory:'))
        self.bot.bot_id = 99
        self.bot.username = 'testbot'
        self.bot.can_manage_tags = True
        self.a = self.bot.attention
        # deployment far in the past, then drop the startup gap the first tick records
        with self.bot.db:
            self.bot.db.execute("UPDATE meta SET value=? WHERE key='activity_started'", (str(time.time() - 40 * 86400),))
            self.bot.db.execute("INSERT OR REPLACE INTO meta VALUES('activity_heartbeat',?)", (str(time.time()),))
            self.bot.db.execute('DELETE FROM activity_gaps')

    def days(self):
        today = self.bot.points.day()
        from datetime import datetime, timedelta
        from points import CST
        base = datetime.fromisoformat(today)
        return [(base - timedelta(days=n)).date().isoformat() for n in (1, 2, 3)]

    def seed(self, checkins, speaks=(), total=60):
        d1, d2, d3 = self.days()
        with self.bot.db:
            self.bot.db.execute('INSERT OR REPLACE INTO point_users VALUES(?,?,?)', (2, 'Alice', total))
            for day in checkins:
                self.bot.db.execute('INSERT OR REPLACE INTO point_events VALUES(?,?,?,?,?)', (f'c:{day}', 2, day, 'checkin', 3))
            for day in speaks:
                self.bot.db.execute('INSERT OR IGNORE INTO activity_days VALUES(?,?)', (2, day))

    def tags(self):
        return [d.get('tag') for m, d in self.api.calls if m == 'setChatMemberTag']

    def total(self):
        return self.bot.points.stats(2)[0]

    def bans(self):
        return [d for m,d in self.api.calls if m == 'banChatMember']

    def test_three_days_ban_without_tag_permission(self):
        self.seed(self.days())
        self.bot.can_manage_tags = False
        self.a.tick(force=True)
        self.assertTrue(self.bot.blacklisted(2))
        self.assertEqual(len(self.bans()), 1)
        self.assertTrue(self.bans()[0]['revoke_messages'])
        self.assertEqual(self.tags(), [])
        self.assertEqual(self.total(), 0)
        self.a.tick(force=True)
        self.assertEqual(len(self.bans()), 1)

    def test_two_days_never_notify_or_ban(self):
        self.seed(self.days()[:2])
        self.a.tick(force=True)
        self.assertEqual(self.bans(), [])
        self.assertFalse(any(m == 'sendMessage' for m,d in self.api.calls))
        self.assertEqual(self.total(), 60)

    def test_speech_prevents_ban(self):
        self.seed(self.days(), speaks=[self.days()[1]])
        self.a.tick(force=True)
        self.assertEqual(self.bans(), [])

    def test_gap_prevents_ban(self):
        self.seed(self.days())
        with self.bot.db:
            self.bot.db.execute('INSERT INTO activity_gaps VALUES(?)', (self.days()[1],))
        self.a.tick(force=True)
        self.assertEqual(self.bans(), [])

    def test_today_is_not_a_completed_day(self):
        self.seed([self.bot.points.day(), *self.days()[:2]])
        self.a.tick(force=True)
        self.assertEqual(self.bans(), [])

    def test_partial_deployment_day_excluded(self):
        self.seed(self.days())
        with self.bot.db:
            self.bot.db.execute("UPDATE meta SET value=? WHERE key='activity_started'", (str(time.time()-2*86400),))
        self.a.tick(force=True)
        self.assertEqual(self.bans(), [])

    def test_nonmembers_bots_and_admins_are_not_blacklisted(self):
        self.seed(self.days())
        for member in ({'status':'administrator'}, {'status':'creator'}, {'status':'left'},
                       {'status':'restricted','is_member':False}, {'status':'member','user':{'is_bot':True}}):
            self.api.members[2] = member
            self.a.tick(force=True)
        self.assertEqual(self.bans(), [])

    def test_failed_ban_retries(self):
        self.seed(self.days())
        self.api.failure = 'banChatMember'
        self.a.tick(force=True)
        self.assertEqual(self.total(), 60)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM attention_bans').fetchone()[0], 0)
        self.api.failure = None
        self.a.tick(force=True)
        self.assertEqual(len(self.bans()), 2)
        self.assertEqual(self.total(), 0)

    def test_attention_flag_table_is_not_created(self):
        row=self.bot.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='attention_flags'").fetchone()
        self.assertIsNone(row)
