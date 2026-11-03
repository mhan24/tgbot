import sqlite3
import unittest
from bot import Bot, APIError
from unittest.mock import patch
from test_bot import FakeAPI


class Tests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI();self.db=sqlite3.connect(':memory:')
        self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2'},self.api,self.db)
        self.addCleanup(self.db.close)

    def test_visible_and_empty_results_record_source_without_photo_identifiers(self):
        for count,photos,expected,result in ((1,[[{'file_id':'secret-file-id'}]],True,'visible'),(0,[],False,'invisible')):
            self.api.photos={'total_count':count,'photos':photos}
            with self.assertLogs('groupbot',level='INFO') as logs:
                self.assertIs(self.bot.avatar_visible(2,message_id=77,purpose='checkin'),expected)
            output=' '.join(logs.output)
            for field in ('group=-1','uid=2','purpose=checkin','message_id=77',f'total_count={count}',f'result={result}'):
                self.assertIn(field,output)
            self.assertNotIn('secret-file-id',output)

    def test_error_and_malformed_data_are_unknown_and_do_not_leak_response(self):
        self.api.failure='getUserProfilePhotos'
        with self.assertLogs('groupbot',level='INFO') as logs:
            self.assertIsNone(self.bot.avatar_visible(2,88,'checkin'))
        self.assertIn('reason=api_error api_code=500',' '.join(logs.output))
        self.api.failure=None;self.api.photos={'total_count':'private-token','photos':'sensitive-response'}
        with self.assertLogs('groupbot',level='INFO') as logs:
            self.assertIsNone(self.bot.avatar_visible(2,89,'checkin'))
        output=' '.join(logs.output)
        self.assertIn('reason=malformed_response',output)
        self.assertNotIn('private-token',output);self.assertNotIn('sensitive-response',output)

    def test_checkin_and_speech_records_have_distinct_purposes_and_real_message_ids(self):
        self.api.photos={'total_count':0,'photos':[]}
        with self.assertLogs('groupbot',level='INFO') as logs:
            self.bot.points_command({'from':{'id':2},'message_id':31,'text':'/checkin'})
        self.assertIn('purpose=checkin message_id=31',' '.join(logs.output))
        with self.assertLogs('groupbot',level='INFO') as logs:
            self.bot.handle({'update_id':32,'message':{'chat':{'id':-1,'type':'supergroup'},'from':{'id':2},'message_id':32,'text':'hello'}})
        self.assertIn('purpose=message message_id=32',' '.join(logs.output))
        self.assertEqual(self.bot.points.stats(2)[0],0)
        self.assertEqual(self.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone()[0],1)

    def test_empty_profile_list_with_private_chat_photo_is_visible_and_no_warning(self):
        self.api.photos={'total_count':0,'photos':[]}
        original=self.api.call
        def call(method,**data):
            if method=='getChat':return {'id':2,'type':'private','photo':{'small_file_id':'private-secret-photo'}}
            return original(method,**data)
        with patch.object(self.api,'call',side_effect=call), self.assertLogs('groupbot',level='INFO') as logs:
            self.bot.points_command({'from':{'id':2},'message_id':40,'text':'/checkin'})
        self.assertGreater(self.bot.points.stats(2)[0],0)
        self.assertIsNone(self.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone())
        output=' '.join(logs.output)
        self.assertIn('result=visible reason=chat_photo',output)
        self.assertIn('chat_photo=True chat_status=ok',output)
        self.assertNotIn('private-secret-photo',output)

    def test_private_chat_lookup_failure_is_unknown_and_never_warns(self):
        self.api.photos={'total_count':0,'photos':[]};self.api.failure='getChat'
        with self.assertLogs('groupbot',level='INFO') as logs:
            self.bot.points_command({'from':{'id':2},'message_id':41,'text':'/checkin'})
        self.assertEqual(self.bot.points.stats(2)[0],0)
        self.assertIsNone(self.db.execute('SELECT count FROM warnings WHERE uid=2').fetchone())
        self.assertIn('result=unknown reason=chat_lookup_error api_code=500',' '.join(logs.output))

    def test_inconsistent_list_and_wrong_chat_identity_do_not_warn(self):
        self.api.photos={'total_count':1,'photos':[]}
        with self.assertLogs('groupbot',level='INFO') as logs:
            self.assertIsNone(self.bot.avatar_visible(2,42,'checkin'))
        self.assertIn('reason=inconsistent_response',' '.join(logs.output))
        self.api.photos={'total_count':0,'photos':[]}
        original=self.api.call
        def call(method,**data):
            if method=='getChat':return {'id':-1,'type':'supergroup','photo':{'small_file_id':'wrong-photo'}}
            return original(method,**data)
        with patch.object(self.api,'call',side_effect=call):
            self.assertIsNone(self.bot.avatar_visible(2,43,'checkin'))
