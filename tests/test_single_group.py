import sqlite3
import unittest
from unittest.mock import patch
from bot import Bot
from test_bot import FakeAPI


class SingleGroupTests(unittest.TestCase):
    def make(self, db=None):
        db = db or sqlite3.connect(':memory:')
        bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2'}, FakeAPI(), db)
        self.addCleanup(db.close)
        return bot

    def test_current_state_survives_restart_without_legacy_storage(self):
        bot=self.make();db=bot.db
        with db:
            db.execute("INSERT INTO point_users VALUES(2,'Fictional',42)")
            db.execute("INSERT INTO point_events VALUES('test',2,'2026-10-30','message',1)")
            db.execute('INSERT INTO warnings(uid,count,mute_until) VALUES(2,2,0)')
            db.execute("INSERT INTO blacklist VALUES(3,1,1,'test')")
            db.execute("INSERT INTO group_settings VALUES('MESSAGE_DAILY_LIMIT','10')")
        current=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},bot.api,db)
        self.assertEqual(current.points.stats(2)[0],42)
        self.assertEqual(current.message_daily_limit,10)
        self.assertEqual(db.execute('SELECT count FROM warnings WHERE uid=2').fetchone()[0],2)
        self.assertTrue(current.blacklisted(3))
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables.intersection({'managed_groups','airdrops','news_state','event_forms','attention_flags','attention_cleared',
                                              'chat_history','ai_cooldown','ai_jobs','ai_media',
                                              'keyword_rewards','keyword_claims','keyword_forms','keyword_form_inputs',
                                              'treasures','treasure_entries','treasure_result_pages','treasure_forms','treasure_form_inputs'}))

    def test_other_groups_cannot_trigger_business_or_observation(self):
        bot = self.make()
        for update in (
            {'message':{'chat':{'id':-3,'type':'supergroup'},'from':{'id':2},'message_id':1,'text':'hello'}},
            {'chat_member':{'chat':{'id':-3},'from':{'id':2}}},
            {'chat_join_request':{'chat':{'id':-3},'from':{'id':2}}},
        ):
            with patch.object(bot.members,'observe') as observe:
                bot.handle(update)
                observe.assert_not_called()
        self.assertEqual(bot.api.calls,[])

    def test_private_queries_and_defaults_do_not_offer_group_selection(self):
        bot=self.make()
        self.assertFalse(hasattr(bot,'managed_bots'))
        bot.points_command({'chat':{'id':2,'type':'private'},'from':{'id':2},'message_id':1,'text':'/points'})
        reply=bot.api.calls[-1][1]
        self.assertIn('群组：-1',reply['text'])
        self.assertNotIn('reply_markup',reply)
        default = Bot({'CHANNEL_ID':'-2'}, FakeAPI(), sqlite3.connect(':memory:'))
        self.addCleanup(default.db.close)
        self.assertEqual(default.group,-1003014905211)
