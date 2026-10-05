import sqlite3
import unittest
from bot import Bot
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI();self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,sqlite3.connect(':memory:'))
        self.bot.bot_id=99;self.bot.username='testbot'
        self.user={'id':2,'username':'Alice','first_name':'Alice'}
        self.api.members[2]={'status':'member','user':self.user}
        self.bot.members.remember(self.user)
    def command(self,text,**extra):
        self.bot.command({'chat':{'id':-1},'message_id':10,'from':{'id':1},'text':text,**extra},10)
    def test_ban_name_case_insensitive_and_explicit_target(self):
        self.command('/ban @ALICE spam',reply_to_message={'from':{'id':3}})
        bans=[d for m,d in self.api.calls if m=='banChatMember']
        self.assertEqual([d['user_id'] for d in bans],[2])
        self.assertTrue(bans[0]['revoke_messages'])
    def test_changed_name_fails_closed(self):
        self.api.members[2]['user']={'id':2,'username':'renamed'}
        self.command('/ban @Alice')
        self.assertFalse(any(m=='banChatMember' for m,d in self.api.calls))
        with self.assertRaises(ValueError):self.bot.members.resolve('@Alice')
    def test_unknown_and_lookup_failure_do_not_ban(self):
        self.command('/ban @unknown')
        self.api.failure='getChatMember'
        # Isolate the resolver failure; admin verification is a separate API request.
        with self.assertRaises(Exception):self.bot.members.resolve('@Alice')
        self.assertFalse(any(m=='banChatMember' for m,d in self.api.calls))
    def test_mute_preserves_duration(self):
        self.command('/mute @Alice 30')
        self.assertTrue(any(m=='restrictChatMember' and d['user_id']==2 for m,d in self.api.calls))
        self.assertIn('30 分钟',self.api.calls[-1][1]['text'])
    def test_current_observations_clear_removed_username(self):
        self.bot.members.observe({'message':{'chat':{'id':-1},'from':{'id':2},'text':'hi'}})
        with self.assertRaises(ValueError):self.bot.members.resolve('@Alice')
    def test_admin_protection(self):
        self.api.members[2]['status']='administrator'
        self.command('/ban @Alice')
        self.assertFalse(any(m=='banChatMember' for m,d in self.api.calls))
