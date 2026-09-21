import asyncio
import base64
import csv
import hashlib
import io
import json
from copy import deepcopy
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from sales_reports import Reports, dates, destination, project, register, summary


def run(coro):
    return asyncio.run(coro)


def order(i=1, status='paid', sid=10):
    return {'id': i, 'seller': {'id': 7}, 'pack_id': 55, 'status': status,
            'date_created': '2025-09-20T23:30:00-03:00', 'currency_id': 'ARS',
            'shipping': {'id': sid}, 'total_amount': 300.30, 'paid_amount': 9999,
            'order_items': [{'unit_price': 150.15, 'quantity': 2,
                             'item': {'title': 'private product'}}],
            'payments': [{'id': 101, 'status': 'approved', 'transaction_amount_refunded': 0}],
            'buyer': {'email': 'private@example.com'}}


class ML:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [order()]
        self.calls = []
        self.shipment = {'id': 10, 'sender_id': 7, 'receiver_address': {
            'country': {'id': 'AR'}, 'state': {'name': 'Capital Federal'},
            'address_line': 'private address', 'receiver_name': 'private person'}}
        self.error = None

    async def get(self, path, params=None, headers=None):
        self.calls.append((path, params, headers))
        if path == '/orders/search':
            offset = params['offset']
            return {'results': deepcopy(self.rows[offset:offset+50]), 'paging': {'total': len(self.rows)}}
        if self.error:
            raise self.error
        assert path == '/shipments/10'
        return deepcopy(self.shipment)


def finish(store, client):
    rid = store.create('2025-09-01', '2025-09-20')['report_id']
    for _ in range(20):
        status = run(store.advance(client, rid))
        if status['complete']:
            return rid
    raise AssertionError('report did not finish')


def test_period_inclusive_timezone_and_invalid_dates():
    assert dates('2025-09-01', '2025-09-20') == (
        '2025-09-01T00:00:00-03:00', '2025-09-21T00:00:00-03:00')
    for a, b in [('2025-09-20', '2025-09-01'), ('2025-09-01', '2025-10-02'),
                 ('2099-09-01', '2099-09-20'), ('20250901', '2025-09-20')]:
        with pytest.raises(ValueError):
            dates(a, b)


def test_resume_pagination_deduplicate_shipments_and_exact_amounts(tmp_path):
    path = tmp_path/'reports.db'
    store, ml = Reports(path, '7'), ML([order(i) for i in range(1, 52)]+[order(52, 'cancelled')])
    rid = store.create('2025-09-01', '2025-09-20')['report_id']
    assert not run(store.advance(ml, rid))['complete']
    assert store.read(rid)['summary'] is None
    with pytest.raises(ValueError):
        store.download(rid)
    store = Reports(path, '7')
    run(store.advance(ml, rid))
    assert run(store.advance(ml, rid))['complete']
    result = store.read(rid, limit=50)
    s = result['summary']
    assert s['sales_count'] == 51 and s['units'] == 102
    assert s['gross_sales_ars'] == '15315.30'
    assert s['cancelled_orders_count'] == 1
    assert s['geography_complete'] and s['by_province'][0]['province'] == 'CABA'
    assert s['by_province'][0]['revenue_share_pct'] == '100.00'
    assert result['next_offset'] == 50
    assert len(store.read(rid, 50)['rows']) == 2
    assert len([p for p, _, _ in ml.calls if p.startswith('/shipments/')]) == 1
    assert 'private' not in json.dumps(result)
    assert 'private' not in store.load(rid)[1].__str__()
    calls = len(ml.calls)
    assert run(store.advance(ml, rid))['complete']
    assert len(ml.calls) == calls


def test_foreign_order_and_foreign_shipment_fail_closed(tmp_path):
    store, ml = Reports(tmp_path/'r.db', '7'), ML()
    rid = store.create('2025-09-01', '2025-09-20')['report_id']
    ml.rows[0]['seller']['id'] = 8
    with pytest.raises(ValueError):
        run(store.advance(ml, rid))
    assert store.read(rid)['orders_downloaded'] == 0
    ml.rows[0]['seller']['id'] = 7
    run(store.advance(ml, rid))
    ml.shipment['sender_id'] = 8
    with pytest.raises(ValueError):
        run(store.advance(ml, rid))
    assert store.read(rid)['stage'] == 'geography'
    with pytest.raises(ValueError):
        Reports(tmp_path/'r.db', '8').read(rid)


