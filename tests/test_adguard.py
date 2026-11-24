# coding: utf-8
"""广告检查：后台 Jev 判定、删除与警告、累计 3 次移出本群。"""
import unittest
from unittest.mock import patch

import ai as aimod
from adguard import MAX_WARNINGS
from tests.support import BOT_ID, GROUP, group_message, json_response, make_bot


class AdGuardTests(unittest.TestCase):
    def setUp(self):
        self.bot, self.api = make_bot(TYPESAFE_API_KEY='t')
        self.addCleanup(self.bot.db.close)
        self.verdicts = {}   # 文本 -> 概率；未列出的按 0.1
        self.judged = []

        def verdict(text, threshold):
            self.judged.append(text)
            probability = self.verdicts.get(text, 0.1)
            return probability >= threshold, probability

        patch.object(self.bot.ai, 'ad_verdict', side_effect=verdict).start()
        self.addCleanup(patch.stopall)

    def settle(self):
        """等后台判定跑完，再让主线程处置结果。"""
        self.bot.ads.jobs.join()
        self.bot.ads.drain()
        self.assertFalse(self.bot.ads.busy())

    def warnings(self, uid=2):
        row = self.bot.db.execute('SELECT count FROM ad_warnings WHERE uid=?', (uid,)).fetchone()
        return row[0] if row else 0

    def test_every_group_message_is_judged_and_normal_chat_is_left_alone(self):
        self.bot.handle(group_message(1, '今天天气不错'))
        self.settle()
        self.assertEqual(self.judged, ['今天天气不错'])
        self.assertEqual(self.api.find('deleteMessage'), [])
        self.assertEqual(self.warnings(), 0)

    def test_ad_is_deleted_and_warned_then_kicked_on_third_warning(self):
        self.verdicts['加V领福利'] = 0.96
        for mid in range(1, MAX_WARNINGS):
            self.bot.handle(group_message(mid, '加V领福利'))
            self.settle()
            self.assertEqual(self.warnings(), mid)
            self.assertIn(f'警告 {mid}/{MAX_WARNINGS}', self.api.texts()[-1])
            self.assertIn('jev-latest 判定值 0.96（阈值 0.95）', self.api.texts()[-1])
        self.assertEqual(self.api.find('banChatMember'), [])
        self.bot.handle(group_message(MAX_WARNINGS, '加V领福利'))
        self.settle()
        self.assertEqual([d['message_id'] for d in self.api.find('deleteMessage')],
                         list(range(1, MAX_WARNINGS + 1)))
        self.assertEqual(self.api.find('banChatMember'),
                         [{'chat_id': GROUP, 'user_id': 2, 'revoke_messages': True}])
        self.assertEqual(self.api.find('unbanChatMember'),
                         [{'chat_id': GROUP, 'user_id': 2, 'only_if_banned': True}])
        self.assertIn('移出本群', self.api.texts()[-1])
        self.assertIn('判定值 0.96', self.api.texts()[-1])
        self.assertEqual(self.warnings(), 0)
        actions = [r[0] for r in self.bot.db.execute('SELECT action FROM audit ORDER BY id')]
        self.assertEqual(actions, ['ad_warning'] * MAX_WARNINGS + ['ad_kick'])

    def test_below_threshold_is_not_an_ad(self):
        self.verdicts['有点像广告'] = 0.94
        self.bot.handle(group_message(1, '有点像广告'))
        self.settle()
        self.assertEqual(self.api.find('deleteMessage'), [])

    def test_threshold_comes_from_config(self):
        bot, _ = make_bot(TYPESAFE_API_KEY='t', AD_THRESHOLD='0.6')
        self.addCleanup(bot.db.close)
        self.assertEqual(bot.ads.threshold, 0.6)
        for value, expected in (('2', 0.99), ('0.1', 0.5), ('x', aimod.AD_THRESHOLD)):
            other, _ = make_bot(AD_THRESHOLD=value)
            self.addCleanup(other.db.close)
            self.assertEqual(other.ads.threshold, expected)

    def test_admins_are_not_warned(self):
        self.verdicts['广告'] = 0.99
        self.api.members[2] = {'status': 'administrator'}
        self.bot.handle(group_message(1, '广告'))
        self.settle()
        self.assertEqual(self.api.methods(), ['getChatMember'])

    def test_bots_channels_and_private_chats_are_not_judged(self):
        self.bot.handle(group_message(1, '广告', **{'from': {'id': 3, 'is_bot': True}}))
        self.bot.handle(group_message(2, '广告', sender_chat={'id': -10, 'type': 'channel'}))
        self.bot.handle(group_message(3, '广告', uid=BOT_ID))
        self.bot.handle(group_message(4, '广告', chat={'id': 4, 'type': 'private'}))
        self.settle()
        self.assertEqual(self.judged, [])

    def test_edited_caption_is_judged_but_one_message_warns_once(self):
        self.verdicts['广告'] = 0.99
        self.bot.handle(group_message(1, '广告'))
        self.settle()
        edited = group_message(1, None, caption='广告')['message']
        del edited['text']
        self.bot.handle({'update_id': 2, 'edited_message': edited})
        self.settle()
        self.assertEqual(self.judged, ['广告', '广告'])
        self.assertEqual(self.warnings(), 1)
        self.assertEqual(len(self.api.find('deleteMessage')), 2)

    def test_judgment_failure_is_not_an_ad(self):
        self.bot.ai.ad_verdict.side_effect = lambda text, threshold: None
        self.bot.handle(group_message(1, '广告'))
        self.settle()
        self.assertEqual(self.api.calls, [])

    def test_disabled_without_typesafe_key(self):
        bot, api = make_bot()
        self.addCleanup(bot.db.close)
        bot.handle(group_message(1, '广告'))
        self.assertFalse(bot.ads.busy())
        self.assertNotIn('广告检查', bot.help_text())
        self.assertIn('广告检查', self.bot.help_text())

    def test_busy_shortens_polling_until_results_are_handled(self):
        self.bot.handle(group_message(1, '你好'))
        self.assertTrue(self.bot.ads.busy())
        self.bot.ads.jobs.join()
        self.bot.maintenance()
        self.assertFalse(self.bot.ads.busy())


