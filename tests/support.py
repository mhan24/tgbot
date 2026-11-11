# coding: utf-8
"""测试公用件：最小 Telegram 模拟、Bot 装配与几个假上游响应。

在仓库根目录跑全部测试：

    python3 -m unittest discover -s tests -t . -v
"""
import io
import json
import sqlite3
import urllib.error

from bot import Bot
from botapi import APIError

GROUP = -1
CHANNEL = -2
BOT_ID = 99
BOT_USERNAME = 'testbot'


class FakeAPI:
    """最小 Telegram 模拟；只实现被测代码真正调用的方法。"""

    def __init__(self):
        self.calls = []
        self.failure = None          # 设成方法名就让这次调用失败
        self.flood = 0               # >0 时先返回这么多次 429
        self.members = {}            # user_id -> getChatMember 结果
        self.sent = 0
        self.channel_status = 'member'
        self.group_status = 'member'
        self.chat = {'permissions': {'can_send_messages': True, 'can_send_photos': True,
                                     'can_invite_users': False}}

    def call(self, method, **data):
        self.calls.append((method, data))
        if self.failure == method:
            raise APIError(method, {'error_code': 500, 'description': 'temporary failure'})
        if self.flood and method == 'sendMessage':
            self.flood -= 1
            raise APIError(method, {'error_code': 429, 'description': 'Too Many Requests',
                                    'parameters': {'retry_after': 1}})
        if method == 'getMe':
            return {'id': BOT_ID, 'username': BOT_USERNAME}
        if method == 'getWebhookInfo':
            return {'url': ''}
        if method == 'sendMessage':
            self.sent += 1
            return {'message_id': self.sent}
        if method == 'getChatMember':
            if data['user_id'] in self.members:
                return self.members[data['user_id']]
            if data['chat_id'] == CHANNEL:
                return {'status': self.channel_status}
            return {'status': self.group_status}
        if method == 'getChat':
            return self.chat
        return True

    # --- 断言辅助 ---------------------------------------------------------

    def methods(self):
        return [method for method, _ in self.calls]

    def find(self, method):
        return [data for name, data in self.calls if name == method]

    def texts(self):
        return [data['text'] for data in self.find('sendMessage')]

    def clear(self):
        self.calls.clear()
        self.sent = 0


def make_bot(api=None, **cfg):
    """装配一个指向内存库、已就绪的 Bot（bot_id / username 已设好）。"""
    api = api or FakeAPI()
    values = {'GROUP_ID': str(GROUP), 'CHANNEL_ID': str(CHANNEL)} | cfg
    bot = Bot(values, api, sqlite3.connect(':memory:'))
    bot.bot_id, bot.username = BOT_ID, BOT_USERNAME
    return bot, api


def group_message(mid, text, uid=2, entities=None, **extra):
    """群消息；text 可以是正文，entities 用来标 @ 提及。"""
    message = {'update_id': mid, 'message': {
        'chat': {'id': GROUP, 'type': 'supergroup'}, 'message_id': mid,
        'from': {'id': uid, 'first_name': 'U'}, 'text': text}}
    if entities is not None:
        message['message'] = message['message'] | {'entities': entities}
    message['message'] |= extra
    return message


def mention(mid, question, uid=2):
    """``@testbot <question>`` 形式的一条群消息。"""
    text = f'@{BOT_USERNAME} {question}'
    return group_message(mid, text, uid,
                         entities=[{'type': 'mention', 'offset': 0,
                                    'length': len(BOT_USERNAME) + 1}])


class FakeResponse:
    """给 httpjson 用的假响应：``json.load(response)`` 只用到 read()。"""

    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self, *args):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def json_response(payload, calls=None):
    """让 httpjson 返回固定 JSON；calls 传入列表可记录每次请求。"""
    def urlopen(request, timeout=None):
        if calls is not None:
            calls.append((request, timeout))
        return FakeResponse(payload)
    return urlopen


def http_status(code, body=b'upstream error', calls=None):
    """让 httpjson 收到非 2xx 状态码。"""
    def urlopen(request, timeout=None):
        if calls is not None:
            calls.append((request, timeout))
        raise urllib.error.HTTPError('upstream', code, 'failed', {}, io.BytesIO(body))
    return urlopen


def network_down(calls=None):
    """让 httpjson 连不上上游。"""
    def urlopen(request, timeout=None):
        if calls is not None:
            calls.append((request, timeout))
        raise urllib.error.URLError('down')
    return urlopen
