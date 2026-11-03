# coding: utf-8
import io
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from bot import API, APIError, Bot
from test_bot import FakeAPI


class Tests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI()
        self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,sqlite3.connect(':memory:'))
        self.addCleanup(self.bot.db.close)
    def test_invalid_api_payload_is_a_retryable_error(self):
        for payload in (b'invalid',b'[]',b'{"ok":true}'):
            with self.subTest(payload=payload), patch('bot.urllib.request.urlopen',return_value=io.BytesIO(payload)):
                with self.assertRaises(APIError) as exc:API('fake-token').call('getMe')
                self.assertEqual(exc.exception.code,503)
                self.assertNotIn('fake-token',str(exc.exception))
    def test_valid_api_payload_is_unchanged(self):
        payload=json.dumps({'ok':True,'result':{'id':99}}).encode()
        with patch('bot.urllib.request.urlopen',return_value=io.BytesIO(payload)):
            self.assertEqual(API('fake-token').call('getMe'),{'id':99})
    def test_permanent_expired_join_request_is_not_retried(self):
        self.bot.start_verification({'id':22},'request',22)
        self.bot.db.execute('UPDATE verification SET expires=0');self.bot.db.commit()
        original=self.api.call
        count=[]
        def expired(method,**kwargs):
            if method=='declineChatJoinRequest':
                count.append(1)
                raise APIError(method,{'error_code':403,'description':'Forbidden: user is deactivated'})
            return original(method,**kwargs)
        with patch.object(self.api,'call',side_effect=expired):self.bot.expire();self.bot.expire()
        self.assertEqual(len(count),1)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM verification').fetchone()[0],0)
    def test_permission_failure_keeps_join_request_for_recovery(self):
        self.bot.start_verification({'id':22},'request',22)
        self.bot.db.execute('UPDATE verification SET expires=0');self.bot.db.commit()
        original=self.api.call
        def forbidden(method,**kwargs):
            if method=='declineChatJoinRequest':raise APIError(method,{'error_code':403,'description':'not enough rights'})
            return original(method,**kwargs)
        with patch.object(self.api,'call',side_effect=forbidden):self.bot.expire()
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM verification').fetchone()[0],1)
    def test_background_failure_does_not_block_other_tasks(self):
        seen=[]
        def failure():seen.append('failed');raise RuntimeError('temporary')
        with patch.object(self.bot,'expire',side_effect=failure), patch.object(self.bot.attention,'tick',side_effect=lambda:seen.append('tick')):
            with patch('bot.time.time',return_value=100):self.bot.maintenance();self.bot.maintenance()
            self.assertEqual(seen.count('failed'),1)
            self.assertEqual(seen.count('tick'),2)
            with patch('bot.time.time',return_value=131):self.bot.maintenance()
            self.assertEqual(seen.count('failed'),2)
    def test_non_acknowledgement_errors_are_not_swallowed(self):
        with patch.object(self.api,'call',side_effect=APIError('banChatMember',{'error_code':400,'description':'query is too old'})):
            with self.assertRaises(APIError):self.bot.call('banChatMember',chat_id=-1,user_id=2)

    def test_whitelist_restore_failure_preserves_pending_state(self):
        self.bot.start_verification({'id':22},'request',22)
        self.bot.db.execute('UPDATE verification SET expires=0');self.bot.db.commit()
        with patch.object(self.bot,'whitelisted',return_value=True):
            self.api.failure='approveChatJoinRequest'
            self.bot.expire()
            self.assertEqual(self.bot.db.execute('SELECT kind FROM verification').fetchone()[0],'request')
            self.api.failure=None
            self.bot.expire()
        self.assertEqual(self.bot.db.execute('SELECT kind FROM verification').fetchone()[0],'approved')

    def test_admin_invited_bot_bypasses_verification(self):
        self.bot.bot_id=99
        self.bot.joined({'id':30,'is_bot':True,'first_name':'Test Bot'},{'id':1})
        self.assertFalse(any(m in ('banChatMember','restrictChatMember') for m,d in self.api.calls))
        self.assertEqual(self.bot.db.execute('SELECT approved FROM known_bots WHERE uid=30').fetchone()[0],1)
    def test_regular_inviter_bot_is_rejected(self):
        self.bot.bot_id=99
        self.bot.joined({'id':30,'is_bot':True},{'id':2})
        self.assertTrue(any(m=='banChatMember' and d['user_id']==30 for m,d in self.api.calls))
    def test_bot_membership_event_checks_inviter(self):
        self.bot.bot_id=99
        user={'id':30,'is_bot':True}
        event={'chat':{'id':-1},'from':{'id':1},'old_chat_member':{'status':'left','user':user},'new_chat_member':{'status':'member','user':user}}
        self.bot.handle({'chat_member':event})
        self.assertEqual(self.bot.db.execute('SELECT inviter,approved FROM known_bots WHERE uid=30').fetchone()[:],(1,1))
