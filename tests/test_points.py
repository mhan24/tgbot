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
    def test_checkin_once_and_range(self):
        n,status=self.points.award(self.user,'checkin','a',100000)
        self.assertIn(n,range(1,6));self.assertEqual(status,'added')
        self.assertEqual(self.points.award(self.user,'checkin','b',100000),(0,'limit'))
        self.assertEqual(self.points.award(self.user,'checkin','a',100000),(n,'replay'))
        self.assertEqual(self.points.stats(2,100000)[0],n)
    def test_message_cap_and_next_day(self):
        for i in range(10): self.points.award(self.user,'message',str(i),100000)
        self.assertEqual(self.points.stats(2,100000)[0],5)
        self.points.award(self.user,'message','newday',186400)
        self.assertEqual(self.points.stats(2,186400)[0],6)
        self.assertEqual(self.points.stats(2,186400)[2],1)
    def test_configured_checkin_range_and_message_daily_points_cap(self):
        points=Points(self.db,checkin_min=5,checkin_max=10,message_points=2,message_daily_limit=5)
        with patch('points.secrets.randbelow',return_value=0):
            self.assertEqual(points.award(self.user,'checkin','checkin',100000),(5,'added'))
        self.assertEqual(points.award(self.user,'message','m1',100000),(2,'added'))
        self.assertEqual(points.award(self.user,'message','m2',100000),(2,'added'))
        self.assertEqual(points.award(self.user,'message','m3',100000),(1,'added'))
        self.assertEqual(points.award(self.user,'message','m4',100000),(0,'limit'))
        self.assertEqual(points.stats(2,100000)[:3],(10,5,5))
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
        with patch('points.secrets.randbelow',return_value=4):
            bot.handle({'update_id':6,'message':msg(6,'/checkin')})
            bot.handle({'update_id':7,'message':msg(7,'/checkin')})
        self.assertEqual(bot.points.stats(2)[0],7)
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

    def test_private_chat_redirects_other_points_commands(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.bot_id=99;bot.username='testbot'
        bot.handle({'update_id':1,'message':{'chat':{'id':2,'type':'private'},'message_id':1,'from':self.user,'text':'/checkin'}})
        self.assertIn('私聊支持积分',[d for m,d in api.calls if m=='sendMessage'][-1]['text'])
        self.assertEqual(bot.points.stats(2)[0],0)

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
        self.assertFalse(any(m=='sendMessage' for m,d in api.calls))
        self.assertEqual(self.db.execute('SELECT count(*) FROM warnings').fetchone()[0],0)

    def test_hidden_avatar_checkin_visible_warning_once_per_message_and_mute(self):
        api=FakeAPI();api.photos={'total_count':0,'photos':[]}
        bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        for mid in (10,10,11,12):
            bot.points_command({'text':'/checkin','from':self.user,'message_id':mid})
        self.assertEqual(self.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone()[0],3)
        self.assertEqual(sum(m=='restrictChatMember' for m,d in api.calls),1)
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

    def test_successful_and_repeated_checkins_deleted(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.points_command({'text':'/checkin','from':self.user,'message_id':31})
        bot.points_command({'text':'/checkin','from':self.user,'message_id':32})
        deleted=[d['message_id'] for m,d in api.calls if m=='deleteMessage']
        self.assertEqual(deleted,[31,32])
    def test_failed_avatar_checkin_deleted(self):
        api=FakeAPI();api.photos={'total_count':0,'photos':[]}
        bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.points_command({'text':'/checkin','from':self.user,'message_id':33})
        self.assertTrue(any(m=='deleteMessage' for m,d in api.calls))

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
        # delete the source message after replying.
        bot.avatar_visible=lambda uid: True
        checkin={'chat':{'id':-1,'type':'supergroup'},'from':self.user,'message_id':54,'text':'签到'}
        self.assertTrue(bot.points_command(checkin))
        self.assertGreater(bot.points.stats(self.user['id'])[0],0)
        self.assertIn(('deleteMessage',{'chat_id':-1,'message_id':54}),api.calls)

        # Lookup aliases use the same private-only output as their English names.
        for n,alias,canonical in ((55,'积分','/points'),(56,'排行榜','/rank'),(57,'原神榜','/milestones')):
            alias_msg={'chat':{'id':-1,'type':'supergroup'},'from':self.user,'message_id':n,'text':alias}
            self.assertTrue(bot.points_command(alias_msg))
            reply=[data for method,data in api.calls if method=='sendMessage'][-1]
            self.assertIn(f'start={canonical[1:]}',reply['reply_markup']['inline_keyboard'][0][0]['url'])

    def test_points_and_rank_delete_source_after_reply_in_correct_chat(self):
        for chat in ({'id':-1,'type':'supergroup'},{'id':2,'type':'private'}):
            for cmd in ('/points','/rank'):
                api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
                bot.username='testbot'
                bot.points_command({'chat':chat,'from':self.user,'message_id':51,'text':cmd})
                sent=next(i for i,(m,d) in enumerate(api.calls) if m=='sendMessage')
                deleted=next(i for i,(m,d) in enumerate(api.calls) if m=='deleteMessage')
                self.assertGreater(deleted,sent)
                self.assertEqual(api.calls[deleted][1],{'chat_id':chat['id'],'message_id':51})
    def test_checkin_source_deleted_after_success_reply(self):
        api=FakeAPI();bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},api,self.db)
        bot.points_command({'from':self.user,'message_id':52,'text':'/checkin'})
        sent=max(i for i,(m,d) in enumerate(api.calls) if m=='sendMessage')
        deleted=next(i for i,(m,d) in enumerate(api.calls) if m=='deleteMessage')
        self.assertGreater(deleted,sent)
