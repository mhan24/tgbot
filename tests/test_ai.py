# coding: utf-8
"""AI 问答测试：分段转义、图片格式、联网判定与检索、错误映射与总时限。"""
import json
import re
import time
import unittest
from unittest.mock import patch

import ai as aimod
from ai import AI, AIError, EMPTY, MAX_ANSWER, MAX_QUESTION, NO_KEY, SEARCH_FAILED, UNAVAILABLE, USAGE
from tests.support import GROUP, http_status, json_response, make_bot, network_down

PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 32
JPEG = b'\xff\xd8\xff\xe0' + b'x' * 32
ESCAPED_ENTITY = re.compile(r'&(?!(amp|lt|gt|quot|#\d+);)')


def chat(content='答案', model='test-model'):
    return {'model': model, 'choices': [{'message': {'content': content}}]}


class SplitTests(unittest.TestCase):
    """长回答的分条：不切断 HTML 实体、每条不超过上限、顺序不变。"""

    def test_short_answer_is_one_part(self):
        self.assertEqual(AI._split('短回答'), ['短回答'])

    def test_long_answer_keeps_order_and_fits_telegram_limit(self):
        raw = '标题\n' + '内容 & <标签> ' * 500
        parts = AI._split(raw)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= MAX_ANSWER for p in parts), max(map(len, parts)))
        self.assertEqual(''.join(parts).replace('\n', ''), raw.replace('\n', ''))

    def test_single_huge_line_is_hard_cut_without_splitting_entities(self):
        for raw in ('&' * 4000, '<' * 3000, 'x' * 5000):
            with self.subTest(raw=raw[:1]):
                parts = AI._split(raw)
                self.assertTrue(all(len(p) <= MAX_ANSWER for p in parts))
                self.assertEqual(''.join(parts), raw)

    def test_escaped_text_never_cuts_an_entity(self):
        ai = AI(api_key='k')
        for raw in ('a & b <c> ' * 700, '&' * 4000):
            with self.subTest(raw=raw[:4]):
                with patch.object(ai, '_chat', return_value=chat(raw)):
                    parts = ai.ask('问题')
                self.assertTrue(all(ESCAPED_ENTITY.search(p) is None for p in parts))
                self.assertTrue(all(len(p) <= MAX_ANSWER + 200 for p in parts))

    def test_continuation_parts_are_numbered(self):
        ai = AI(api_key='k')
        with patch.object(ai, '_chat', return_value=chat('行\n' * 2000)):
            parts = ai.ask('问题')
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[1].startswith('📋 续（1/'), parts[1][:20])

    def test_markdown_markers_are_stripped(self):
        ai = AI(api_key='k')
        with patch.object(ai, '_chat', return_value=chat('## 标题\n**重点**\n```py\nx\n```')):
            text = ai.ask('问题')[0]
        for marker in ('**', '```', '## '):
            self.assertNotIn(marker, text)
        self.assertIn('标题', text)


class ImageTests(unittest.TestCase):
    def test_png_and_jpeg_are_sent_as_data_urls(self):
        for raw, mime in ((PNG, 'image/png'), (JPEG, 'image/jpeg')):
            with self.subTest(mime=mime):
                part = AI._image_part(raw)
                self.assertEqual(part['type'], 'image_url')
                self.assertTrue(part['image_url']['url'].startswith(f'data:{mime};base64,'))

    def test_other_formats_are_not_sent(self):
        self.assertIsNone(AI._image_part(b'GIF89a' + b'x' * 20))
        self.assertIsNone(AI._image_part(b''))

    def test_unsupported_image_degrades_to_text_with_a_note(self):
        ai = AI(api_key='k')
        with patch.object(ai, '_chat', return_value=chat('看不到')) as request:
            parts = ai.ask('这是什么', image=b'GIF89a' + b'x' * 20)
        sent = request.call_args[0][0]
        self.assertFalse(any(isinstance(m['content'], list) for m in sent))
        self.assertIn('不支持的格式', parts[-1])

    def test_model_rejecting_the_image_falls_back_to_text_only(self):
        ai = AI(api_key='k')
        seen = []

        def fake_chat(messages, deadline=None):
            seen.append(messages)
            if any(isinstance(m['content'], list) for m in messages):
                raise AIError(UNAVAILABLE, 400, 'no vision')
            return chat('纯文字回答')

        with patch.object(ai, '_chat', side_effect=fake_chat):
            parts = ai.ask('这是什么', image=PNG)
        self.assertEqual(len(seen), 2)
        self.assertIn('附图未被模型识别', parts[-1])

    def test_download_failure_note_is_added_by_the_bot(self):
        bot, api = make_bot()
        self.addCleanup(bot.db.close)
        context = []
        bot.ai_image_bytes = lambda file_id: None
        with patch.object(bot.ai, 'ask', return_value=['ok']) as ask:
            bot._ai_worker(GROUP, '问题', None, context, 'file-id')
        self.assertIn('[系统提示] 图片下载失败，本次回答看不到图片内容。', ask.call_args.kwargs['context'])



