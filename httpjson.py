# coding: utf-8
"""最小 HTTP JSON 封装：统一的 UA、超时与错误类型，供 exchange / binlookup / ai 复用。

上游（exchangerate.fun、binlist.net、OpenAI 兼容网关、Exa、TypeSafe）都会拦掉默认的
Python-urllib UA，所以必须在每个请求上带自定义 UA —— 抽到这里只写一次。

错误只有两种，调用方各自翻译成自己的用户可读异常：

* ``HTTPStatus``：拿到了响应但状态码不是 2xx，``code`` 是状态码，``detail`` 是截断的响应体；
* ``OSError`` / ``ValueError``：连不上、超时、或者响应不是可解析的 JSON 对象。

``botapi.py`` 不走这里：Telegram 的响应体里带 ``ok`` / ``error_code`` / ``retry_after``，
需要按它自己的协议解析。
"""
import json
import urllib.error
import urllib.request

USER_AGENT = 'tgbot/1.0'
DEFAULT_TIMEOUT = 10
DETAIL_CHARS = 300


class HTTPStatus(Exception):
    """上游返回了非 2xx 状态码。"""

    def __init__(self, code, detail=''):
        self.code = code
        self.detail = detail
        super().__init__(f'HTTP {code}')


def get(url, headers=None, timeout=DEFAULT_TIMEOUT):
    """GET 一个 JSON 对象。"""
    return _json(url, None, headers, timeout)


def post(url, body, headers=None, timeout=DEFAULT_TIMEOUT):
    """POST 一个 JSON 对象。"""
    return _json(url, json.dumps(body).encode(), headers, timeout)


def _json(url, data, headers, timeout):
    request = urllib.request.Request(url, data=data, headers={
        'Accept': 'application/json', 'User-Agent': USER_AGENT,
        **({'Content-Type': 'application/json'} if data else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise HTTPStatus(exc.code, _detail(exc)) from None
    except (OSError, urllib.error.URLError):
        raise OSError('request failed') from None
    except (ValueError, UnicodeError):
        raise ValueError('unreadable response') from None
    if not isinstance(payload, dict):
        raise ValueError('unreadable response')
    return payload


def _detail(exc):
    try:
        return exc.read().decode('utf-8', 'replace')[:DETAIL_CHARS]
    except Exception:
        return ''
