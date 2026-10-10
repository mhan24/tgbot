"""ExchangeRate.fun conversion, cached hourly; Decimal avoids float rounding."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import json
import html
import re
import time
import urllib.request


class RateError(Exception):
    pass


def number(value):
    text = f'{value:.8f}'.rstrip('0').rstrip('.')
    return text or '0'


class Exchange:
    def __init__(self, db):
        self.db = db
        db.execute('CREATE TABLE IF NOT EXISTS exchange_cache(id INTEGER PRIMARY KEY,data TEXT,expires INTEGER)')
        db.commit()

    def convert(self, args):
        if len(args) == 2:
            args = ['1'] + list(args)
        if len(args) != 3 or not re.fullmatch(r'\d{1,15}(?:\.\d{1,8})?', args[0]):
            raise RateError('用法：/rate 100 USD CNY\n或 /rate USD CNY（换算 1 USD）')
        amount = Decimal(args[0])
        source, target = (s.upper() for s in args[1:])
        if not all(re.fullmatch(r'[A-Z]{3}', s) for s in (source, target)):
            raise RateError('请输入三位币种代码，例如 USD、CNY、EUR、BTC。')
        now = int(time.time())
        row = self.db.execute('SELECT data FROM exchange_cache WHERE id=1 AND expires>?', (now,)).fetchone()
        if row:
            data = json.loads(row[0])
        else:
            try:
                req = urllib.request.Request('https://api.exchangerate.fun/latest?base=USD',
                                             headers={'Accept': 'application/json', 'User-Agent': 'SetupGroupBot/1.0'})
                with urllib.request.urlopen(req, timeout=8) as response:
                    data = json.load(response)
                if data.get('base') != 'USD' or not isinstance(data.get('rates'), dict):
                    raise ValueError()
                for value in data['rates'].values():
                    rate = Decimal(str(value))
                    if not rate.is_finite() or rate <= 0:
                        raise ValueError()
                if not data['rates']:
                    raise ValueError()
            except (OSError, ValueError, InvalidOperation, TypeError):
                raise RateError('汇率服务暂时不可用，请稍后重试。') from None
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO exchange_cache VALUES(1,?,?)',
                                (json.dumps(data), now + 3600))
        rates = {'USD': 1, **data['rates']}
        if source not in rates or target not in rates:
            raise RateError('不支持该币种，请使用接口支持的三位币种代码。')
        with localcontext() as ctx:
            ctx.prec = 40
            ratio = Decimal(str(rates[target])) / Decimal(str(rates[source]))
            total = amount * ratio
            text = f'{number(amount)} {source} ≈ <b>{number(total)} {target}</b>\n1 {source} ≈ {number(ratio)} {target}'
        stamp = data.get('timestamp')
        try:
            date = datetime.fromtimestamp(float(stamp), timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M（北京时间）')
        except (TypeError, ValueError, OverflowError, OSError):
            date = str(data.get('date') or '未知')
        return text + '\n数据更新：' + html.escape(date) + '\n来源：ExchangeRate.fun · 参考汇率'
