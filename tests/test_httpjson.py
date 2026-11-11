# coding: utf-8
"""共用 HTTP 封装测试：UA、超时、状态码与不可解析响应的错误类型。"""
import unittest
from unittest.mock import patch

import httpjson
from tests.support import FakeResponse, http_status, json_response, network_down


class Tests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)

    def test_get_sends_user_agent_and_accept_header(self):
        calls = []
        patch('httpjson.urllib.request.urlopen',
              side_effect=json_response({'ok': 1}, calls=calls)).start()
        self.assertEqual(httpjson.get('https://upstream/x'), {'ok': 1})
        headers = {key.lower(): value for key, value in calls[0][0].headers.items()}
        self.assertEqual(headers['user-agent'], httpjson.USER_AGENT)
        self.assertEqual(headers['accept'], 'application/json')
        self.assertNotIn('content-type', headers)

    def test_post_sends_json_body(self):
        calls = []
        patch('httpjson.urllib.request.urlopen',
              side_effect=json_response({'ok': 1}, calls=calls)).start()
        httpjson.post('https://upstream/x', {'a': [1, 2]}, {'Authorization': 'Bearer t'})
        request = calls[0][0]
        self.assertEqual(request.data, b'{"a": [1, 2]}')
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(headers['content-type'], 'application/json')
        self.assertEqual(headers['authorization'], 'Bearer t')

    def test_timeout_is_passed_through(self):
        calls = []
        patch('httpjson.urllib.request.urlopen',
              side_effect=json_response({}, calls=calls)).start()
        httpjson.get('https://upstream/x', timeout=3)
        self.assertEqual(calls[0][1], 3)
        httpjson.post('https://upstream/x', {})
        self.assertEqual(calls[1][1], httpjson.DEFAULT_TIMEOUT)

    def test_http_status_carries_code_and_truncated_detail(self):
        body = b'x' * (httpjson.DETAIL_CHARS + 50)
        patch('httpjson.urllib.request.urlopen', side_effect=http_status(429, body)).start()
        with self.assertRaises(httpjson.HTTPStatus) as caught:
            httpjson.get('https://upstream/x')
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(len(caught.exception.detail), httpjson.DETAIL_CHARS)
        self.assertEqual(str(caught.exception), 'HTTP 429')

    def test_network_failures_are_oserror(self):
        patch('httpjson.urllib.request.urlopen', side_effect=network_down()).start()
        with self.assertRaises(OSError) as caught:
            httpjson.get('https://upstream/x')
        self.assertNotIsInstance(caught.exception, httpjson.HTTPStatus)

    def test_unreadable_payloads_are_value_errors(self):
        for payload in (b'not-json', b'[]', b'"text"', b'{"ok": true} trailing'):
            with self.subTest(payload=payload):
                patch('httpjson.urllib.request.urlopen',
                      side_effect=lambda request, timeout=None, p=payload: FakeResponse(p)).start()
                with self.assertRaises(ValueError):
                    httpjson.get('https://upstream/x')
                patch.stopall()


if __name__ == '__main__':
    unittest.main()
