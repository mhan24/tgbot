import io
import json
import unittest
from unittest.mock import patch
from context_filter import filter_context, PREFIX, SEPARATOR, BACKGROUND

class Tests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'JEV_API_KEY': 'test'}
        self.material = {'引用消息': '那会通知吗？', '引用对话链（按先后顺序，优先用于理解引用）': ['补货会通知'],
                         BACKGROUND: '[消息 #1] 用户: 补货通知\n补充说明\n[消息 #2] 用户: 天气不错'}
        self.prompt = PREFIX + json.dumps(self.material, ensure_ascii=False) + SEPARATOR + '解释引用'
    def response(self, probability=.99):
        return io.BytesIO(json.dumps({'answers': {
            'm0': {'type': 'choice', 'choice': 'keep', 'probabilities': {'drop': .01}},
            'm1': {'type': 'choice', 'choice': 'drop', 'probabilities': {'drop': probability}}}}).encode())
    def test_selects_background_and_keeps_quote_chain(self):
        with patch('context_filter.urllib.request.urlopen', return_value=self.response()) as call:
            result = filter_context(self.cfg, self.prompt)
        self.assertNotIn('天气不错', result)
        self.assertIn('补充说明', result)
        self.assertIn('那会通知吗', result)
        self.assertIn('补货会通知', result)
        payload = json.loads(call.call_args.args[0].data)
        self.assertEqual(len(payload['questions']), 2)
        self.assertEqual(payload['state']['current_question'], '解释引用')
    def test_uncertainty_kept(self):
        with patch('context_filter.urllib.request.urlopen', return_value=self.response(.6)):
            self.assertIn('天气不错', filter_context(self.cfg, self.prompt))
    def test_failure_keeps_original(self):
        with patch('context_filter.urllib.request.urlopen', side_effect=TimeoutError()), self.assertLogs('ai-context'):
            self.assertEqual(filter_context(self.cfg, self.prompt), self.prompt)
        with patch('context_filter.urllib.request.urlopen', return_value=io.BytesIO(b'{}')), self.assertLogs('ai-context'):
            self.assertEqual(filter_context(self.cfg, self.prompt), self.prompt)
    def test_no_key_or_background_skips_request(self):
        with patch('context_filter.urllib.request.urlopen') as call:
            self.assertEqual(filter_context({}, self.prompt), self.prompt)
            self.assertEqual(filter_context(self.cfg, '普通问题'), '普通问题')
            call.assert_not_called()
