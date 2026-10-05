"""Binlist v3 client with persistent cache and rate-limit backoff."""
import html
import json
import re
import time
import urllib.error
import urllib.request

class LookupError(Exception):
    pass

class BinLookup:
    def __init__(self, db):
        self.db = db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS bin_cache(bin TEXT PRIMARY KEY,data TEXT,expires INTEGER);
        CREATE TABLE IF NOT EXISTS bin_state(key TEXT PRIMARY KEY,value INTEGER);
        ''')

    def lookup(self, number):
        if not re.fullmatch(r'[0-9]{6,8}', number):
            raise LookupError('请输入 6–8 位 BIN，例如：/bin 45717360')
        now = int(time.time())
        row = self.db.execute('SELECT data FROM bin_cache WHERE bin=? AND expires>?', (number, now)).fetchone()
        if row:
            return json.loads(row[0])
        row = self.db.execute("SELECT value FROM bin_state WHERE key='retry_at'").fetchone()
        if row and row[0] > now:
            raise LookupError(f'Binlist 查询暂受限，请约 {(row[0]-now+59)//60} 分钟后重试。')
        req = urllib.request.Request('https://lookup.binlist.net/' + number,
                                     headers={'Accept-Version':'3', 'Accept':'application/json', 'User-Agent':'SetupGroupBot/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.load(response)
            if not isinstance(data, dict) or not any(data.get(k) for k in ('scheme','type','brand','bank','country')):
                raise LookupError('未查询到该 BIN 的有效信息。')
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LookupError('未找到该 BIN 的信息。') from None
            if exc.code == 429:
                retry = exc.headers.get('Retry-After', '')
                seconds = max(60, min(int(retry),86400)) if retry.isdigit() else 3600
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO bin_state VALUES('retry_at',?)", (now+seconds,))
                raise LookupError(f'Binlist 已限流，请约 {(seconds+59)//60} 分钟后重试。') from None
            raise LookupError('Binlist 服务暂时不可用，请稍后重试。') from None
        except (OSError, urllib.error.URLError, ValueError):
            raise LookupError('查询超时或服务响应异常，请稍后重试。') from None
        with self.db:
            self.db.execute('DELETE FROM bin_cache WHERE expires<=?', (now,))
            self.db.execute('INSERT OR REPLACE INTO bin_cache VALUES(?,?,?)', (number,json.dumps(data),now+86400))
        return data


def format_result(number, data):
    def safe(value):
        return html.escape(str(value)) if value is not None and value != '' else '未知'
    def boolean(value):
        return '是' if value is True else '否' if value is False else '未知'
    country = data.get('country') if isinstance(data.get('country'),dict) else {}
    bank = data.get('bank') if isinstance(data.get('bank'),dict) else {}
    card = data.get('number') if isinstance(data.get('number'),dict) else {}
    kind = data.get('type')
    kind = {'debit':'借记卡', 'credit':'信用卡'}.get(kind,kind) if isinstance(kind,str) else None
    place = ' '.join(str(country[k]) for k in ('emoji','name','alpha2') if country.get(k))
    return (f'💳 <b>BIN {safe(number)}</b>\n'
            f'卡组织：{safe(data.get("scheme"))}\n卡类型：{safe(kind)}\n品牌：{safe(data.get("brand"))}\n'
            f'预付卡：{boolean(data.get("prepaid"))}\n国家：{safe(place)}\n币种：{safe(country.get("currency"))}\n'
            f'银行：{safe(bank.get("name"))}\n城市：{safe(bank.get("city"))}\n'
            f'银行网站：{safe(bank.get("url"))}\n银行电话：{safe(bank.get("phone"))}\n'
            f'卡号长度：{safe(card.get("length"))}\nLuhn 校验：{boolean(card.get("luhn"))}\n'
            '数据来源：Binlist（缓存最长24小时）')
