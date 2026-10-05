import time
import unittest
from unittest.mock import patch
from chain import latest_block,BlockUnavailable,candidate_order

class Tests(unittest.TestCase):
    def block(self):return {'hash':'0x'+'a'*64,'number':'0x10','timestamp':hex(int(time.time()))}
    def test_valid_base_sealed_block(self):
        with patch('chain.rpc',side_effect=['0x2105',self.block()]) as rpc:
            b=latest_block();self.assertEqual(b['chain'],'Base');self.assertEqual(b['height'],16)
            self.assertEqual(rpc.call_args.args[2],['latest',False])
    def test_wrong_chain_fails_over(self):
        with patch('chain.rpc',side_effect=['0x1','0x2105',self.block()]):
            self.assertIn('publicnode',latest_block()['provider'])
    def test_stale_or_null_block_rejected(self):
        stale=dict(self.block(),timestamp='0x0')
        with patch('chain.rpc',side_effect=['0x2105',stale,'0x2105',None]):
            with self.assertRaises(BlockUnavailable):latest_block()
    def test_bad_hash_rejected(self):
        with patch('chain.rpc',side_effect=['0x2105',dict(self.block(),hash='bad')]*2):
            with self.assertRaises(BlockUnavailable):latest_block()
    def test_deterministic_order(self):
        self.assertEqual(candidate_order('0x'+'a'*64,[1,2,3]),candidate_order('0x'+'a'*64,[3,1,2]))
