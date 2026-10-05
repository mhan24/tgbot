import sqlite3
import time
import unittest
from unittest.mock import patch
from bot import Bot,APIError
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self):
        self.block=patch('airdrop.latest_block',return_value={'hash':'a'*64,'height':123,'provider':'test','chain':'Base'}).start()
        self.addCleanup(patch.stopall)
        self.api=FakeAPI();self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,sqlite3.connect(':memory:'))
        self.bot.bot_id=99
        for uid,total,status,is_bot in [(2,10,'member',False),(3,9,'member',False),(4,20,'left',False),(5,30,'member',True),(99,100,'administrator',True)]:
            self.bot.db.execute('INSERT INTO point_users VALUES(?,?,?)',(uid,f'Name{uid}',total))
            self.api.members[uid]={'user':{'id':uid,'is_bot':is_bot,'first_name':f'<@Name{uid}>'},'status':status}
        self.bot.db.commit()
        # Airdrops now require a member tag or a custom title.
        self.api.members[2]['tag']='家人'
        self.api.members[3]['tag']='家人'
        self.api.members[1]={'user':{'id':1,'is_bot':False,'first_name':'Owner'},'status':'creator'}
        self.api.members[8]={'user':{'id':8,'is_bot':False,'first_name':'Mod'},'status':'administrator'}
        self.msg={'chat':{'id':1,'type':'private'},'from':{'id':1,'first_name':'Owner'},'message_id':1,'text':'/airdrop 10 礼品'}
        self.send_patcher=patch.object(self.bot,'send',return_value={'message_id':88})
        self.send=self.send_patcher.start()
        self.addCleanup(patch.stopall)
    def test_exact_threshold_and_exclusions_without_debit(self):
        self.bot.airdrops.command(self.msg)
        row=self.bot.db.execute('SELECT * FROM airdrops').fetchone()
        self.assertEqual(self.bot.airdrops.winners(row['id'])[0]['uid'],2)
        self.assertEqual(self.bot.points.stats(2)[0],10)
        group_text=[c.args[1] for c in self.send.call_args_list if c.args[0]==-1][0]
        self.assertIn('tg://user?id=2',group_text);self.assertNotIn('@Name',group_text)
        self.assertIn('tg://user?id=1',group_text)
        self.assertIn('发起人：',group_text)
        self.assertIn('唯一哈希：<code>'+('a'*64),group_text)
        self.assertIn('&lt;',group_text)

    def test_current_schema_uses_normalized_winners(self):
        columns={row['name'] for row in self.bot.db.execute('PRAGMA table_info(airdrops)')}
        self.assertTrue({'actor_name','block_hash','block_height','winner_count','approved_by'}<=columns)
        self.assertFalse({'winner','name','balance'}&columns)

    def test_custom_winner_count_and_active_window(self):
        now=int(time.time())
        with self.bot.db:
            self.bot.db.execute('INSERT INTO user_activity(uid,last_active) VALUES(2,?)',(now,))
            self.bot.db.execute('INSERT INTO user_activity(uid,last_active) VALUES(3,?)',(now,))
        msg=dict(self.msg,text='/airdrop 9 840 5 奖品')
        self.bot.airdrops.command(msg)
        row=self.bot.db.execute('SELECT * FROM airdrops').fetchone()
        winners=self.bot.airdrops.winners(row['id'])
        self.assertEqual((row['minimum'],row['active_minutes'],row['winner_count']),(9,840,5))
        self.assertEqual({winner['uid'] for winner in winners},{2,3})
        self.assertEqual(row['status'],'drawn')
        text=[call.args[1] for call in self.send.call_args_list if call.args[0]==-1][0]
        self.assertIn('中奖人数：2/5',text)
        self.assertIn('tg://user?id=2',text)
        self.assertIn('tg://user?id=3',text)

    def test_custom_winner_count_is_carried_through_approval(self):
        request=dict(self.msg,text='/airdrop 10 0 2 礼品',chat={'id':-1,'type':'supergroup'},
                     **{'from':{'id':2,'first_name':'Member'}})
        self.bot.airdrops.command(request)
        grant=self.bot.db.execute('SELECT * FROM airdrop_grants').fetchone()
        self.assertEqual(grant['winner_count'],2)
        self.bot.airdrops.callback({'id':'q','from':{'id':8,'first_name':'Mod'},
            'data':f'a:ok:{grant["id"]}',
            'message':{'chat':{'id':-1,'type':'supergroup'},'message_id':grant['response']}})
        row=self.bot.db.execute('SELECT * FROM airdrops').fetchone()
        self.assertEqual(row['winner_count'],2)
        self.assertEqual(len(self.bot.airdrops.winners(row['id'])),1)

    def test_extra_airdrop_pending_notice_links_to_existing_group_approval(self):
        self.bot.group=-1001234567890
        with self.bot.db:
            self.bot.db.execute('INSERT INTO airdrops(source,actor,minimum,prize,candidates,created) VALUES(?,?,?,?,?,?)',
                                ('used-today',8,1,'Earlier prize','[]',int(time.time())))
        request=dict(self.msg,text='/airdrop 10 60 2 礼品',message_id=21,
                     **{'from':{'id':8,'first_name':'Mod'}})
        self.bot.airdrops.command(request)
        grant=self.bot.db.execute("SELECT * FROM airdrop_grants WHERE actor=8 AND request_kind='admin_extra'").fetchone()
        self.assertEqual(grant['status'],'pending')
        shortcut=self.send.call_args_list[-1].kwargs['reply_markup']
        self.assertEqual(shortcut['inline_keyboard'][0][0]['url'],
                         f'https://t.me/c/1234567890/{grant["response"]}')

        self.bot.airdrops.command(dict(request,message_id=22))
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM airdrop_grants WHERE actor=8 AND request_kind='admin_extra'").fetchone()[0],1)
        notice=self.send.call_args_list[-1]
        self.assertIn('正在等待群主批准',notice.args[1])
        self.assertEqual(notice.kwargs['reply_markup']['inline_keyboard'][0][0]['url'],
                         f'https://t.me/c/1234567890/{grant["response"]}')
    def test_repeat_update_does_not_redraw_or_republish(self):
        self.bot.airdrops.command(self.msg);self.bot.airdrops.command(self.msg)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],1)
        self.assertEqual(sum(c.args[0]==-1 for c in self.send.call_args_list),1)
    def test_nonadmin_can_request_approval_but_invalid_args_are_denied(self):
        request=dict(self.msg,chat={'id':-1,'type':'supergroup'},**{'from':{'id':2,'first_name':'Member'}})
        self.bot.airdrops.command(request)
        self.bot.airdrops.command(dict(self.msg,text='/airdrop -1 test'))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],0)
        grant=self.bot.db.execute('SELECT * FROM airdrop_grants').fetchone()
        self.assertEqual((grant['actor'],grant['status'],grant['request_kind']),(2,'pending','member'))

    def test_nonmember_cannot_create_an_airdrop_request(self):
        self.api.members[7]={'user':{'id':7,'is_bot':False,'first_name':'Outsider'},'status':'left'}
        msg=dict(self.msg,chat={'id':-1,'type':'supergroup'},
                 **{'from':{'id':7,'first_name':'Outsider'}})
        self.bot.airdrops.command(msg)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrop_grants').fetchone()[0],0)
        self.assertIn('只有当前管理群成员',self.send.call_args.args[1])

    def test_admin_approval_executes_member_request_and_records_both_people(self):
        request=dict(self.msg,chat={'id':-1,'type':'supergroup'},
                     **{'from':{'id':2,'first_name':'Member'}})
        self.bot.airdrops.command(request)
        grant=self.bot.db.execute('SELECT * FROM airdrop_grants').fetchone()
        self.assertEqual(grant['status'],'pending')
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],0)
        wrong_user={'id':'q0','from':{'id':2,'first_name':'Member'},
                    'data':f'a:ok:{grant["id"]}',
                    'message':{'chat':{'id':-1,'type':'supergroup'},'message_id':grant['response']}}
        self.bot.airdrops.callback(wrong_user)
        wrong_message={'id':'q1','from':{'id':8,'first_name':'Mod'},
                       'data':f'a:ok:{grant["id"]}',
                       'message':{'chat':{'id':-1,'type':'supergroup'},'message_id':999}}
        self.bot.airdrops.callback(wrong_message)
        self.assertEqual(self.bot.db.execute('SELECT status FROM airdrop_grants').fetchone()[0],'pending')
        callback={'id':'q','from':{'id':8,'first_name':'Mod'},
                  'data':f'a:ok:{grant["id"]}',
                  'message':{'chat':{'id':-1,'type':'supergroup'},'message_id':grant['response']}}
        self.bot.handle({'update_id':20,'callback_query':callback})
        row=self.bot.db.execute('SELECT * FROM airdrops').fetchone()
        self.assertEqual((row['actor'],row['approved_by'],row['approver_name']),(2,8,'Mod'))
        self.assertEqual(self.bot.db.execute('SELECT status FROM airdrop_grants').fetchone()[0],'used')
        result=[c.args[1] for c in self.send.call_args_list if c.args[0]==-1 and '空投开奖' in str(c.args[1])][0]
        self.assertIn('发起人：',result)
        self.assertIn('tg://user?id=2',result)
        self.assertIn('同意人：',result)
        self.assertIn('tg://user?id=8',result)
        self.assertIn(grant['response'],[r['message_id'] for r in self.bot.db.execute('SELECT message_id FROM deletions')])

    def test_airdrop_result_is_exempt_from_auto_deletion(self):
        self.send_patcher.stop()
        with self.bot.db:
            aid=self.bot.db.execute("INSERT INTO airdrops(source,actor,minimum,prize,candidates,status,created) VALUES('auto-delete',1,10,'礼品','[]','drawn',1)").lastrowid
            self.bot.db.execute("INSERT INTO airdrop_winners(airdrop_id,ordinal,uid,name,balance) VALUES(?,1,2,'Winner',10)",(aid,))
        row=self.bot.db.execute("SELECT * FROM airdrops WHERE source='auto-delete'").fetchone()
        self.bot.airdrops.finalize(row,-1)
        self.assertIsNone(self.bot.db.execute('SELECT chat FROM deletions WHERE message_id=1').fetchone())

    def test_member_application_denied_by_admin_consumes_the_daily_request(self):
        msg=dict(self.msg,chat={'id':-1,'type':'supergroup'},
                 **{'from':{'id':2,'first_name':'Member'}})
        self.bot.airdrops.command(msg)
        grant=self.bot.db.execute('SELECT * FROM airdrop_grants').fetchone()
        query={'id':'q','from':{'id':8,'first_name':'Mod'},'data':f'a:no:{grant["id"]}',
               'message':{'chat':{'id':-1,'type':'supergroup'},'message_id':grant['response']}}
        self.bot.airdrops.callback(query)
        self.assertEqual(self.bot.db.execute('SELECT status FROM airdrop_grants').fetchone()[0],'denied')
        self.bot.airdrops.command(dict(msg,message_id=2))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrop_grants WHERE actor=2').fetchone()[0],1)
        self.assertIn('每天最多申请一次',self.send.call_args.args[1])

    def test_history_lists_only_latest_twenty_records(self):
        with self.bot.db:
            for number in range(1,26):
                self.bot.db.execute('INSERT INTO airdrops(source,actor,minimum,prize,candidates,created,actor_name) '
                                    'VALUES(?,?,?,?,?,?,?)',
                                    (f'history-{number}',2,10,f'Prize {number}','[]',number,f'Member {number}'))
        msg=dict(self.msg,text='/airdrops')
        self.bot.airdrops.command(msg)
        text=self.send.call_args.args[1]
        self.assertIn('最近 20 条',text)
        self.assertIn('#25',text)
        self.assertIn('#6',text)
        self.assertNotIn('#5',text)

    def test_history_id_shows_requester_approver_winner_and_block_proof(self):
        request=dict(self.msg,chat={'id':-1,'type':'supergroup'},
                     **{'from':{'id':2,'first_name':'Member'}})
        self.bot.airdrops.command(request)
        grant=self.bot.db.execute('SELECT * FROM airdrop_grants').fetchone()
        self.bot.airdrops.callback({'id':'q','from':{'id':8,'first_name':'Approver'},
            'data':f'a:ok:{grant["id"]}',
            'message':{'chat':{'id':-1,'type':'supergroup'},'message_id':grant['response']}})
        self.bot.airdrops.command(dict(self.msg,text='/airdrops 1'))
        text=self.send.call_args.args[1]
        self.assertIn('空投 #1',text)
        self.assertIn('发起人：Member',text)
        self.assertIn('同意人：Approver',text)
        self.assertIn('中奖者：',text)
        self.assertIn('区块哈希：<code>'+('a'*64),text)

    def test_history_rejects_nonmembers_and_invalid_ids(self):
        self.api.members[7]={'user':{'id':7,'is_bot':False},'status':'left'}
        self.bot.airdrops.command(dict(self.msg,text='/airdrops',**{'from':{'id':7}}))
        self.assertIn('只有当前管理群成员',self.send.call_args.args[1])
        self.bot.airdrops.command(dict(self.msg,text='/airdrops 0'))
        self.assertIn('正整数',self.send.call_args.args[1])

    def test_member_can_request_only_once_each_day_even_if_request_is_pending(self):
        msg=dict(self.msg,chat={'id':-1,'type':'supergroup'},
                 **{'from':{'id':2,'first_name':'Member'}})
        self.bot.airdrops.command(msg)
        self.bot.airdrops.command(dict(msg,message_id=2))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrop_grants WHERE actor=2').fetchone()[0],1)
        self.assertIn('正在等待管理员处理',self.send.call_args.args[1])
    def test_no_candidates(self):
        self.bot.airdrops.command(dict(self.msg,text='/airdrop 999 礼品'))
        self.assertEqual(self.bot.db.execute('SELECT status FROM airdrops').fetchone()[0],'empty')
        self.assertFalse(any(c.args[0]==-1 for c in self.send.call_args_list))
    def test_member_without_tag_is_excluded_even_above_threshold(self):
        self.api.members[2]={'user':{'id':2,'is_bot':False,'first_name':'NoTag'},'status':'member'}
        self.bot.airdrops.command(self.msg)
        self.assertEqual(self.bot.db.execute('SELECT status FROM airdrops').fetchone()[0],'empty')

    def test_member_with_custom_title_is_eligible(self):
        self.api.members[2]={'user':{'id':2,'is_bot':False,'first_name':'Titled'},'status':'administrator','custom_title':'创始人'}
        self.bot.airdrops.command(self.msg)
        self.assertEqual(self.bot.db.execute('SELECT uid FROM airdrop_winners').fetchone()[0],2)

    def test_blank_tag_does_not_count(self):
        self.api.members[2]={'user':{'id':2,'is_bot':False,'first_name':'Blank'},'status':'member','tag':'   '}
        self.bot.airdrops.command(self.msg)
        self.assertEqual(self.bot.db.execute('SELECT status FROM airdrops').fetchone()[0],'empty')

    def test_tagged_candidate_wins_when_others_lack_titles(self):
        # uid 2 (10 分) has no tag, uid 3 (9 分) has a tag but misses the 10 分 floor.
        self.api.members[2]={'user':{'id':2,'is_bot':False,'first_name':'NoTag'},'status':'member'}
        self.bot.db.execute('UPDATE point_users SET total=20 WHERE uid=3')
        self.bot.db.commit()
        self.bot.airdrops.command(self.msg)
        self.assertEqual(self.bot.db.execute('SELECT uid FROM airdrop_winners').fetchone()[0],3)

    def test_pending_verification_excluded(self):
        with self.bot.db:self.bot.db.execute("INSERT INTO verification(uid,kind) VALUES(2,'member')")
        self.bot.airdrops.command(self.msg)
        self.assertEqual(self.bot.db.execute('SELECT status FROM airdrops').fetchone()[0],'empty')
    def test_send_failure_preserves_winner(self):
        self.send.side_effect=APIError('sendMessage',{'error_code':500})
        with self.assertRaises(APIError):self.bot.airdrops.command(self.msg)
        winner=self.bot.db.execute('SELECT uid FROM airdrop_winners ORDER BY ordinal LIMIT 1').fetchone()[0]
        self.send.side_effect=None
        self.bot.airdrops.command(self.msg)
        self.assertEqual(self.bot.db.execute('SELECT uid FROM airdrop_winners ORDER BY ordinal LIMIT 1').fetchone()[0],winner)

    def test_airdrop_history_alias_is_rejected(self):
        self.assertFalse(self.bot.airdrops.command(dict(self.msg,text='/airdrop_history')))
    def test_private_and_group_dispatch(self):
        self.bot.handle({'update_id':1,'message':self.msg})
        self.block.return_value={'hash':'b'*64,'height':124,'provider':'test','chain':'Base'}
        msg=dict(self.msg,chat={'id':-1,'type':'supergroup'},message_id=2)
        self.bot.handle({'update_id':2,'message':msg})
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],2)

    def test_same_block_prevents_second_airdrop(self):
        self.bot.airdrops.command(self.msg)
        self.bot.airdrops.command(dict(self.msg,message_id=2))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],1)
        self.assertIn('等待新区块',self.send.call_args.args[1])
    def test_block_failure_creates_nothing(self):
        from chain import BlockUnavailable
        self.block.side_effect=BlockUnavailable('暂时无法获取区块')
        self.bot.airdrops.command(self.msg)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],0)
    def test_retry_does_not_fetch_another_block(self):
        self.bot.airdrops.command(self.msg)
        self.bot.airdrops.command(self.msg)
        self.assertEqual(self.block.call_count,1)
    def test_admin_daily_limit_needs_owner_grant(self):
        msg=dict(self.msg,**{'from':{'id':8,'first_name':'Mod'}})
        self.bot.airdrops.command(msg)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],1)
        self.block.return_value={'hash':'b'*64,'height':124,'provider':'test','chain':'Base'}
        self.bot.airdrops.command(dict(msg,message_id=2))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],1)
        grant=self.bot.db.execute('SELECT * FROM airdrop_grants').fetchone()
        self.assertEqual(grant['status'],'pending')
        # The authorisation prompt carries buttons, so it must outlive the auto-delete timer.
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM deletions').fetchone()[0],0)
        # A non-owner cannot approve.
        query_message={'chat':{'id':-1,'type':'supergroup'},'message_id':grant['response']}
        self.bot.handle({'update_id':9,'callback_query':{'id':'q','from':{'id':8},'data':f'a:ok:{grant["id"]}', 'message':query_message}})
        self.assertEqual(self.bot.db.execute('SELECT status FROM airdrop_grants').fetchone()[0],'pending')
        # Owner approval executes this airdrop immediately without re-sending.
        self.bot.handle({'update_id':10,'callback_query':{'id':'q','from':{'id':1,'first_name':'Owner'},'data':f'a:ok:{grant["id"]}', 'message':query_message}})
        self.assertEqual(self.bot.db.execute('SELECT status FROM airdrop_grants').fetchone()[0],'used')
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],2)
        # The next request needs a fresh grant again.
        self.block.return_value={'hash':'c'*64,'height':125,'provider':'test','chain':'Base'}
        self.bot.airdrops.command(dict(msg,message_id=3))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],2)
    def test_owner_unlimited_airdrops(self):
        self.bot.airdrops.command(self.msg)
        self.block.return_value={'hash':'b'*64,'height':124,'provider':'test','chain':'Base'}
        self.bot.airdrops.command(dict(self.msg,message_id=2))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],2)
    def test_owner_can_deny_extra_airdrop(self):
        msg=dict(self.msg,**{'from':{'id':8,'first_name':'Mod'}})
        self.bot.airdrops.command(msg)
        self.block.return_value={'hash':'b'*64,'height':124,'provider':'test','chain':'Base'}
        self.bot.airdrops.command(dict(msg,message_id=2))
        grant=self.bot.db.execute('SELECT * FROM airdrop_grants').fetchone()
        self.send.reset_mock()
        self.bot.handle({'update_id':11,'callback_query':{'id':'q','from':{'id':1,'first_name':'Owner'},'data':f'a:no:{grant["id"]}',
                         'message':{'chat':{'id':-1,'type':'supergroup'},'message_id':grant['response']}}})
        self.assertEqual(self.bot.db.execute('SELECT status FROM airdrop_grants').fetchone()[0],'denied')
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM airdrops').fetchone()[0],1)
        notices=[c.args[1] for c in self.send.call_args_list if '拒绝' in str(c.args[1])]
        self.assertTrue(notices)
        self.bot.airdrops.command(dict(msg,message_id=3))
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM airdrop_grants WHERE status='pending'").fetchone()[0],1)
