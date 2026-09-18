import asyncio
from copy import deepcopy

import pytest
from fastmcp.exceptions import ToolError
from financial_reads import billing, payment_summary, reconcile_order
from sales_data import orders_page
from financial_reads import release_timing
from datetime import datetime, timezone


def run(coro):
    return asyncio.run(coro)


def order():
    return {'id': 101, 'seller': {'id': 7}, 'pack_id': 900, 'status': 'cancelled',
            'date_created': '2026-09-15T12:00:00-03:00', 'currency_id': 'ARS',
            'payments': [{'id': 201, 'status': 'approved', 'transaction_amount': 1000,
                          'payer': {'email': 'private@example.org'}}]}


class ML:
    def __init__(self, rows=None, evidence=None):
        self.rows = rows or {'101': order()}
        self.evidence = evidence if evidence is not None else {'results': []}
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, params))
        if path == '/orders/search':
            return {'results': list(self.rows.values()), 'paging': {'total': len(self.rows)}}
        if path.startswith('/orders/'):
            return deepcopy(self.rows[path.split('/')[-1]])
        return deepcopy(self.evidence)


class MP:
    def __init__(self):
        self.verified = False
        self.calls = []
        self.payment = {'id': 201, 'collector_id': 7, 'transaction_amount': 1000,
                        'transaction_amount_refunded': 400, 'currency_id': 'ARS',
                        'payer': {'email': 'private@example.org'},
                        'charges_details': [{'id': 'charge-1', 'name': 'ml_fee',
                                             'amounts': {'original': 200, 'refunded': 80}}]}
        self.refunds = [{'id': 301, 'payment_id': 201, 'amount': 400, 'status': 'approved',
                         'source': {'name': 'private person'}}]

    async def verify(self):
        self.verified = True

    async def request(self, method, path):
        assert self.verified
        assert method == 'GET'
        self.calls.append(path)
        return deepcopy(self.refunds if path.endswith('/refunds') else self.payment)


def test_partial_refund_is_evidence_not_zeroed_cancelled_sale():
    result = run(reconcile_order(ML(), MP(), '7', '101'))
    p = result['payments'][0]
    assert result['order']['status'] == 'cancelled'
    assert p['transaction_amount_refunded'] == 400
    assert p['charges_details'][0]['amounts'] == {'original': 200, 'refunded': 80}
    assert p['refunds'][0]['amount'] == 400
    assert 'private' not in str(result)
    assert result['financial_status'] == 'evidence_available_pending_reconciliation'


def test_foreign_order_rejected_before_billing_or_mp():
    raw = order()
    raw['seller']['id'] = 8
    ml, mp = ML({'101': raw}), MP()
    with pytest.raises(ToolError):
        run(reconcile_order(ml, mp, '7', '101'))
    assert not mp.verified
    with pytest.raises(ToolError):
        run(billing(ml, '7', ['101']))
    assert all(path.startswith('/orders/') for path, _ in ml.calls)


def test_foreign_payment_rejected_before_refund_read():
    mp = MP()
    mp.payment['collector_id'] = 8
    with pytest.raises(ToolError):
        run(reconcile_order(ML(), mp, '7', '101'))
    assert mp.calls == ['/v1/payments/201']


@pytest.mark.parametrize('change', ['duplicate', 'foreign_payment'])
def test_bad_refund_evidence_rejected(change):
    mp = MP()
    if change == 'duplicate':
        mp.refunds *= 2
    else:
        mp.refunds[0]['payment_id'] = 202
    with pytest.raises(ToolError):
        run(reconcile_order(ML(), mp, '7', '101'))


def test_unavailable_payments_are_not_empty_or_zero():
    raw = order()
    del raw['payments']
    mp = MP()
    result = run(reconcile_order(ML({'101': raw}), mp, '7', '101'))
    assert result['payments'] is None
    assert result['financial_status'] == 'pending'
    assert not mp.verified


def test_repeated_payments_rejected_but_shared_pack_payments_not_added():
    raw = order()
    raw['payments'] *= 2
    with pytest.raises(ToolError):
        payment_summary(raw)
    second = order()
    second['id'] = 102
    page = run(orders_page(ML({'101': order(), '102': second}), '7',
                           '2026-09-15T00:00:00-03:00', '2026-09-16T00:00:00-03:00'))
    assert [r['payments'][0]['id'] for r in page['orders']] == [201, 201]
    assert 'private' not in str(page)


def test_empty_billing_is_pending_not_zero():
    result = run(billing(ML(), '7', ['101']))
    assert result['fixed_fee_total'] is None
    assert result['variable_fee_total'] is None
    assert result['complete'] is False


def test_billing_preserves_charge_identifiers_and_omits_private_data():
    evidence = {'results': [{'order_id': 101, 'details': [
        {'charge_info': {'charge_id': 'charge-1', 'charge_amount': 200},
         'payer': {'email': 'private@example.org'}}]}]}
    ml = ML(evidence=evidence)
    result = run(billing(ml, '7', ['101']))
    assert result['evidence']['results'][0]['details'][0]['charge_info']['charge_id'] == 'charge-1'
    assert 'private' not in str(result)
    assert result['omitted_field_count'] == 1
    assert ml.calls[-1][1] == {'order_ids': '101', 'seller_id': '7'}


@pytest.mark.parametrize('ids', [['101', '101'], ['101/../../users'], [], ['101'] * 21])
def test_invalid_batch_does_not_call_api(ids):
    ml = ML()
    with pytest.raises(ToolError):
        run(billing(ml, '7', ids))
    assert not ml.calls


@pytest.mark.parametrize('state,date,classification,days', [
    ('released', '2026-09-12T10:00:00-03:00', 'released', 2),
    ('released', '2026-10-12T10:00:00-03:00', 'inconsistent_release_date', None),
    ('released', None, 'inconsistent_release_date', None),
    ('released', '2026-09-12T10:00:00', 'inconsistent_release_date', None),
    ('pending', '2026-09-12T10:00:00-03:00', 'pending', None),
    ('held', '2026-09-22T10:00:00-03:00', 'pending', None),
    (None, '2026-09-12T10:00:00-03:00', 'unknown', None),
])
def test_release_date_needs_confirmed_status(state, date, classification, days):
    p = {'status': 'approved', 'transaction_amount_refunded': 0,
         'money_release_status': state, 'money_release_date': date}
    r = release_timing(p, '2026-09-10T10:00:00-03:00', datetime(2026,9,18,13,tzinfo=timezone.utc))
    assert r['classification'] == classification
    assert r['days_sale_to_reported_release'] == days
    assert r['included_in_completed_average'] == (classification == 'released')
    assert r['pending_age_days'] == (8 if classification == 'pending' else None)


def test_refunded_and_partial_refund_excluded_from_normal_release_average():
    p = {'status': 'refunded', 'transaction_amount_refunded': 100,
         'money_release_status': 'released', 'money_release_date': '2026-09-12T13:00:00Z'}
    now = datetime(2026,9,18,13,tzinfo=timezone.utc)
    assert release_timing(p, '2026-09-10T13:00:00Z', now)['classification'] == 'excluded_payment_status'
    p['status'] = 'approved'
    assert release_timing(p, '2026-09-10T13:00:00Z', now)['classification'] == 'refund_review'
