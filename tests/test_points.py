import json
import sqlite3
import unittest
from unittest.mock import patch
from datetime import datetime,timezone
from points import Points
from bot import Bot
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.points=Points(self.db);self.user={'id':2,'first_name':'Alice'}
        self.addCleanup(self.db.close)
    def test_ten_pull_single_reward_and_replay(self):
        with patch('points.secrets.randbelow', return_value=999999):
            amount, status, results, pity4, pity5, guaranteed = self.points.checkin(
                self.user, 'a', 100000)
        self.assertEqual((amount, status), (1, 'added'))
        self.assertEqual(results, [3] * 9 + [4])
        self.assertEqual((pity4, pity5, guaranteed), (0, 10, 0))
        self.assertEqual(self.points.checkin(self.user, 'a', 100000)[0:3], (1, 'replay', results))
        self.assertEqual(self.points.award(self.user, 'checkin', 'b', 100000)[0:2], (0, 'limit'))
        self.assertEqual(self.points.stats(2, 100000)[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM checkin_draws').fetchone()[0], 1)

    def test_five_star_standard_then_guaranteed_up(self):
        rolls = [0, 999999] + [999999, 999999] * 9
        with patch('points.secrets.randbelow', side_effect=rolls):
            amount, status, results, pity4, pity5, guaranteed = self.points.checkin(
                self.user, 'a', 100000)
        self.assertEqual((amount, status), (5, 'added'))
        self.assertEqual(results, [5] + [3] * 9)
        self.assertEqual((pity4, pity5, guaranteed), (9, 9, 1))
        self.assertFalse(self.points.checkin_notified('a', 'result'))
        self.assertFalse(self.points.checkin_notified('a', 'broadcast'))
        self.points.mark_checkin_notified('a', 'result')
        self.points.mark_checkin_notified('a', 'broadcast')
        self.assertTrue(self.points.checkin_notified('a', 'broadcast'))

        rolls = [0, 999999] + [999999, 999999] * 9
        with patch('points.secrets.randbelow', side_effect=rolls):
            amount, status, results, pity4, pity5, guaranteed = self.points.checkin(
                self.user, 'b', 100000 + 86400)
        self.assertEqual((amount, status), (10, 'added'))
        self.assertEqual(results[0], 6)
        self.assertEqual((pity4, pity5, guaranteed), (9, 9, 0))

    def test_five_star_hard_pity_and_four_star_carryover(self):
        with self.db:
            self.db.execute('INSERT INTO checkin_pity VALUES(2,0,89,1,1)')
        with patch('points.secrets.randbelow', return_value=999999):
            amount, status, results, pity4, pity5, guaranteed = self.points.checkin(
                self.user, 'a', 100000)
        self.assertEqual((amount, status), (10, 'added'))
        self.assertEqual(results[0], 6)
        self.assertEqual((pity4, pity5, guaranteed), (9, 9, 0))
        with patch('points.secrets.randbelow', return_value=999999):
            amount, status, results, pity4, pity5, guaranteed = self.points.checkin(
                self.user, 'b', 100000 + 86400)
        self.assertEqual((amount, status), (1, 'added'))
        self.assertEqual(results, [4] + [3] * 9)
        self.assertEqual((pity4, pity5, guaranteed), (9, 19, 0))

    def test_multi_high_rarity_points_are_summed_and_capped(self):
        cases = (
            ([0, 0] + [999999, 0]*2 + [999999, 999999]*7, [6, 4, 4] + [3]*7, 12),
            ([0, 999999, 999999, 0] + [999999, 999999]*8, [5, 4] + [3]*8, 6),
            ([999999, 0]*2 + [999999, 999999]*8, [4, 4] + [3]*8, 2),
            ([0, 0]*3, [6]*3 + [3]*7, 30),
        )
        for uid, (rolls, expected, amount) in enumerate(cases, 3):
            with self.subTest(results=expected), patch('points.secrets.randbelow', side_effect=rolls):
                actual = self.points.checkin({'id': uid}, str(uid), 100000)
                self.assertEqual(actual[:3], (amount, 'added', expected))

    def test_message_cap_and_next_day(self):
        for i in range(10): self.points.award(self.user,'message',str(i),100000)
        self.assertEqual(self.points.stats(2,100000)[0],5)
        self.points.award(self.user,'message','newday',186400)
        self.assertEqual(self.points.stats(2,186400)[0],6)
        self.assertEqual(self.points.stats(2,186400)[2],1)
    def test_message_points_are_configurable(self):
        points=Points(self.db,message_points=2,message_daily_limit=5)
        self.assertEqual(points.award(self.user,'message','m1',100000),(2,'added'))
        self.assertEqual(points.award(self.user,'message','m2',100000),(2,'added'))
        self.assertEqual(points.award(self.user,'message','m3',100000),(1,'added'))
        self.assertEqual(points.award(self.user,'message','m4',100000),(0,'limit'))
        self.assertEqual(points.stats(2,100000)[:3],(5,0,5))

    def test_checkin_broadcasts_only_five_star_tiers(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.bot_id=99;bot.username='testbot'
        def msg(mid,uid):
            return {'chat':{'id':-1,'type':'supergroup'},'message_id':mid,
                    'from':{'id':uid,'first_name':f'U{uid}'},'text':'/checkin'}
        with patch('points.secrets.randbelow',return_value=999999):
            bot.points_command(msg(1,2))
        sends=[d.get('text','') for m,d in api.calls if m=='sendMessage']
        self.assertTrue(any('十连签到成功' in text for text in sends))
        self.assertTrue(any('四星 ×1' in text and '本次获得 1 积分' in text for text in sends))
        self.assertFalse(any(text.startswith('🎉') for text in sends))
        with patch('points.secrets.randbelow',side_effect=[0,999999]+[999999,999999]*9):
            bot.points_command(msg(2,3))
        sends=[d.get('text','') for m,d in api.calls if m=='sendMessage']
        self.assertTrue(any('五星常驻' in text and 'tg://user?id=3' in text for text in sends))
        with patch('points.secrets.randbelow',side_effect=[0,0]+[999999,999999]*9):
            bot.points_command(msg(3,4))
        sends=[d.get('text','') for m,d in api.calls if m=='sendMessage']
        self.assertTrue(any('五星 UP' in text and 'tg://user?id=4' in text for text in sends))
        self.assertEqual(bot.points.stats(2)[0],1)
        self.assertEqual(bot.points.stats(3)[0],5)
        self.assertEqual(bot.points.stats(4)[0],10)
        with patch('points.secrets.randbelow',return_value=0):
            bot.points_command(msg(4,5))
        sends=[d.get('text','') for m,d in api.calls if m=='sendMessage']
        self.assertTrue(any('三黄' in text and '五星 UP ×3' in text and 'tg://user?id=5' in text for text in sends))
        self.assertEqual(bot.points.stats(5)[0],30)

    def test_failed_broadcast_retries_without_duplicate_result(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.bot_id=99;bot.username='testbot'
        msg={'chat':{'id':-1,'type':'supergroup'},'message_id':1,
             'from':{'id':2,'first_name':'Alice'},'text':'/checkin'}
        original=bot.send
        def flaky(chat,text,keep=False,**kwargs):
            if text.startswith('🎉'):
                raise RuntimeError('broadcast failed')
            return original(chat,text,keep=keep,**kwargs)
        bot.send=flaky
        rolls=[0,999999]+[999999,999999]*9
        with patch('points.secrets.randbelow',side_effect=rolls):
            with self.assertRaises(RuntimeError):
                bot.points_command(msg)
        self.assertEqual(bot.points.stats(2)[0],5)
        bot.send=original
        rolls=[0,999999]+[999999,999999]*9
        with patch('points.secrets.randbelow',side_effect=rolls):
            bot.points_command(msg)
        broadcasts=[d.get('text','') for m,d in api.calls if m=='sendMessage' and d.get('text','').startswith('🎉')]
        results=[d.get('text','') for m,d in api.calls if m=='sendMessage' and '十连签到成功' in d.get('text','')]
        self.assertEqual(len(broadcasts),1)
        self.assertEqual(len(results),1)
        self.assertEqual(bot.points.stats(2)[0],5)
    def test_beijing_midnight(self):
        before=datetime(2026,9,15,15,59,59,tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.points.day(before),'2026-09-15')
        self.assertEqual(self.points.day(before+1),'2026-09-16')
        self.assertEqual(self.points.award(self.user,'checkin','a',before)[1],'added')
        self.assertEqual(self.points.award(self.user,'checkin','b',before+1)[1],'added')
    def test_duplicate_message_and_persistence(self):
        self.points.award(self.user,'message','a',100000)
        other=Points(self.db);other.award(self.user,'message','a',100000)
        self.assertEqual(other.stats(2,100000)[0],1)
    def test_ranking_and_ties(self):
        for uid in [2,3,4]:
            for n in range(uid):self.points.award({'id':uid},'message',f'{uid}:{n}',100000)
        self.assertEqual([r['uid'] for r in self.points.leaderboard()],[4,3,2])
        self.assertEqual(self.points.stats(2,100000)[3],3)
    def test_commands_and_moderation_eligibility(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db);bot.bot_id=99
        def msg(n,text,**extra):
            return {'chat':{'id':-1,'type':'supergroup'},'message_id':n,'from':self.user,'text':text,**extra}
        bot.handle({'update_id':1,'message':msg(1,'你好')})
        bot.handle({'update_id':2,'edited_message':msg(1,'再见')})
        bot.handle({'update_id':3,'message':msg(3,'日赚1000，私聊我')})
        bot.handle({'update_id':4,'message':msg(4,'/random')})
        bot.handle({'update_id':5,'message':msg(5,'bot',**{'from':{'id':5,'is_bot':True}})})
        self.assertEqual(bot.points.stats(2)[0],2)
        with patch('points.secrets.randbelow',return_value=999999):
            bot.handle({'update_id':6,'message':msg(6,'/checkin')})
            bot.handle({'update_id':7,'message':msg(7,'/checkin')})
        self.assertEqual(bot.points.stats(2)[0],3)
        bot.handle({'update_id':8,'message':msg(8,'/rank')})
        self.assertIn('私聊',[d for m,d in api.calls if m=='sendMessage'][-1]['text'])
    def test_group_rank_redirects_private_rank_is_complete(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.bot_id=99;bot.username='testbot'
        with self.db:
            for uid in range(100,112):
                self.db.execute('INSERT INTO point_users VALUES(?,?,?)',(uid,f'User{uid}',uid))
        bot.handle({'update_id':1,'message':{'chat':{'id':-1,'type':'supergroup'},'message_id':1,'from':self.user,'text':'/rank'}})
        group_text=[d for m,d in api.calls if m=='sendMessage'][-1]['text']
        self.assertIn('私聊',group_text)
        self.assertNotIn('User100',group_text)
        bot.handle({'update_id':2,'message':{'chat':{'id':2,'type':'private'},'message_id':2,'from':self.user,'text':'/rank'}})
        private_text=[d for m,d in api.calls if m=='sendMessage'][-1]['text']
        self.assertIn('全部 12 人',private_text)
        self.assertEqual(private_text.count(' 分'),12)

    def test_private_rank_needs_group_membership(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.bot_id=99;bot.username='testbot'
        api.members[2]={'status':'left'}
        bot.handle({'update_id':1,'message':{'chat':{'id':2,'type':'private'},'message_id':1,'from':self.user,'text':'/rank'}})
        self.assertIn('请先加入「-1」',[d for m,d in api.calls if m=='sendMessage'][-1]['text'])

    def test_private_checkin_query_is_read_only_and_member_scoped(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.bot_id=99;bot.username='testbot'
        for uid, results in ((2, [5,6]+[3]*8), (3, [6]*10)):
            self.db.execute('INSERT INTO checkin_draws VALUES(?,?,?,?,?,?,?,?)',
                            (str(uid), uid, '2026-01-01', json.dumps(results), 15, 1, 1, 1))
        self.db.execute('INSERT INTO checkin_pity VALUES(2,4,20,1,1)')
        self.db.commit()
        # Reopening current storage preserves records without an upgrade path.
        Points(self.db)
        for mid, (uid, text, status, expected) in enumerate((
            (2, '/checkin', 'member', ('累计抽数：10', '累计出金：2 次（五星 UP 1 次 · 常驻 1 次）',
                                      '四星保底进度：4/10', '五星保底进度：20/90', '下次五星必为 UP：是')),
            (4, '签到', 'member', ('累计出金：0 次', '0/90', '否（50/50）')),
            (2, '签到', 'left', ('请先加入',)),
        ), 1):
            with self.subTest(uid=uid, text=text, status=status):
                api.members[uid]={'status':status}
                bot.handle({'update_id':mid, 'message':{'chat':{'id':uid,'type':'private'},
                            'message_id':mid,'from':{'id':uid},'text':text}})
                reply=[d for m,d in api.calls if m=='sendMessage'][-1]['text']
                for fragment in expected:self.assertIn(fragment, reply)
        self.assertEqual(self.points._pity(2), (4,20,1))
        self.assertEqual(self.db.execute('SELECT count(*) FROM checkin_draws').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM point_events').fetchone()[0], 0)
        self.assertEqual(self.points.stats(2)[0], 0)

    def test_pending_users_cannot_checkin(self):
        bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},FakeAPI(),self.db)
        self.db.execute("INSERT INTO verification(uid,kind) VALUES(2,'member')")
        bot.points_command({'text':'/checkin','from':self.user,'message_id':1})
        self.assertEqual(bot.points.stats(2)[0],0)

    def test_avatar_required_and_retry_does_not_consume_daily_limit(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        msg={'text':'/checkin','from':self.user,'message_id':10}
        api.photos={'total_count':0,'photos':[]}
        bot.points_command(msg)
        self.assertEqual(bot.points.stats(2)[0],0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM point_events WHERE kind='checkin'").fetchone()[0],0)
        api.photos={'total_count':1,'photos':[[{'file_id':'visible'}]]}
        bot.points_command(msg)
        self.assertGreater(bot.points.stats(2)[0],0)
    def test_avatar_api_failure_blocks_award(self):
        api=FakeAPI();api.failure='getUserProfilePhotos'
        bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.points_command({'text':'/checkin','from':self.user,'message_id':11})
        self.assertEqual(bot.points.stats(2)[0],0)
        self.assertIn('未加分、未计警告',[d['text'] for m,d in api.calls if m=='sendMessage'][-1])
        self.assertEqual(self.db.execute('SELECT count(*) FROM warnings').fetchone()[0],0)

    def test_hidden_avatar_checkin_visible_warning_once_per_message_and_mute(self):
        api=FakeAPI();api.photos={'total_count':0,'photos':[]}
        bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        for mid in (10,10,11,12):
            bot.points_command({'text':'/checkin','from':self.user,'message_id':mid})
        self.assertEqual(self.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone()[0],3)
        self.assertEqual(sum(m=='banChatMember' for m,d in api.calls),1)
        self.assertEqual(sum(m=='sendMessage' for m,d in api.calls),3)
        self.assertEqual(bot.points.stats(2)[0],0)
    def test_hidden_avatar_speech_no_points_no_warning(self):
        api=FakeAPI();api.photos={'total_count':0,'photos':[]}
        bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        msg={'chat':{'id':-1,'type':'supergroup'},'message_id':20,'from':self.user,'text':'普通聊天'}
        bot.handle({'update_id':20,'message':msg})
        self.assertEqual(bot.points.stats(2)[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM warnings').fetchone()[0],0)
        self.assertFalse(any(m=='sendMessage' for m,d in api.calls))
        api.photos={'total_count':1,'photos':[[{'file_id':'visible'}]]}
        msg['message_id']=21
        bot.handle({'update_id':21,'message':msg})
        self.assertEqual(bot.points.stats(2)[0],1)

    def test_checkin_source_retained_for_success_repeat_and_hidden_avatar(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        for mid, visible in ((31,True),(32,True),(33,False)):
            with self.subTest(mid=mid, visible=visible):
                api.photos={'total_count':int(visible),'photos':[[{'file_id':'visible'}]] if visible else []}
                bot.points_command({'text':'/checkin','from':self.user,'message_id':mid})
                self.assertFalse(any(m=='deleteMessage' for m,d in api.calls))

    def test_points_only_in_private_and_deep_link(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.username='testbot'
        with self.db:self.db.execute("INSERT INTO point_users VALUES(2,'Alice',42)")
        bot.points_command({'chat':{'id':-1,'type':'supergroup'},'from':self.user,'message_id':40,'text':'/points'})
        self.assertNotIn('42',[d for m,d in api.calls if m=='sendMessage'][-1]['text'])
        self.assertIn('start=points',[d for m,d in api.calls if m=='sendMessage'][-1]['reply_markup']['inline_keyboard'][0][0]['url'])
        bot.points_command({'chat':{'id':2,'type':'private'},'from':self.user,'message_id':41,'text':'/start points'})
        self.assertEqual([d for m,d in api.calls if m=='sendMessage'][-1]['chat_id'],2)
        self.assertIn('我的积分：42',[d for m,d in api.calls if m=='sendMessage'][-1]['text'])

    def test_milestones_command_and_deep_link(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.username='testbot'
        group_msg={'chat':{'id':-1,'type':'supergroup'},'from':self.user,'message_id':50,'text':'/milestones'}
        bot.points_command(group_msg)
        reply=[d for m,d in api.calls if m=='sendMessage'][-1]
        self.assertIn('start=milestones',reply['reply_markup']['inline_keyboard'][0][0]['url'])
        private_msg={'chat':{'id':2,'type':'private'},'from':self.user,'message_id':51,'text':'/start milestones'}
        bot.points_command(private_msg)
        self.assertIn('达标榜',[d for m,d in api.calls if m=='sendMessage'][-1]['text'])

    def test_chinese_points_aliases_work_without_replacing_english_commands(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.username='testbot'
        msg={'chat':{'id':2,'type':'private'},'from':self.user,'message_id':52,'text':'/start genshin'}
        self.assertFalse(bot.points_command(msg))
        for text in ('/checkin','签到','积分','排行榜','原神榜'):
            self.assertTrue(bot.is_bot_operation({'text':text,'from':self.user}),text)
        self.assertTrue(bot.is_bot_operation({'text':'/milestones','from':self.user}))
        for text in ('/签到','/积分','/排行榜','/原神榜'):
            self.assertFalse(bot.is_bot_operation({'text':text,'from':self.user}),text)
            self.assertFalse(bot.points_command({'chat':{'id':-1,'type':'supergroup'},
                                                 'from':self.user,'message_id':53,'text':text}),text)

        # Chinese check-in aliases enter the same validated path and still
        # retain the source message after replying.
        bot.avatar_visible=lambda uid, **kwargs: True
        checkin={'chat':{'id':-1,'type':'supergroup'},'from':self.user,'message_id':54,'text':'签到'}
        self.assertTrue(bot.points_command(checkin))
        self.assertGreater(bot.points.stats(self.user['id'])[0],0)
        self.assertNotIn(('deleteMessage',{'chat_id':-1,'message_id':54}),api.calls)

        # Lookup aliases use the same private-only output as their English names.
        for n,alias,canonical in ((55,'积分','/points'),(56,'排行榜','/rank'),(57,'原神榜','/milestones')):
            alias_msg={'chat':{'id':-1,'type':'supergroup'},'from':self.user,'message_id':n,'text':alias}
            self.assertTrue(bot.points_command(alias_msg))
            reply=[data for method,data in api.calls if method=='sendMessage'][-1]
            self.assertIn(f'start={canonical[1:]}',reply['reply_markup']['inline_keyboard'][0][0]['url'])

    def test_points_and_rank_retain_source_in_group_and_private(self):
        for chat in ({'id':-1,'type':'supergroup'},{'id':2,'type':'private'}):
            for cmd in ('/points','/rank'):
                api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
                bot.username='testbot'
                bot.points_command({'chat':chat,'from':self.user,'message_id':51,'text':cmd})
                sent=next(i for i,(m,d) in enumerate(api.calls) if m=='sendMessage')
                self.assertFalse(any(m == 'deleteMessage' for m, d in api.calls))
