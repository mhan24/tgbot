import sqlite3
import unittest
from unittest.mock import patch
from bot import Bot
from test_bot import FakeAPI


class Tests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI();self.db=sqlite3.connect(':memory:')
        self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,self.db)
        self.bot.bot_id=99;self.bot.username='testbot'
        self.addCleanup(self.db.close)

    def message(self,text,uid=1,mid=1,private=True):
        return {'chat':{'id':uid if private else -1,'type':'private' if private else 'supergroup'},'from':{'id':uid,'first_name':'Fictional'},'message_id':mid,'text':text}

    def link(self,uid=2):
        original=self.api.call
        def call(method,**data):
            if method=='createChatInviteLink':
                self.assertTrue(data['creates_join_request'])
                return {'invite_link':f'https://t.me/+fictional{uid}'}
            return original(method,**data)
        with patch.object(self.api,'call',side_effect=call):self.bot.invitations.command(self.message('/invite',uid))
        return f'https://t.me/+fictional{uid}'

    def event(self,uid,link=None,**flags):
        event={'chat':{'id':-1},'from':{'id':99,'is_bot':True},'old_chat_member':{'status':'left'},'new_chat_member':{'status':'member','user':{'id':uid}},**flags}
        if link:event['invite_link']={'invite_link':link}
        return event

    def test_personal_link_is_visible_reused_and_join_rewards_owner(self):
        link=self.link()
        with patch.object(self.api,'call',wraps=self.api.call) as api:
            self.bot.invitations.command(self.message('/invite',2,2))
            self.assertFalse(any(c.args[0]=='createChatInviteLink' for c in api.call_args_list))
        self.assertIn(link,self.api.calls[-1][1]['text'])
        for uid in range(10,21):self.bot.handle({'chat_member':self.event(uid,link)})
        self.bot.handle({'chat_member':self.event(10,link)})
        self.assertEqual(self.bot.points.stats(2)[0],10)
        self.assertEqual(self.bot.points.stats(99)[0],0)

    def test_join_request_attribution_survives_missing_link_after_approval(self):
        link=self.link()
        self.bot.invitations.request({'from':{'id':10},'invite_link':{'invite_link':link}})
        self.bot.handle({'chat_member':self.event(10,via_join_request=True)})
        self.assertEqual(self.bot.points.stats(2)[0],1)
        self.bot.handle({'chat_member':self.event(11,'https://t.me/+unknown',via_join_request=True)})
        self.assertEqual(self.bot.points.stats(2)[0],1)

    def test_link_permission_and_timeout_do_not_create_or_award(self):
        self.api.members[2]={'status':'left'}
        self.bot.invitations.command(self.message('/invite',2))
        self.assertEqual(self.db.execute('SELECT count(*) FROM invite_links').fetchone()[0],0)
        self.api.members[2]={'status':'member'}
        self.api.failure='createChatInviteLink'
        self.bot.invitations.command(self.message('/invite',2,2))
        self.assertEqual(self.bot.points.stats(2)[0],0)
        self.assertIn('暂时无法',self.api.calls[-1][1]['text'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM invite_links').fetchone()[0],0)
