# coding: utf-8
"""卡 BIN 查询：binlist.net 接口（Accept-Version: 3），成功结果缓存 24 小时。

上游无需密钥，免费档按 IP 限流，因此缓存只存成功结果，展示格式对齐
TeleBox 的 bin 插件（卡头/品牌/类型/等级/国家/货币/银行/预付卡/商业卡）。
缓存按字段归一化后存储并带版本号，旧版本式样的缓存行直接忽略重建。
数据字段缺失显示“未知”。
"""
import html
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request

CACHE_SECONDS = 86400
API_URL = 'https://lookup.binlist.net/'
TIMEOUT = 10
# 与 exchange.py 保持一致：请求携带自定义 UA，避免被上游按默认库 UA 拦截。
USER_AGENT = 'tgbot/1.0'
USAGE = '用法：/bin 45717360（6–8 位数字）。'
UNAVAILABLE = 'BIN 查询服务暂时不可用，请稍后重试。'
PAYLOAD_VERSION = 2

FUNDING = {'credit': '贷记', 'debit': '借记', 'charge': '签账', 'prepaid': '预付'}
LEVELS = re.compile(r'BUSINESS|CORPORATE|PLATINUM|GOLD|CLASSIC|SIGNATURE'
                    r'|INFINITE|WORLD|PREMIER|PREMIUM', re.I)
COMMERCIAL = re.compile(r'BUSINESS|CORPORATE|COMMERCIAL', re.I)
CURRENCY = {'CNY': '人民币', 'USD': '美元', 'EUR': '欧元', 'GBP': '英镑', 'JPY': '日元',
            'HKD': '港币', 'TWD': '新台币', 'KRW': '韩元', 'SGD': '新加坡元', 'AUD': '澳元'}
UNKNOWN = {'', 'unknown'}


class BinError(Exception):
    """用户可读的查询失败说明；调用方按失败时长展示。"""


class BinLookup:
    def __init__(self, db, timeout=TIMEOUT):
        self.db = db
        self.timeout = timeout
        db.row_factory = db.row_factory or sqlite3.Row
        db.executescript('''
        CREATE TABLE IF NOT EXISTS bin_cache(
          bin TEXT PRIMARY KEY, fetched INTEGER, payload TEXT NOT NULL);
        ''')

    def query(self, args):
        """处理 /bin 参数并返回回复文本；用户可见的失败抛 BinError。"""
        if len(args) != 1:
            raise BinError(USAGE)
        bin_number = re.sub(r'\D', '', args[0])
        if not 6 <= len(bin_number) <= 8:
            raise BinError(USAGE)
        row = self.db.execute('SELECT payload FROM bin_cache WHERE bin=?',
                              (bin_number,)).fetchone()
        if row:
            try:
                payload = json.loads(row['payload'])
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get('v') == PAYLOAD_VERSION:
                return self._format(payload, bin_number)
            with self.db:
                self.db.execute('DELETE FROM bin_cache WHERE bin=?', (bin_number,))
        request = urllib.request.Request(API_URL + bin_number, headers={
            'Accept': 'application/json', 'Accept-Version': '3', 'User-Agent': USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise BinError(f'BIN {bin_number} 未收录，请确认号码后重试。') from None
            if exc.code == 429:
                raise BinError('BIN 查询上游限流，请稍后再试。') from None
            raise BinError(UNAVAILABLE) from None
        except (OSError, urllib.error.URLError, ValueError):
            raise BinError(UNAVAILABLE) from None
        # binlist 对未收录的 BIN 返回 200 空数据而非 404：按未收录提示。
        if not isinstance(data, dict) or not data.get('scheme'):
            raise BinError(f'BIN {bin_number} 未收录，请确认号码后重试。')
        payload = self._normalize(data)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO bin_cache(bin,fetched,payload) VALUES(?,?,?)',
                            (bin_number, int(time.time()), json.dumps(payload)))
        return self._format(payload, bin_number)

    @staticmethod
    def _normalize(data):
        """只保留展示用字段，缓存与上游响应结构解耦。"""
        country = data.get('country') or {}
        bank = data.get('bank') or {}

        def clean(value):
            value = str(value or '').strip()
            return '' if value.lower() in UNKNOWN else value

        return {'v': PAYLOAD_VERSION,
                'scheme': clean(data.get('scheme')),
                'brand': clean(data.get('brand')),
                'type': clean(data.get('type')),
                'prepaid': bool(data.get('prepaid')),
                'country': clean(country.get('name')),
                'emoji': clean(country.get('emoji')),
                'alpha2': clean(country.get('alpha2')),
                'currency': clean(country.get('currency')),
                'bank_name': clean(bank.get('name')),
                'bank_url': clean(bank.get('url')),
                'bank_phone': clean(bank.get('phone'))}

    @staticmethod
    def _format(payload, bin_number):
        scheme = payload['scheme'] or '未知'
        brand = payload['brand']
        level = LEVELS.search(brand)
        kind = FUNDING.get(payload['type'].lower(), payload['type']) or '未知'
        country = payload['country']
        if 'taiwan' in country.lower():
            country = '台湾'
        else:
            country = country.replace(' (Province of China)', '')
        currency = payload['currency']
        currency = CURRENCY.get(currency.upper(), currency) or '未知'
        geo = f"{payload['emoji']} {html.escape(country or '未知')}（{payload['alpha2'] or '-'}）"
        lines = ['💳 卡头检测',
                 f"🔢卡头：{bin_number} 🔳品牌：{html.escape(scheme.title())}",
                 f"🔖类型：{html.escape(kind)} 💹等级："
                 f"{html.escape(level.group(0).title()) if level else '未知'}",
                 f"🗺国家：{geo} 💸货币：{html.escape(currency)}",
                 f"🏦银行：{html.escape(payload['bank_name'] or '未知')}",
                 f"💰预付卡：{'✓' if payload['prepaid'] else '×'} "
                 f"🧾商业卡：{'✓' if COMMERCIAL.search(brand) else '×'}"]
        if payload['bank_url']:
            lines.append(f"发卡行网址：{html.escape(payload['bank_url'])}")
        if payload['bank_phone']:
            lines.append(f"发卡行电话：{html.escape(payload['bank_phone'])}")
        return '\n'.join(lines)
