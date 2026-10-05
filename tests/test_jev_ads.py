import io
import json
import sqlite3
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from bot import Bot
from jev_ads import JevAds, JevError, classify
from test_bot import FakeAPI


class Pool:
    def __init__(self, result=(True, .98, 'jev-test')):
        self.result = result
        self.calls = []
    def submit(self, fn, *args):
        self.calls.append(args)
        f = Future()
        if isinstance(self.result, Exception): f.set_exception(self.result)
        else: f.set_result(self.result)
        return f


class JevTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.db = sqlite3.connect(':memory:')
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2', 'JEV_API_KEY': 'test'}, self.api, self.db)
        self.bot.bot_id, self.bot.username = 99, 'testbot'
        self.guard = self.bot.jev
        self.guard.pool.shutdown()
        self.pool = Pool()
        self.guard.pool = self.pool
        self.target = {'message_id': 100, 'from': {'id': 2}, 'text': '代开VCC，联系我下单'}

    def report(self, uid=3, target=None, text='/report', gid=-1):
        msg = {'chat': {'id': gid, 'type': 'supergroup' if gid < 0 else 'private'},
               'message_id': 200, 'from': {'id': uid}, 'text': text,
               'reply_to_message': self.target if target is None else target}
        self.bot.handle({'update_id': 1, 'message': msg})

    def row(self):
        return self.db.execute('SELECT * FROM jev_cases ORDER BY id DESC LIMIT 1').fetchone()

    def ready(self):
        self.report()
        self.guard.work(); self.guard.work()
        return self.row()

    def vote(self, uid=3, action='yes', gid=-1, mid=None, rid=None):
        row = self.row()
        return self.guard.callback({'id': str(uid), 'from': {'id': uid},
               'data': f'jev:{action}:{gid}:{rid or row["id"]}',
               'message': {'chat': {'id': gid}, 'message_id': mid or row['card']}})

    def warned(self):
        row = self.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone()
        return row[0] if row else 0

    def test_no_monitoring_even_for_ad_text_and_edits(self):
        for kind in ('message', 'edited_message'):
            self.bot.handle({'update_id': 1, kind: dict(self.target, chat={'id': -1})})
        self.guard.work()
        self.assertEqual(self.pool.calls, [])
        self.assertIsNone(self.row())
        self.assertEqual(self.warned(), 0)

    def test_regular_member_and_admin_can_report(self):
        for uid in (3, 1): self.report(uid)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jev_cases').fetchone()[0], 1)
        self.guard.work(); self.guard.work()
        self.assertEqual(len(self.pool.calls), 1)
        self.assertEqual(self.row()['state'], 'waiting')
        self.assertEqual(self.warned(), 0)
        self.assertFalse(any(m == 'deleteMessage' for m, _ in self.api.calls))
        self.assertFalse(self.db.execute('SELECT 1 FROM deletions WHERE message_id=?', (self.row()['card'],)).fetchone())

    def test_admin_one_vote_and_replays_warn_once(self):
        self.ready(); self.vote(1)
        self.guard.work(); self.guard.work()
        self.vote(1); self.vote(3); self.guard.work()
        self.assertEqual(self.row()['state'], 'confirmed')
        self.assertEqual(self.warned(), 1)
        self.assertEqual(sum(m == 'deleteMessage' and d['message_id'] == 100 for m,d in self.api.calls), 1)

    def test_five_distinct_members_required(self):
        self.ready()
        for _ in range(5): self.vote(3)
        self.guard.work()
        self.assertEqual(self.warned(), 0)
        for uid in (4, 5, 6): self.vote(uid)
        self.guard.work(); self.assertEqual(self.warned(), 0)
        self.vote(7); self.guard.work()
        self.assertEqual(self.warned(), 1)

    def test_outsider_unverified_and_bot_cannot_vote(self):
        self.ready()
        self.api.members[3] = {'status': 'left'}
        self.api.members[4] = {'status': 'member', 'user': {'id': 4, 'is_bot': True}}
        with self.db: self.db.execute("INSERT INTO verification(uid,kind) VALUES(5,'member')")
        for uid in (3,4,5): self.vote(uid)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jev_votes').fetchone()[0], 0)

    def test_departed_votes_are_removed_at_threshold(self):
        self.ready()
        for uid in (3,4,5,6): self.vote(uid)
        self.api.members[3] = {'status': 'left'}
        self.vote(7); self.guard.work()
        self.assertEqual(self.warned(), 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jev_votes').fetchone()[0], 4)

    def test_membership_error_never_counts_vote(self):
        self.ready(); self.api.failure = 'getChatMember'
        self.vote(1)
        self.assertEqual(self.row()['state'], 'waiting')
        self.assertEqual(self.warned(), 0)

    def test_cross_group_and_wrong_card_rejected(self):
        self.ready(); self.vote(1, gid=-20); self.vote(1, mid=9999)
        self.assertEqual(self.row()['state'], 'waiting')
        self.assertEqual(self.db.execute('SELECT count(*) FROM jev_votes').fetchone()[0], 0)

    def test_only_admin_can_dismiss(self):
        self.ready(); self.vote(3, action='no')
        self.assertEqual(self.row()['state'], 'waiting')
        self.vote(1, action='no'); self.guard.work(); self.vote(1)
        self.assertEqual(self.row()['state'], 'dismissed')
        self.assertEqual(self.warned(), 0)

    def test_edit_invalidates_old_votes_without_new_api_call(self):
        row = self.ready(); self.vote(3)
        changed = dict(self.target, text='正常提问', chat={'id': -1})
        self.bot.handle({'update_id': 2, 'edited_message': changed})
        self.guard.work(); self.vote(1)
        self.assertEqual(self.row()['state'], 'superseded')
        self.assertEqual(len(self.pool.calls), 1)
        self.assertEqual(self.warned(), 0)
        self.report(target=changed)
        self.assertNotEqual(self.row()['id'], row['id'])
        self.assertEqual(self.row()['state'], 'queued')

    def test_normal_classification_does_not_create_vote(self):
        self.pool.result = (False, .02, 'jev-test')
        self.ready()
        self.assertEqual(self.row()['state'], 'clear')
        self.assertIsNone(self.row()['card'])
        self.assertEqual(self.warned(), 0)
        self.assertTrue(any('未判为广告' in d.get('text','') for m,d in self.api.calls))

    def test_invalid_response_retries_without_punishment(self):
        self.pool.result = JevError('error')
        self.report()
        for _ in range(3):
            with self.db: self.db.execute('UPDATE jev_cases SET next_try=0')
            self.guard.work(); self.guard.work()
        self.assertEqual(self.row()['state'], 'error')
        self.assertEqual(self.warned(), 0)
        self.report()
        self.assertEqual(self.row()['state'], 'queued')

    def test_vote_expiry_does_not_warn(self):
        self.ready()
        with self.db: self.db.execute('UPDATE jev_cases SET expires=0')
        self.vote(1); self.guard.work()
        self.assertEqual(self.row()['state'], 'expired')
        self.assertEqual(self.warned(), 0)

    def test_restart_preserves_votes_and_warning_idempotence(self):
        self.ready()
        for uid in (3,4,5,6): self.vote(uid)
        self.guard = JevAds(self.bot); self.guard.pool.shutdown(); self.guard.pool = self.pool
        self.vote(7); self.guard.work()
        self.assertEqual(self.warned(), 1)
        with self.db: self.db.execute("UPDATE jev_cases SET state='approving'")
        self.guard.work()
        self.assertEqual(self.warned(), 1)

    def test_failed_warning_delivery_does_not_increment_again(self):
        self.ready(); self.vote(1)
        self.api.failure = 'sendMessage'
        self.guard.work()
        self.assertEqual(self.warned(), 1)
        self.assertEqual(self.row()['state'], 'approving')
        self.api.failure = None
        with self.db: self.db.execute('UPDATE jev_cases SET next_try=0')
        self.guard.work()
        self.assertEqual(self.warned(), 1)
        self.assertEqual(self.row()['state'], 'confirmed')

    def test_admin_target_exempt_and_private_report_rejected(self):
        self.report(target=dict(self.target, **{'from': {'id':1}}))
        self.assertIsNone(self.row())
        self.report(gid=3)
        self.assertIsNone(self.row())

    def test_caption_and_embedded_link_submitted(self):
        target = {'message_id':101,'from':{'id':2},'caption':'点这里购买','caption_entities':[{'type':'text_link','url':'https://example.test'}]}
        self.report(target=target); self.guard.work()
        self.assertEqual(self.pool.calls[0][1:], ('点这里购买', ['https://example.test']))

    def test_deprecated_commands_are_absent_from_help(self):
        self.bot.help_command({'chat':{'id':-1},'message_id':201,'from':{'id':1},'text':'/help'})
        text = self.api.calls[-1][1]['text']
        self.assertIn('/report',text)
        for cmd in ('/ads','/adreview','/adok','/adno','/adwords'):
            self.assertNotIn(cmd,text)
            self.assertFalse(self.bot.command({'text':cmd,'from':{'id':1}},1))

    def test_api_contract_and_invalid_probability(self):
        valid = {'model':'jev-1.13.0','answers':{'advertising':{'type':'choice','choice':'ad','probabilities':{'ad':.99,'normal':.01}}}}
        with patch('jev_ads.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(valid).encode())) as req:
            self.assertEqual(classify(self.bot.cfg,'hello',[]),(True,.99,'jev-1.13.0'))
            sent = json.loads(req.call_args.args[0].data)
            self.assertEqual(sent['questions']['advertising']['type'],'choice')
            self.assertEqual(sent['state']['message'],'hello')
        valid['answers']['advertising']['probabilities']['ad'] = True
        with patch('jev_ads.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(valid).encode())):
            with self.assertRaises(JevError): classify(self.bot.cfg,'hello',[])