class AdCommandTests(unittest.TestCase):
    def setUp(self):
        self.bot, self.api = make_bot(TYPESAFE_API_KEY='t')
        self.addCleanup(self.bot.db.close)
        self.api.members[7] = {'status': 'administrator', 'user': {'id': 7, 'first_name': 'Admin'}}
        self.api.members[5] = {'status': 'member', 'user': {'id': 5, 'first_name': 'Five'}}
        self.judged = []

        def verdict(text, threshold):
            self.judged.append(text)
            ad = '广告' in text or '福利' in text
            return ad, 0.97 if ad else 0.05

        patch.object(self.bot.ai, 'ad_verdict', side_effect=verdict).start()
        self.addCleanup(patch.stopall)

    def command(self, text, uid=7, private=False, reply_uid=0):
        chat = {'id': uid, 'type': 'private'} if private else {'id': GROUP, 'type': 'supergroup'}
        extra = {'reply_to_message': {'message_id': 50, 'from': {'id': reply_uid, 'first_name': 'R'},
                                      'text': 'x'}} if reply_uid else {}
        self.bot.handle(group_message(100, text, uid=uid, chat=chat, **extra))
        self.bot.ads.jobs.join()
        self.bot.ads.drain()
        return self.api.texts()[-1]

    def warn(self, uid, count=1):
        with self.bot.db:
            self.bot.db.execute('INSERT OR REPLACE INTO ad_warnings VALUES(?,?,0)', (uid, count))

    def warnings(self, uid):
        row = self.bot.db.execute('SELECT count FROM ad_warnings WHERE uid=?', (uid,)).fetchone()
        return row[0] if row else 0

    def test_whitelist_by_reply_skips_checks_and_clears_warnings(self):
        self.warn(2, 2)
        self.assertIn('加入广告白名单', self.command('/whitelist add', reply_uid=2))
        self.assertIn('已清空', self.api.texts()[-1])
        self.assertEqual(self.warnings(2), 0)
        self.assertIn('已在广告白名单中', self.command('/whitelist add', reply_uid=2))
        self.judged.clear()
        self.bot.handle(group_message(1, '加V领福利', uid=2))
        self.bot.ads.jobs.join()
        self.bot.ads.drain()
        self.assertEqual(self.judged, [])
        self.assertIn('移出广告白名单', self.command('/whitelist remove', reply_uid=2))
        self.assertIn('不在广告白名单中', self.command('/whitelist remove', reply_uid=2))
        self.judged.clear()
        self.bot.handle(group_message(2, '加V领福利', uid=2))
        self.bot.ads.jobs.join()
        self.assertEqual(self.judged, ['加V领福利'])

    def test_whitelist_by_id_in_private_and_list(self):
        self.assertIn('空的', self.command('/whitelist list', private=True))
        self.assertIn('加入广告白名单', self.command('/whitelist add 5', private=True))
        self.assertEqual(self.command('/whitelist list', private=True),
                         '<b>广告白名单</b>\nFive <code>5</code>')
        self.assertIn('请回复', self.command('/whitelist add', private=True))
        self.assertIn('请回复', self.command('/whitelist add bob', private=True))
        self.assertIn('/whitelist add', self.command('/whitelist', private=True))

    def test_whitelisted_during_judgment_is_not_warned(self):
        self.bot.handle(group_message(1, '广告', uid=2))
        self.bot.ads.jobs.join()
        with self.bot.db:
            self.bot.db.execute("INSERT INTO ad_whitelist VALUES(2,'',7,0)")
        self.bot.ads.drain()
        self.assertEqual(self.api.find('deleteMessage'), [])

    def test_clearwarn_one_member_or_everyone(self):
        self.warn(2, 2)
        self.warn(3, 1)
        self.assertIn('已清空', self.command('/clearwarn', reply_uid=2))
        self.assertIn('2 次', self.api.texts()[-1])
        self.assertEqual(self.warnings(2), 0)
        self.assertEqual(self.warnings(3), 1)
        self.assertIn('没有广告警告', self.command('/clearwarn 2', private=True))
        self.assertIn('/clearwarn all', self.command('/clearwarn'))
        self.assertIn('1 人', self.command('/clearwarn all', private=True))
        self.assertEqual(self.warnings(3), 0)
        actions = [r[0] for r in self.bot.db.execute(
            'SELECT action FROM audit WHERE actor=7 ORDER BY id')]
        self.assertEqual(actions, ['ad_clear', 'ad_clear'])

    def test_non_admins_cannot_manage(self):
        for text in ('/whitelist add 5', '/whitelist list', '/clearwarn all'):
            for private in (False, True):
                self.assertIn('只有群主和群管理员', self.command(text, uid=2, private=private))
        self.assertFalse(self.bot.ads.whitelisted(5))

    def test_help_lists_admin_commands(self):
        text = self.bot.help_text()
        self.assertIn('/whitelist', text)
        self.assertIn('/clearwarn', text)


class AdVerdictTests(unittest.TestCase):
    def test_noul_probability_is_compared_with_threshold(self):
        ai = aimod.AI(api_key='k', typesafe_key='t')
        calls = []
        with patch('httpjson.urllib.request.urlopen',
                   side_effect=json_response({'answers': {'is_ad': {'noul': 0.96}}}, calls)):
            self.assertEqual(ai.ad_verdict('加群领福利'), (True, 0.96))
            self.assertEqual(ai.ad_verdict('加群领福利', 0.99), (False, 0.96))
        self.assertLessEqual(calls[0][1], aimod.AUX_TIMEOUT)

    def test_missing_key_empty_text_or_bad_answer_returns_none(self):
        self.assertIsNone(aimod.AI(api_key='k').ad_verdict('x'))
        ai = aimod.AI(api_key='k', typesafe_key='t')
        self.assertIsNone(ai.ad_verdict('  '))
        with patch('httpjson.urllib.request.urlopen',
                   side_effect=json_response({'answers': {}})):
            self.assertIsNone(ai.ad_verdict('x'))


if __name__ == '__main__':
    unittest.main()
