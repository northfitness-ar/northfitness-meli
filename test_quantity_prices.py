import copy
import tempfile
import unittest
from pathlib import Path
import httpx
from fastmcp.exceptions import ToolError
from price_tools import PriceChanges
from quantity_prices import read, update, targets


class Client:
    def __init__(self):
        self.item = dict(id='MLA123', seller_id=1, currency_id='ARS', price=17990,
                         status='active', variations=[], user_product_id='UP1')
        self.prices = {'id': 'MLA123', 'prices': [
            {'id': '1', 'type': 'standard', 'currency_id': 'ARS', 'amount': 17990, 'conditions': {}},
            {'id': '2', 'type': 'standard', 'currency_id': 'ARS', 'amount': 11590,
             'conditions': {'context_restrictions': ['channel_marketplace', 'user_type_business'],
                            'min_purchase_unit': 5}}]}
        self.posts = 0
        self.status = 200
        self.timeout = False
        self.mismatch = False

    async def get(self, path):
        return copy.deepcopy(self.prices if path.endswith('/prices') else self.item)

    async def request(self, method, path, json):
        assert method == 'POST' and path == '/items/MLA123/prices/standard/quantity'
        self.posts += 1
        if self.timeout:
            raise httpx.ReadTimeout('timeout')
        if self.status == 200:
            self.prices['prices'] = [self.prices['prices'][0]] + [dict(t, type='standard') for t in json['prices']]
            if self.mismatch:
                self.item['price'] = 18000
        return httpx.Response(self.status)


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.changes = PriceChanges(Path(self.tmp.name) / 'prices.db')
        self.c = Client()
        self.rows = [{'quantity': 5, 'discount_percent': 17.18}]

    async def send(self, op='operation_123'):
        state = await read(self.c, '1', 'MLA123')
        return await update(self.changes, self.c, '1', 'MLA123', self.rows, state['snapshot_hash'], op)

    async def test_verified_and_replay(self):
        state = await read(self.c, '1', 'MLA123')
        args = (self.changes, self.c, '1', 'MLA123', self.rows, state['snapshot_hash'], 'operation_123')
        r = await update(*args)
        self.assertEqual(r['state'], 'verified')
        self.assertEqual(r['observed'][0]['amount'], '14899.32')
        self.assertEqual(await update(*args), r)
        self.assertEqual(self.c.posts, 1)

    async def test_timeout_locks_resource(self):
        self.c.timeout = True
        self.assertEqual((await self.send())['state'], 'unknown')
        with self.assertRaises(ToolError):
            await self.send('operation_456')
        self.assertEqual(self.c.posts, 1)

    async def test_forbidden_no_verification_retry(self):
        self.c.status = 403
        self.assertEqual((await self.send())['state'], 'rejected')
        with self.assertRaises(ToolError):
            await self.send('operation_456')
        self.assertEqual(self.c.posts, 1)

    async def test_retail_changed_detected(self):
        self.c.mismatch = True
        self.assertEqual((await self.send())['state'], 'verification_mismatch')

    async def test_wrong_owner(self):
        self.c.item['seller_id'] = 2
        with self.assertRaises(ToolError):
            await self.send()
        self.assertEqual(self.c.posts, 0)

    async def test_different_audience_rejected(self):
        self.c.prices['prices'][1]['conditions']['context_restrictions'] = ['channel_marketplace']
        with self.assertRaises(ToolError):
            await self.send()
        self.assertEqual(self.c.posts, 0)

    async def test_missing_tier_rejected(self):
        self.rows = []
        with self.assertRaises(ToolError):
            await self.send()

    async def test_stale_snapshot(self):
        s = await read(self.c, '1', 'MLA123')
        self.c.item['price'] = 19000
        with self.assertRaises(ToolError):
            await update(self.changes, self.c, '1', 'MLA123', self.rows, s['snapshot_hash'], 'operation_123')
        self.assertEqual(self.c.posts, 0)

    def test_invalid_discounts(self):
        for discount in [0, 100, 'NaN', -1]:
            with self.assertRaises(ToolError):
                targets([{'quantity': 5}], [{'quantity': 5, 'discount_percent': discount}], '17990')


if __name__ == '__main__':
    unittest.main()
