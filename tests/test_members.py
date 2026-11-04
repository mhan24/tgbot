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
        self.addCleanup(self.bot.db.close)
    def ban(self,value):
        """Resolve a target the same way /manage does, then run the action service."""
        try:
            target=self.bot.members.resolve(value)
        except ValueError:
            return False
        return self.bot.moderate('ban',target,1)
    def test_ban_name_case_insensitive_and_explicit_target(self):
        self.assertTrue(self.ban('@ALICE'))
        bans=[d for m,d in self.api.calls if m=='banChatMember']
        self.assertEqual([d['user_id'] for d in bans],[2])
        self.assertTrue(bans[0]['revoke_messages'])
    def test_unresolvable_usernames_are_not_banned(self):
        for case in ('renamed', 'removed_username'):
            with self.subTest(case=case):
                self.bot.members.remember(self.user)
                self.api.members[2]={'status':'member','user':self.user}
                self.api.calls.clear()
                if case == 'renamed':self.api.members[2]['user']={'id':2,'username':'renamed'}
                else:self.bot.members.observe({'message':{'chat':{'id':-1},'from':{'id':2},'text':'hi'}})
                self.assertFalse(self.ban('@Alice'))
                self.assertFalse(any(m=='banChatMember' for m,d in self.api.calls))
                with self.assertRaises(ValueError):self.bot.members.resolve('@Alice')
    def test_protected_targets_are_not_banned(self):
        self.api.members[2]['status']='administrator'
        self.assertFalse(self.ban('@Alice'))
        self.assertFalse(any(m=='banChatMember' for m,d in self.api.calls))
    def test_unknown_and_lookup_failure_do_not_ban(self):
        self.assertFalse(self.ban('@unknown'))
        self.api.failure='getChatMember'
        # A resolver failure must not fall through to an unverified ban.
        with self.assertRaises(Exception):self.bot.members.resolve('@Alice')
        self.assertFalse(any(m=='banChatMember' for m,d in self.api.calls))
    def test_mute_preserves_duration(self):
        self.assertTrue(self.bot.moderate('mute',2,1,seconds=1800))
        self.assertTrue(any(m=='restrictChatMember' and d['user_id']==2 for m,d in self.api.calls))
        self.assertIn('30 分钟',self.api.calls[-1][1]['text'])
