# coding: utf-8
"""卡 BIN 查询：cardbincheck.com（IPNOVA）接口，成功结果缓存 24 小时。

鉴权：请求头 X-Api-Key，密钥来自环境变量 BIN_API_KEY；未配置时上游按
IP 限流开放访问。成功一次消耗 1 个额度，失败与限流不计费，因此缓存
只存成功结果。数据字段缺失显示“未知”。
"""
import json
import sqlite3
import time
import urllib.error
import urllib.request

CACHE_SECONDS = 86400
API_URL = 'https://cardbincheck.com/api/bin/'
TIMEOUT = 10
# 与 exchange.py 保持一致：请求携带自定义 UA，避免被上游按默认库 UA 拦截。
USER_AGENT = 'tgbot/1.0'
USAGE = '用法：/bin 45717360（6–8 位数字）。'
UNAVAILABLE = 'BIN 查询服务暂时不可用，请稍后重试。'
BAD_KEY = 'BIN 查询密钥无效，请检查 BIN_API_KEY 配置。'

FUNDING = {'credit': '信用卡', 'debit': '借记卡', 'prepaid': '预付卡', 'charge': '签账卡'}
UNKNOWN = {'', 'unknown'}


class BinError(Exception):
    """用户可读的查询失败说明；调用方按失败时长展示。"""


class BinLookup:
    def __init__(self, db, api_key='', timeout=TIMEOUT):
        self.db = db
        self.api_key = (api_key or '').strip()
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
        bin_number = args[0].strip()
        if not bin_number.isdigit() or not 6 <= len(bin_number) <= 8:
            raise BinError(USAGE)
        row = self.db.execute('SELECT payload FROM bin_cache WHERE bin=?',
                              (bin_number,)).fetchone()
        if row:
            return self._format(json.loads(row['payload']), bin_number)
        headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT}
        if self.api_key:
            headers['X-Api-Key'] = self.api_key
        request = urllib.request.Request(API_URL + bin_number, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise BinError(f'BIN {bin_number} 未收录，请确认号码后重试。') from None
            if exc.code in (401, 403):
                raise BinError(BAD_KEY) from None
            if exc.code == 429:
                raise BinError('BIN 查询上游限流，请稍后再试。') from None
            raise BinError(UNAVAILABLE) from None
        except (OSError, urllib.error.URLError, ValueError):
            raise BinError(UNAVAILABLE) from None
        if not isinstance(payload, dict) or payload.get('success') is not True \
                or not isinstance(payload.get('data'), dict):
            raise BinError(UNAVAILABLE)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO bin_cache(bin,fetched,payload) VALUES(?,?,?)',
                            (bin_number, int(time.time()), json.dumps(payload)))
        return self._format(payload, bin_number)

    @staticmethod
    def _format(payload, bin_number):
        data = payload.get('data') or {}

        def clean(key):
            value = str(data.get(key) or '').strip()
            return '' if value.lower() in UNKNOWN else value

        scheme = clean('scheme') or '未知'
        funding = clean('funding')
        kind = FUNDING.get(funding.lower(), funding) or '未知'
        lines = [f'BIN {bin_number}',
                 f'卡组织：{scheme}',
                 f'类型：{kind}',
                 f'卡种：{clean("segment") or "未知"}',
                 f'发卡行：{clean("issuer") or "未知"}']
        country, code = clean('country_name') or '未知', clean('country_code')
        lines.append(f'国家/地区：{country}（{code}）' if code else f'国家/地区：{country}')
        url, phone = clean('bank_url'), clean('bank_phone')
        if url:
            lines.append(f'发卡行网址：{url}')
        if phone:
            lines.append(f'发卡行电话：{phone}')
        lines.append('来源：CardBinCheck.com')
        return '\n'.join(lines)
