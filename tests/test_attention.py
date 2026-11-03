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
        self.a = self.bot.attention
        # deployment far in the past, then drop the startup gap the first tick records
        with self.bot.db:
            self.bot.db.execute("UPDATE meta SET value=? WHERE key='activity_started'", (str(time.time() - 40 * 86400),))
            self.bot.db.execute("INSERT OR REPLACE INTO meta VALUES('activity_heartbeat',?)", (str(time.time()),))
            self.bot.db.execute('DELETE FROM activity_gaps')
        self.addCleanup(self.bot.db.close)

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

    def mutes(self):
        return [d for m,d in self.api.calls if m == 'restrictChatMember']

    def total(self):
        return self.bot.points.stats(2)[0]

    def test_three_days_mute_keeps_points_and_never_repeats_one_window(self):
        self.seed(self.days())
        self.a.tick(force=True)
        self.assertFalse(self.bot.blacklisted(2))
        self.assertEqual(len(self.mutes()), 1)
        until = self.mutes()[0]['until_date']
        self.assertTrue(time.time() + self.bot.mute_seconds - 5 <= until <= time.time() + self.bot.mute_seconds + 5)
        self.assertEqual(self.bot.db.execute('SELECT mute_until FROM warnings WHERE uid=2').fetchone()[0], until)
        self.assertEqual(self.total(), 60)
        self.assertTrue(any('仅签到未发言' in d.get('text', '') for m, d in self.api.calls if m == 'sendMessage'))
        self.a.tick(force=True)
        self.assertEqual(len(self.mutes()), 1)
        self.assertEqual(self.total(), 60)

    def test_incomplete_or_unreliable_activity_never_mutes(self):
        for case in ('two_days', 'speech', 'gap', 'today', 'partial_deployment'):
            with self.subTest(case=case):
                with self.bot.db:
                    for table in ('point_events', 'activity_days', 'activity_gaps'):
                        self.bot.db.execute(f'DELETE FROM {table}')
                    started = time.time() - (2 if case == 'partial_deployment' else 40)*86400
                    self.bot.db.execute("UPDATE meta SET value=? WHERE key='activity_started'", (str(started),))
                days = self.days()
                checkins = days[:2] if case == 'two_days' else ([self.bot.points.day(), *days[:2]] if case == 'today' else days)
                self.seed(checkins, speaks=[days[1]] if case == 'speech' else ())
                if case == 'gap':
                    with self.bot.db:self.bot.db.execute('INSERT INTO activity_gaps VALUES(?)', (days[1],))
                self.a.tick(force=True)
                self.assertEqual(self.mutes(), [])
                self.assertFalse(any(m == 'sendMessage' for m,d in self.api.calls))
                self.assertEqual(self.total(), 60)

    def test_nonmembers_bots_and_admins_are_not_muted(self):
        self.seed(self.days())
        for member in ({'status':'administrator'}, {'status':'creator'}, {'status':'left'},
                       {'status':'restricted','is_member':False}, {'status':'member','user':{'is_bot':True}}):
            self.api.members[2] = member
            self.a.tick(force=True)
        self.assertEqual(self.mutes(), [])

    def test_failed_mute_retries(self):
        self.seed(self.days())
        self.api.failure = 'restrictChatMember'
        self.a.tick(force=True)
        self.assertEqual(self.total(), 60)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM attention_bans').fetchone()[0], 0)
        self.assertIsNone(self.bot.db.execute('SELECT mute_until FROM warnings WHERE uid=2').fetchone())
        self.api.failure = None
        self.a.tick(force=True)
        # FakeAPI records the failed attempt too, so two calls: failed + successful retry.
        self.assertEqual(len(self.mutes()), 2)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM attention_bans').fetchone()[0], 1)
        self.assertEqual(self.total(), 60)
        self.a.tick(force=True)
        self.assertEqual(len(self.mutes()), 2)
