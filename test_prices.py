import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastmcp.exceptions import ToolError
from price_tools import PriceChanges, amount, fingerprint, snapshot


class Client:
    def __init__(self):
        self.item = dict(id='MLA123', seller_id=7, currency_id='ARS', status='active',
                         price=15490, original_price=None, variations=[],
                         user_product_id='MLAU123', shipping={'logistic_type': 'fulfillment'})
        self.calls = []
        self.code = 200
        self.timeout = False
        self.mismatch = False
        self.fail_verification = False

    async def get(self, path):
        if self.calls and self.fail_verification:
            raise ToolError('unavailable')
        return copy.deepcopy(self.item)

    async def request(self, method, path, json):
        self.calls.append((method, path, json))
        if self.timeout:
            raise httpx.ReadTimeout('timeout')
        if self.code == 200 and not self.mismatch:
            if 'variations' in json:
                for variant in self.item['variations']:
                    variant['price'] = json['variations'][0]['price']
                self.item['price'] = json['variations'][0]['price']
            else:
                self.item.update(json)
        return SimpleNamespace(status_code=self.code)


class PricesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'changes.db'
        self.changes = PriceChanges(self.path)
        self.client = Client()

    async def change(self, **kwargs):
        args = dict(client=self.client, seller='7', item_id='MLA123', price='16990',
                    expected='15490', snapshot_hash=fingerprint(self.client.item), op='price_001')
        args.update(kwargs)
        return await self.changes.set(**args)

    async def test_full_user_product_and_payload(self):
        result = await self.change()
        self.assertEqual(result['state'], 'verified')
        self.assertEqual(self.client.calls, [('PUT', '/items/MLA123', {'price': 16990.0})])

    async def test_replay_after_restart(self):
        h = fingerprint(self.client.item)
        first = await self.change(snapshot_hash=h)
        self.changes = PriceChanges(self.path)
        self.assertEqual(await self.change(snapshot_hash=h), first)
        self.assertEqual(len(self.client.calls), 1)
        with self.assertRaises(ToolError):
            await self.change(price='17990', snapshot_hash=h)

    async def test_all_classic_variants_preserved(self):
        self.client.item['variations'] = [{'id': 1, 'price': 15490, 'available_quantity': 4},
                                         {'id': 2, 'price': 15490, 'available_quantity': 8}]
        self.assertEqual((await self.change())['state'], 'verified')
        payload = self.client.calls[0][2]
        self.assertEqual(payload, {'variations': [{'id': 1, 'price': 16990.0}, {'id': 2, 'price': 16990.0}]})
        self.assertEqual(self.client.item['variations'][1]['available_quantity'], 8)

    async def test_owner_currency_status(self):
        for key, value in [('seller_id', 8), ('currency_id', 'USD'), ('status', 'closed')]:
            original = self.client.item[key]
            self.client.item[key] = value
            with self.assertRaises(ToolError):
                await self.change()
            self.client.item[key] = original
        self.assertFalse(self.client.calls)

    async def test_stale_snapshot_and_expected(self):
        with self.assertRaises(ToolError):
            await self.change(snapshot_hash='0' * 64)
        with self.assertRaises(ToolError):
            await self.change(expected='15000')
        self.assertFalse(self.client.calls)

    async def test_promotions_blocked(self):
        self.client.item['original_price'] = 18000
        with self.assertRaises(ToolError):
            await self.change()
        self.assertFalse(self.client.calls)

    async def test_mixed_variants_blocked(self):
        self.client.item['variations'] = [{'id': 1, 'price': 14000}]
        with self.assertRaises(ToolError):
            await self.change()

    async def test_timeout_blocks_new_operation(self):
        self.client.timeout = True
        self.assertEqual((await self.change())['state'], 'unknown')
        self.changes = PriceChanges(self.path)
        with self.assertRaises(ToolError):
            await self.change(op='price_002')
        self.assertEqual(len(self.client.calls), 1)

    async def test_rejected_permission_no_retry(self):
        self.client.code = 403
        self.assertEqual((await self.change())['state'], 'rejected')
        await self.change()
        with self.assertRaises(ToolError):
            await self.change(op='price_002')
        self.assertEqual(len(self.client.calls), 1)

    async def test_500_uncertain(self):
        self.client.code = 500
        self.assertEqual((await self.change())['state'], 'unknown')

    async def test_verification_mismatch(self):
        self.client.mismatch = True
        self.assertEqual((await self.change())['state'], 'verification_mismatch')

    async def test_failed_readback(self):
        self.client.fail_verification = True
        self.assertEqual((await self.change())['state'], 'unknown')

    async def test_noop(self):
        self.assertEqual((await self.change(price='15490'))['state'], 'unchanged')
        self.assertFalse(self.client.calls)

    async def test_validation(self):
        for value in ['NaN', 'Infinity', '-1', '0', '1.001', '999999999', 'true']:
            with self.assertRaises(ToolError):
                amount(value)
        with self.assertRaises(ToolError):
            await snapshot(self.client, '7', '../users')


if __name__ == '__main__':
    unittest.main()
