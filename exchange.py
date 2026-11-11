# coding: utf-8
"""汇率换算：exchangerate.fun 免费接口，USD 基准小时缓存，Decimal 交叉换算。

用法：/rate [金额] <原币> <目标币>，或 /rate <原币> <目标币>（金额默认 1）。
金额最多 15 位整数、8 位小数，不接受负数与科学计数；币种按接口返回列表校验，
USDT 不自动当作 USD。接口失败时不使用过期缓存，也不伪造结果。
"""
import html
import json
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

import httpjson

CST = timezone(timedelta(hours=8))
CACHE_SECONDS = 3600
FAIL_COOLDOWN = 30  # 上游刚失败过就先冷却，别让每条 /rate 都去撞一个已经挂掉的服务
API_URL = 'https://api.exchangerate.fun/latest'
TIMEOUT = 6  # 上游正常在一秒内返回；超时就尽快放弃，别让主循环陪它等

AMOUNT = re.compile(r'\d{1,15}(\.\d{1,8})?')
SEPARATORS = str.maketrans('', '', ',_\u3000 ')
USAGE = '用法：/rate 100 USD CNY，或 /rate USD CNY。'
UNAVAILABLE = '汇率服务暂时不可用，请稍后重试。'


class RateError(Exception):
    """用户可读的换算失败说明；调用方按失败时长展示。"""


def _fetch(base, timeout):
    try:
        payload = httpjson.get(f'{API_URL}?base={base}', timeout=timeout)
    except (httpjson.HTTPStatus, OSError, ValueError):
        raise RateError(UNAVAILABLE) from None
    rates = payload.get('rates')
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
        self._failed_until = 0.0
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
        if time.time() < self._failed_until:
            raise RateError(UNAVAILABLE)
        try:
            rates, stamp = _fetch('USD', self.timeout)
        except RateError:
            self._failed_until = time.time() + FAIL_COOLDOWN
            raise
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
        # 允许千分位与空格：/rate 1,000 USD CNY。
        amount_text = amount_text.translate(SEPARATORS)
        if not AMOUNT.fullmatch(amount_text):
            raise RateError('金额格式不正确：最多 15 位整数、8 位小数，不接受负数与科学计数。')
        try:
            amount = Decimal(amount_text)
        except InvalidOperation:
            raise RateError('金额格式不正确：最多 15 位整数、8 位小数，不接受负数与科学计数。') from None
        rates, stamp = self._usd_rates()
        for code in (source, target):
            if code not in rates:
                # 回复按 HTML 发送：用户输入的币种码必须先转义。
                raise RateError(f'暂不支持币种 {html.escape(code)}，例如 /rate USD CNY。')
        unit = Decimal(str(rates[target])) / Decimal(str(rates[source]))
        result = amount * unit
        when = datetime.fromtimestamp(stamp or time.time(), CST).strftime('%Y-%m-%d %H:%M')
        return (f'{_format_number(amount)} {source} = {_format_number(result)} {target}\n'
                f'单位汇率：1 {source} = {_format_number(unit)} {target}\n'
                f'数据时间：{when}（北京时间，每小时更新）\n'
                f'来源：exchangerate.fun')
