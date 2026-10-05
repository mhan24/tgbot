"""Fetch an observed Bitcoin chain tip; fail closed when providers are unavailable."""
import hashlib
import json
import re
import urllib.error
import urllib.request

class BitcoinUnavailable(Exception): pass


def latest_block():
    for base in ('https://mempool.space/api', 'https://blockstream.info/api'):
        try:
            request=urllib.request.Request(base+'/blocks/tip/hash',headers={'User-Agent':'SetupGroupBot/1.0','Cache-Control':'no-cache'})
            with urllib.request.urlopen(request,timeout=6) as response:
                block_hash=response.read(256).decode('ascii').strip().lower()
            if not re.fullmatch('[0-9a-f]{64}',block_hash):continue
            request=urllib.request.Request(base+'/block/'+block_hash,headers={'User-Agent':'SetupGroupBot/1.0'})
            with urllib.request.urlopen(request,timeout=6) as response:
                block=json.load(response)
            if not isinstance(block,dict) or block.get('id')!=block_hash or type(block.get('height')) is not int or block['height']<0:continue
            return {'hash':block_hash,'height':block['height'],'provider':base}
        except (OSError,urllib.error.URLError,ValueError,UnicodeError):
            continue
    raise BitcoinUnavailable('暂时无法获取最新 BTC 区块，请稍后重试；本次未创建空投。')


def candidate_order(block_hash,ids):
    return sorted(ids,key=lambda uid:(hashlib.sha256(f'airdrop-v1|{block_hash}|{uid}'.encode('ascii')).digest(),uid))
