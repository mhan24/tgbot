# coding: utf-8
"""汇率换算：exchangerate.fun 免费接口，USD 基准小时缓存，Decimal 交叉换算。

用法：/rate [金额] <原币> <目标币>，或 /rate <原币> <目标币>（金额默认 1）。
金额最多 15 位整数、8 位小数，不接受负数与科学计数；币种按接口返回列表校验，
USDT 不自动当作 USD。接口失败时不使用过期缓存，也不伪造结果。
"""
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

CST = timezone(timedelta(hours=8))
CACHE_SECONDS = 3600
API_URL = 'https://api.exchangerate.fun/latest'
TIMEOUT = 10
# 上游拦截默认的 Python-urllib UA（403），请求必须携带自定义 UA。
USER_AGENT = 'tgbot/1.0'

AMOUNT = re.compile(r'\d{1,15}(\.\d{1,8})?')
USAGE = '用法：/rate 100 USD CNY，或 /rate USD CNY。'
UNAVAILABLE = '汇率服务暂时不可用，请稍后重试。'


class RateError(Exception):
    """用户可读的换算失败说明；调用方按失败时长展示。"""


def _fetch(base, timeout):
    request = urllib.request.Request(f'{API_URL}?base={base}',
                                     headers={'Accept': 'application/json',
                                              'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, ValueError):
        raise RateError(UNAVAILABLE) from None
    rates = payload.get('rates') if isinstance(payload, dict) else None
    if not isinstance(rates, dict) or not rates:
        raise RateError(UNAVAILABLE)
    try:
        stamp = int(payload.get('timestamp'))
    except (TypeError, ValueError):
        stamp = 0
    return rates, stamp


def _format_number(value):
    """Decimal 的无指数展示：按 8 位有效数字截断并去掉多余的尾零。"""
    if not value:
        return '0'
    quantum = Decimal(1).scaleb(value.adjusted() - 7)
    return format(value.quantize(quantum).normalize(), 'f')


class Exchange:
    def __init__(self, db, timeout=TIMEOUT):
        self.db = db
        self.timeout = timeout
        db.row_factory = db.row_factory or sqlite3.Row
        db.executescript('''
        CREATE TABLE IF NOT EXISTS rates(
          base TEXT PRIMARY KEY, stamp INTEGER, fetched INTEGER, payload TEXT NOT NULL);
        ''')

    def _usd_rates(self):
        row = self.db.execute('SELECT stamp,fetched,payload FROM rates WHERE base=?',
                              ('USD',)).fetchone()
        if row and int(time.time()) - row['fetched'] < CACHE_SECONDS:
            return json.loads(row['payload']), row['stamp']
        rates, stamp = _fetch('USD', self.timeout)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO rates(base,stamp,fetched,payload) VALUES(?,?,?,?)',
                            ('USD', stamp, int(time.time()), json.dumps(rates)))
        return rates, stamp

    def convert(self, args):
        """处理 /rate 参数并返回回复文本；用户可见的失败抛 RateError。"""
        args = [item.upper() for item in args if item]
        if len(args) == 2:
            amount_text, source, target = '1', args[0], args[1]
        elif len(args) == 3:
            amount_text, source, target = args
        else:
            raise RateError(USAGE)
        if not AMOUNT.fullmatch(amount_text):
            raise RateError('金额格式不正确：最多 15 位整数、8 位小数，不接受负数与科学计数。')
        try:
            amount = Decimal(amount_text)
        except InvalidOperation:
            raise RateError('金额格式不正确：最多 15 位整数、8 位小数，不接受负数与科学计数。') from None
        rates, stamp = self._usd_rates()
        for code in (source, target):
            if code not in rates:
                raise RateError(f'暂不支持币种 {code}，例如 /rate USD CNY。')
        unit = Decimal(str(rates[target])) / Decimal(str(rates[source]))
        result = amount * unit
        when = datetime.fromtimestamp(stamp or time.time(), CST).strftime('%Y-%m-%d %H:%M')
        return (f'{_format_number(amount)} {source} = {_format_number(result)} {target}\n'
                f'单位汇率：1 {source} = {_format_number(unit)} {target}\n'
                f'数据时间：{when}（北京时间，每小时更新）\n'
                f'来源：exchangerate.fun')
