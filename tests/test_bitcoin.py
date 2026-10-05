import io
import json
import unittest
from unittest.mock import patch
from bitcoin import latest_block,candidate_order,BitcoinUnavailable

class Tests(unittest.TestCase):
    def test_valid_block(self):
        h='a'*64
        with patch('bitcoin.urllib.request.urlopen',side_effect=[io.BytesIO(h.encode()),io.BytesIO(json.dumps({'id':h,'height':123}).encode())]):
            self.assertEqual(latest_block()['hash'],h)
    def test_failover(self):
        h='b'*64
        with patch('bitcoin.urllib.request.urlopen',side_effect=[TimeoutError(),io.BytesIO(h.encode()),io.BytesIO(json.dumps({'id':h,'height':124}).encode())]):
            self.assertIn('blockstream',latest_block()['provider'])
    def test_invalid_blocks_rejected(self):
        with patch('bitcoin.urllib.request.urlopen',side_effect=[io.BytesIO(b'bad'),io.BytesIO(b'bad')]):
            with self.assertRaises(BitcoinUnavailable):latest_block()
    def test_order_reproducible_independent_of_input(self):
        h='a'*64
        self.assertEqual(candidate_order(h,[3,2,1]),candidate_order(h,[1,2,3]))
        self.assertEqual(set(candidate_order(h,[1,2,3])),{1,2,3})