def test_unavailable_province_preserves_denominator_and_refund_flag(tmp_path):
    store, ml = Reports(tmp_path/'r.db', '7'), ML([order(), order(2, sid=None)])
    ml.rows[0]['payments'][0]['transaction_amount_refunded'] = 100
    ml.error = ToolError('HTTP 403 secret provider payload')
    rid = finish(store, ml)
    s = store.read(rid)['summary']
    assert not s['geography_complete']
    assert s['unassigned_sales_count'] == 2
    assert s['unassigned_gross_sales_ars'] == s['gross_sales_ars'] == '600.60'
    assert s['refund_review_count'] == 1
    assert s['by_province'][0]['revenue_share_pct'] == '100.00'
    assert 'secret' not in json.dumps(store.read(rid))


def test_changed_totals_duplicate_and_out_of_range(tmp_path):
    store, ml = Reports(tmp_path/'r.db', '7'), ML([order(i) for i in range(1, 52)])
    rid = store.create('2025-09-01', '2025-09-20')['report_id']
    run(store.advance(ml, rid))
    ml.rows.append(order(52))
    with pytest.raises(ValueError):
        run(store.advance(ml, rid))
    ml.rows.pop()
    ml.rows[-1] = order(1)
    with pytest.raises(ValueError):
        run(store.advance(ml, rid))
    ml.rows[-1] = order(51)
    ml.rows[-1]['date_created'] = '2025-09-21T00:00:00-03:00'
    run(store.advance(ml, rid))
    run(store.advance(ml, rid))
    assert store.read(rid)['summary']['sales_count'] == 50
    assert store.read(rid)['excluded_out_of_range'] == 1


@pytest.mark.parametrize('value', ['NaN', 'Infinity', -1, None])
def test_invalid_money_not_zero(value):
    row = order()
    row['order_items'][0]['unit_price'] = value
    with pytest.raises(ValueError):
        project(row)


def test_amount_mismatch_and_foreign_currency():
    row = order()
    row['total_amount'] = 100
    with pytest.raises(ValueError):
        project(row)
    row = order()
    row['currency_id'] = 'USD'
    with pytest.raises(ValueError):
        project(row)


def test_empty_report_and_immutable_csv_hash(tmp_path):
    store = Reports(tmp_path/'r.db', '7')
    rid = finish(store, ML([]))
    s = store.read(rid)['summary']
    assert s['sales_count'] == 0 and s['by_province'] == []
    result = store.download(rid)
    raw = base64.b64decode(result['data_base64'])
    assert hashlib.sha256(raw).hexdigest() == result['sha256']
    assert result['download_complete']
    assert result == store.download(rid)
    assert len(list(csv.reader(io.StringIO(raw.decode('utf-8-sig')), delimiter=';'))) == 1


def test_chunked_download_reassembles_and_hash_matches(tmp_path):
    store = Reports(tmp_path/'r.db', '7')
    rid = finish(store, ML([order(i) for i in range(1, 602)]))
    chunks, offset, hashes = [], 0, set()
    while True:
        result = store.download(rid, 'ventas', offset)
        chunks.append(base64.b64decode(result['data_base64']))
        hashes.add(result['sha256'])
        if result['download_complete']:
            break
        offset = result['next_offset']
    assert len(chunks) > 1
    raw = b''.join(chunks)
    assert hashes == {hashlib.sha256(raw).hexdigest()}
    assert len(list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig')), delimiter=';'))) == 601


def test_missing_country_or_unknown_state_never_infers_province():
    ml = ML()
    ml.shipment['receiver_address']['state']['name'] = 'UNKNOWN'
    assert run(destination(ml, '7', '10'))['province_status'] == 'unknown_state'
    ml.shipment['receiver_address']['country'] = {}
    assert run(destination(ml, '7', '10'))['province_status'] == 'country_unverified'


def test_caba_separate_from_province_and_decimal_percentages():
    rows = [project(order(i)) for i in range(1, 4)]
    rows[0]['province'] = 'CABA'
    rows[1]['province'] = rows[2]['province'] = 'Buenos Aires'
    s = summary({'rows': rows})
    assert [g['sales_share_pct'] for g in s['by_province']] == ['66.67', '33.33']
    assert s['gross_sales_ars'] == '900.90'


def test_tools_all_require_authentication(tmp_path):
    class MCP:
        def __init__(self): self.tools = {}
        def tool(self, **kwargs):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorator
    def denied():
        raise ToolError('Autorización requerida')
    mcp = MCP()
    register(mcp, denied, '7', tmp_path)
    with pytest.raises(ToolError):
        mcp.tools['nf_ventas_reporte_crear']('2025-09-01', '2025-09-20')
    with pytest.raises(ToolError):
        run(mcp.tools['nf_ventas_reporte_avanzar']('0'*32))
    for name in ('nf_ventas_reporte_leer', 'nf_ventas_reporte_descargar'):
        with pytest.raises(ToolError):
            mcp.tools[name]('0'*32)
