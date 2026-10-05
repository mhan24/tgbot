"""Base mainnet sealed latest-block source for airdrop uniqueness."""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request

class BlockUnavailable(Exception):pass


def rpc(endpoint,method,params):
    request=urllib.request.Request(endpoint,data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode(),
                                   headers={'Content-Type':'application/json','User-Agent':'SetupGroupBot/1.0','Cache-Control':'no-cache'})
    with urllib.request.urlopen(request,timeout=6) as response:payload=json.load(response)
    if not isinstance(payload,dict) or payload.get('error') or 'result' not in payload:raise ValueError('Invalid RPC response')
    return payload['result']


def latest_block():
    for endpoint in ('https://mainnet.base.org','https://base-rpc.publicnode.com'):
        try:
            if rpc(endpoint,'eth_chainId',[])!='0x2105':continue
            block=rpc(endpoint,'eth_getBlockByNumber',['latest',False])
            if not isinstance(block,dict):continue
            h=block.get('hash');number=block.get('number');timestamp=block.get('timestamp')
            if not isinstance(h,str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}',h) or int(h,16)==0:continue
            if not all(isinstance(v,str) and re.fullmatch(r'0x[0-9a-fA-F]+',v) for v in (number,timestamp)):continue
            if not -30<=time.time()-int(timestamp,16)<=60:continue
            return {'hash':h.lower(),'height':int(number,16),'provider':endpoint,'chain':'Base'}
        except (OSError,urllib.error.URLError,ValueError,UnicodeError):continue
    raise BlockUnavailable('暂时无法获取最新 Base 区块，请稍后重试；本次未创建空投。')


def candidate_order(block_hash,ids):
    return sorted(ids,key=lambda uid:(hashlib.sha256(f'airdrop-v1|{block_hash}|{uid}'.encode('ascii')).digest(),uid))
