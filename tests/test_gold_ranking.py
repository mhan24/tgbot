import json
import sqlite3
import unittest
from unittest.mock import patch
from bot import Bot
from test_bot import FakeAPI

DAY = 86400


class GoldRankingTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, self.api,
                       sqlite3.connect(':memory:'))
        self.addCleanup(self.bot.db.close)

    def member(self, uid, name=None, total=0):
        with self.bot.db:
            self.bot.db.execute('INSERT OR REPLACE INTO point_users VALUES(?,?,?)',
                                (uid, name or f'Member{uid}', total))

    def draw(self, uid, results, at, event=None):
        with self.bot.db:
            self.bot.db.execute('INSERT INTO checkin_draws VALUES(?,?,?,?,?,?,0,0)',
                                (event or f'checkin:-1:{uid}:{at}', uid, '2026-11-01',
                                 json.dumps(results), 0, at))

    def texts(self):
        return [d['text'] for m, d in self.api.calls if m == 'sendMessage']

    # ---- ranking data -------------------------------------------------

    def test_gold_ranking_orders_by_first_gold_without_point_threshold(self):
        self.member(2, '甲', total=0)          # 0 points, still listed
        self.member(3, '乙', total=900)
        self.member(4, '丙', total=50)
        self.draw(3, [3] * 10, 100)            # no gold
        self.draw(3, [3, 3, 3, 3, 3, 3, 3, 3, 3, 5], 200)
        self.draw(2, [6] + [3] * 9, 300)       # UP counts as gold
        self.draw(4, [4] * 10, 400)            # four stars only
        rows, total, page, pages = self.bot.points.gold_leaderboard()
        self.assertEqual(total, 2)
        self.assertEqual([(r['uid'], r['ordinal']) for r in rows], [(3, 1), (2, 2)])
        self.assertEqual(rows[0]['first_at'], 200)
        self.assertEqual(rows[0]['first_day'], '1970-01-01')
        self.assertEqual(rows[0]['name'], '乙')

    def test_repeat_gold_keeps_first_position_and_counts(self):
        self.member(5)
        self.draw(5, [5] + [3] * 9, 100)
        self.draw(6, [5] + [3] * 9, 200)
        self.draw(5, [6, 6] + [3] * 8, 300)
        rows = self.bot.points.gold_leaderboard()[0]
        self.assertEqual([(r['uid'], r['ordinal'], r['golds']) for r in rows],
                         [(5, 1, 3), (6, 2, 1)])

    def test_paging_and_clamping(self):
        for uid in range(10, 75):
            self.member(uid)
            self.draw(uid, [5] + [3] * 9, 1000 + uid)
        rows, total, page, pages = self.bot.points.gold_leaderboard(0)
        self.assertEqual((total, page, pages, len(rows)), (65, 0, 2, 50))
        self.assertEqual(rows[0]['ordinal'], 1)
        rows, total, page, pages = self.bot.points.gold_leaderboard(1)
        self.assertEqual((page, len(rows), rows[0]['ordinal']), (1, 15, 51))
        self.assertEqual(self.bot.points.gold_leaderboard(9)[2], 1)
        self.assertEqual(self.bot.points.gold_leaderboard(-4)[2], 0)

    def test_member_without_point_user_row_falls_back_to_id(self):
        self.draw(7, [5], 100)
        rows = self.bot.points.gold_leaderboard()[0]
        self.assertEqual(rows[0]['name'], '7')

    def test_corrupt_draw_rows_are_ignored(self):
        self.member(8)
        with self.bot.db:
            self.bot.db.execute("INSERT INTO checkin_draws VALUES('bad',8,'2026-11-01','oops',0,10,0,0)")
        self.draw(8, [5, 3], 20)
        rows = self.bot.points.gold_leaderboard()[0]
        self.assertEqual([(r['uid'], r['ordinal']) for r in rows], [(8, 1)])

    # ---- private command ---------------------------------------------

    def test_private_milestones_renders_gold_ranking(self):
        self.member(2, 'A<b>@c', total=0)
        self.draw(2, [5] + [3] * 9, 100)
        self.bot.points_command({'chat': {'id': 2, 'type': 'private'}, 'from': {'id': 2},
                                 'message_id': 30, 'text': '/milestones'})
        text = self.texts()[-1]
        self.assertIn('出金榜（共 1 人）', text)
        self.assertIn('1. A&lt;b&gt;＠c', text)
        self.assertIn('累计 1 次', text)
        self.assertNotIn('tg://', text)
        self.assertNotIn('@c', text)

    def test_private_milestones_pages_and_empty_state(self):
        self.bot.points_command({'chat': {'id': 2, 'type': 'private'}, 'from': {'id': 2},
                                 'message_id': 31, 'text': '/milestones'})
        self.assertIn('还没有成员出金', self.texts()[-1])
        self.api.calls.clear()
        for uid in range(10, 75):
            self.member(uid)
            self.draw(uid, [5] + [3] * 9, 1000 + uid)
        self.bot.points_command({'chat': {'id': 2, 'type': 'private'}, 'from': {'id': 2},
                                 'message_id': 32, 'text': '/milestones'})
        self.assertIn('第 1/2 页', self.texts()[-1])

    def test_gold_callbacks_page_and_verify_owner(self):
        for uid in range(10, 75):
            self.member(uid)
            self.draw(uid, [5] + [3] * 9, 1000 + uid)

        def click(page, owner=2, chat=2):
            self.bot.handle({'callback_query': {
                'id': 'q', 'from': {'id': owner}, 'data': f'gold:-1:{owner}:{page}',
                'message': {'message_id': 8, 'chat': {'id': chat, 'type': 'private'}}}})

        click(1)
        edits = [d for m, d in self.api.calls if m == 'editMessageText']
        self.assertEqual(len(edits), 1)
        self.assertIn('第 2/2 页', edits[0]['text'])
        click(1, owner=3)
        click(1, chat=-1)
        self.assertEqual(len([d for m, d in self.api.calls if m == 'editMessageText']), 1)
        self.assertEqual(self.api.calls[-1][0], 'answerCallbackQuery')

    def test_group_milestones_points_to_private_chat(self):
        self.bot.username = 'testbot'
        self.bot.points_command({'chat': {'id': -1, 'type': 'supergroup'}, 'from': {'id': 2},
                                 'message_id': 33, 'text': '/milestones'})
        reply = [d for m, d in self.api.calls if m == 'sendMessage'][-1]
        self.assertIn('start=milestones', reply['reply_markup']['inline_keyboard'][0][0]['url'])

    # ---- group broadcast ---------------------------------------------

    def test_first_gold_broadcast_and_replay_are_sent_once(self):
        user = {'id': 2, 'first_name': 'Alice'}
        self.bot.username = 'testbot'
        self.bot.avatar_visible = lambda uid, **kwargs: True
        rolls = [0, 999999, 999999, 999999] + [999999, 999999] * 8
        msg = {'chat': {'id': -1, 'type': 'supergroup'}, 'from': user,
               'message_id': 60, 'date': 1000, 'text': '/checkin'}
        with patch('points.secrets.randbelow', side_effect=rolls):
            self.bot.points_command(msg)
            self.bot.points_command(msg)   # duplicate update replays the same event
        broadcasts = [text for text in self.texts() if '出金榜' in text]
        self.assertEqual(len(broadcasts), 1)
        self.assertIn('第 1 位出金，已进入出金榜', broadcasts[0])

    def test_repeat_gold_broadcast_shows_accumulated_count(self):
        self.bot.username = 'testbot'
        self.bot.avatar_visible = lambda uid, **kwargs: True
        user = {'id': 2, 'first_name': 'Alice'}
        first = {'chat': {'id': -1, 'type': 'supergroup'}, 'from': user,
                 'message_id': 61, 'date': 1000, 'text': '/checkin'}
        with patch('points.secrets.randbelow', side_effect=[0, 999999] + [999999, 999999] * 9):
            self.bot.points_command(first)
        second = {'chat': {'id': -1, 'type': 'supergroup'}, 'from': user,
                  'message_id': 62, 'date': 1000 + DAY, 'text': '/checkin'}
        with patch('points.secrets.randbelow', side_effect=[0, 999999] + [999999, 999999] * 9):
            self.bot.points_command(second)
        broadcasts = [text for text in self.texts() if '出金榜' in text]
        self.assertEqual(len(broadcasts), 2)
        self.assertIn('第 1 位出金，已进入出金榜', broadcasts[0])
        self.assertIn('出金榜第 1 位，累计出金 2 次', broadcasts[1])

    def test_no_broadcast_without_five_star(self):
        self.bot.username = 'testbot'
        self.bot.avatar_visible = lambda uid, **kwargs: True
        msg = {'chat': {'id': -1, 'type': 'supergroup'}, 'from': {'id': 2, 'first_name': 'Alice'},
               'message_id': 63, 'date': 1000, 'text': '/checkin'}
        with patch('points.secrets.randbelow', return_value=999999):
            self.bot.points_command(msg)
        self.assertFalse([text for text in self.texts() if '出金榜' in text])
        self.assertEqual(self.bot.points.gold_leaderboard()[1], 0)


if __name__ == '__main__':
    unittest.main()