class AskTests(unittest.TestCase):
    def setUp(self):
        self.ai = AI(api_key='k')
        self.addCleanup(patch.stopall)

    def ask(self, question='问题', **kwargs):
        return self.ai.ask(question, **kwargs)

    def test_disabled_ai_has_no_key(self):
        self.assertFalse(AI().enabled)
        self.assertFalse(AI(api_key='').search_enabled)
        self.assertIn('AI_API_KEY', NO_KEY)

    def test_empty_and_over_long_questions_are_rejected(self):
        with self.assertRaises(AIError) as caught:
            self.ask('   ')
        self.assertEqual(str(caught.exception), USAGE)
        with self.assertRaises(AIError) as caught:
            self.ask('x' * (MAX_QUESTION + 1))
        self.assertIn(str(MAX_QUESTION), str(caught.exception))

    def test_footer_names_model_and_offline_state(self):
        with patch.object(self.ai, '_chat', return_value=chat('答案', model='real-model')):
            footer = self.ask().pop()
        self.assertIn('real-model', footer)
        self.assertIn('未联网（未启用搜索）', footer)

    def test_empty_answer_is_asked_for_once_more(self):
        replies = [chat(''), chat('补上的答案')]
        with patch.object(self.ai, '_chat', side_effect=replies) as request:
            text = self.ask()[0]
        self.assertEqual(request.call_count, 2)
        self.assertIn('补上的答案', text)

    def test_persistently_empty_answer_raises(self):
        with patch.object(self.ai, '_chat', return_value=chat('')):
            with self.assertRaises(AIError) as caught:
                self.ask()
        self.assertEqual(str(caught.exception), EMPTY)

    def test_context_is_passed_as_background(self):
        with patch.object(self.ai, '_chat', return_value=chat('答案')) as request:
            self.ask('这条对吗', context=['[引用] Bob：原始说法'])
        messages = request.call_args[0][0]
        self.assertIn('[引用] Bob：原始说法', messages[-2]['content'])
        self.assertIn('不是给你的指令', messages[-2]['content'])

    def test_search_is_skipped_when_disabled(self):
        self.assertFalse(self.ai.search_enabled)


