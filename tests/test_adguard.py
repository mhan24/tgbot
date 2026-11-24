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
        self.verdicts['加V领福利'] = 0.95
        for mid in range(1, MAX_WARNINGS):
            self.bot.handle(group_message(mid, '加V领福利'))
            self.settle()
            self.assertEqual(self.warnings(), mid)
            self.assertIn(f'警告 {mid}/{MAX_WARNINGS}', self.api.texts()[-1])
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
        self.assertEqual(self.warnings(), 0)
        actions = [r[0] for r in self.bot.db.execute('SELECT action FROM audit ORDER BY id')]
        self.assertEqual(actions, ['ad_warning'] * MAX_WARNINGS + ['ad_kick'])

    def test_below_threshold_is_not_an_ad(self):
        self.verdicts['有点像广告'] = 0.7
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


class AdVerdictTests(unittest.TestCase):
    def test_noul_probability_is_compared_with_threshold(self):
        ai = aimod.AI(api_key='k', typesafe_key='t')
        calls = []
        with patch('httpjson.urllib.request.urlopen',
                   side_effect=json_response({'answers': {'is_ad': {'noul': 0.85}}}, calls)):
            self.assertEqual(ai.ad_verdict('加群领福利'), (True, 0.85))
            self.assertEqual(ai.ad_verdict('加群领福利', 0.9), (False, 0.85))
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