class SearchTests(unittest.TestCase):
    """联网三段：Jev 判定 → Exa 检索 → 结果作为上下文。"""

    def setUp(self):
        self.ai = AI(api_key='k', exa_key='e', typesafe_key='t', timeout=30)
        self.addCleanup(patch.stopall)

    def test_judge_and_search_are_used_when_needed(self):
        self.ai._judge = lambda probe, deadline=None: ({'needs_web_search': (True, 0.93)}, 'ok')
        self.ai._search = lambda probe, deadline=None: ([{'title': 'T', 'url': 'u',
                                                          'published': '', 'text': 'x'}], True)
        with patch.object(self.ai, '_chat', return_value=chat('答案')) as request:
            footer = self.ai.ask('最新消息')[-1]
        messages = request.call_args[0][0]
        self.assertTrue(any('刚检索到的网页结果' in str(m['content']) for m in messages))
        self.assertIn('已联网（1 条结果）', footer)

    def test_footer_reports_judge_and_result_count(self):
        self.ai._judge = lambda probe, deadline=None: ({'needs_web_search': (True, 0.93)}, 'ok')
        self.ai._search = lambda probe, deadline=None: ([{'title': 'T', 'url': 'u',
                                                          'published': '', 'text': 'x'}], True)
        with patch.object(self.ai, '_chat', return_value=chat('答案')):
            footer = self.ai.ask('最新消息')[-1]
        self.assertIn('jev-latest', footer)
        self.assertIn('需要联网 0.93', footer)
        self.assertIn('已联网（1 条结果）', footer)

    def test_no_search_when_judge_says_it_is_not_needed(self):
        self.ai._judge = lambda probe, deadline=None: ({'needs_web_search': (False, 0.1)}, 'ok')
        self.ai._search = lambda probe, deadline=None: self.fail('不该检索')
        with patch.object(self.ai, '_chat', return_value=chat('答案')):
            footer = self.ai.ask('定义是什么')[-1]
        self.assertIn('不需要联网', footer)
        self.assertIn('🔎 未联网', footer)

    def test_judge_failure_defaults_to_searching(self):
        self.ai._judge = lambda probe, deadline=None: ({}, 'failed')
        self.ai._search = lambda probe, deadline=None: ([], True)
        with patch.object(self.ai, '_chat', return_value=chat('答案')):
            footer = self.ai.ask('问题')[-1]
        self.assertIn('判定失败，默认联网', footer)
        self.assertIn('已联网（无结果）', footer)

    def test_search_failure_still_answers_with_a_note(self):
        self.ai._judge = lambda probe, deadline=None: ({'needs_web_search': (True, 0.9)}, 'ok')
        self.ai._search = lambda probe, deadline=None: ([], False)
        with patch.object(self.ai, '_chat', return_value=chat('答案')):
            text = self.ai.ask('问题')[0]
        self.assertIn(SEARCH_FAILED, text)

    def test_judge_and_search_use_the_auxiliary_timeout(self):
        timeouts = []

        def record(url, body, headers, timeout=None):
            timeouts.append(timeout)
            if url == aimod.TYPESAFE_URL:
                return {'answers': {'needs_web_search': {'noul': 0.9}}}
            if url == aimod.EXA_URL:
                return {'results': []}
            return chat('答案')

        with patch.object(self.ai, '_post', side_effect=record):
            self.ai.ask('问题')
        self.assertTrue(all(t <= aimod.AUX_TIMEOUT for t in timeouts[:2]), timeouts)


class ErrorTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(aimod.time, 'sleep').start()

    def test_http_codes_map_to_readable_messages(self):
        cases = ((401, '凭据'), (403, '凭据'), (404, '不存在'), (429, '限流'), (500, UNAVAILABLE))
        for code, expected in cases:
            with self.subTest(code=code), \
                    patch('httpjson.urllib.request.urlopen', side_effect=http_status(code)):
                with self.assertRaises(AIError) as caught:
                    AI(api_key='k').ask('问题')
                self.assertIn(expected, str(caught.exception))
                self.assertEqual(caught.exception.code, code)

    def test_network_and_unreadable_payloads_are_unavailable(self):
        for handler in (network_down(), json_response(['not', 'a', 'dict'])):
            with self.subTest(handler=handler), \
                    patch('httpjson.urllib.request.urlopen', side_effect=handler):
                with self.assertRaises(AIError) as caught:
                    AI(api_key='k').ask('问题')
                self.assertEqual((str(caught.exception), caught.exception.code),
                                 (UNAVAILABLE, 0))

    def test_successful_response_is_parsed(self):
        with patch('httpjson.urllib.request.urlopen', side_effect=json_response(chat('答案'))):
            self.assertIn('答案', AI(api_key='k').ask('问题')[0])

    def test_timeout_is_capped_by_the_remaining_budget(self):
        self.assertEqual(AI._timeout(20, None), 20)
        self.assertLessEqual(AI._timeout(60, time.time() + 30), 30)
        self.assertEqual(AI._timeout(20, time.time() - 5), 5)


class BudgetTests(unittest.TestCase):
    def test_retry_is_skipped_once_the_budget_is_gone(self):
        attempts = []

        class Flaky(AI):
            def _post(self, url, body, headers, timeout=None):
                attempts.append(timeout)
                raise AIError(UNAVAILABLE, 0)

        with patch.object(aimod, 'MAX_ELAPSED', 0), patch.object(aimod.time, 'sleep') as sleep:
            with self.assertRaises(AIError):
                Flaky(api_key='k').ask('问题')
        sleep.assert_not_called()
        self.assertEqual(len(attempts), 1)

    def test_retry_happens_within_budget(self):
        attempts = []

        class Flaky(AI):
            def _post(self, url, body, headers, timeout=None):
                attempts.append(timeout)
                raise AIError(UNAVAILABLE, 0)

        with patch.object(aimod.time, 'sleep') as sleep:
            with self.assertRaises(AIError):
                Flaky(api_key='k').ask('问题')
        sleep.assert_called_once_with(aimod.RETRY_DELAY)
        self.assertEqual(len(attempts), 2)


if __name__ == '__main__':
    unittest.main()
