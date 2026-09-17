import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from sales_data import orders_page, all_orders, interval
from profitability import summarize, validate_policy
from monitor import Monitor

DAY='2026-09-17'
START=DAY+'T12:00:00-03:00'
END=DAY+'T12:10:00-03:00'

def order(oid=1, stamp=DAY+'T11:07:00-04:00'):
    return {'id':oid,'seller':{'id':237699011},'date_created':stamp,'status':'paid','currency_id':'ARS',
            'paid_amount':999999, 'order_items':[{'item':{'id':'MLA1','seller_sku':'S'},'quantity':2,'unit_price':'100.10','sale_fee':'10.01'}]}

class Client:
    def __init__(self, pages): self.pages=iter(pages)
    async def get(self, path, params=None, headers=None):
        assert params['order.date_created.from'].endswith('+00:00')
        return next(self.pages)

def page(rows,total=None):return {'results':rows,'paging':{'total':len(rows) if total is None else total}}

def policy():
    return {'currency':'ARS','costs':[{'sku':'S','unit_cost':'30','effective_from':'2026-09-01T00:00:00-03:00','source':'factura'}],
            'orders':{'1':{'refund':'0','logistics':'3','tax_adjustment':'5','source':'liquidacion'}},
            'days':{DAY:{'ads':'10','fixed_costs':'2','source':'balance'}}}

def test_timezone_and_exclusive_upper_bound():
    r=asyncio.run(orders_page(Client([page([order(),order(2,DAY+'T11:10:00-04:00'),order(3,DAY+'T11:40:00-04:00')])]),'237699011',START,END))
    assert [o['id'] for o in r['orders']]==[1]
    assert r['excluded_out_of_range']==2 and r['complete']

def test_filtered_page_does_not_end_pagination():
    rows=[order(i+1,DAY+'T11:40:00-04:00') for i in range(50)]
    r=asyncio.run(orders_page(Client([page(rows,51)]),'237699011',START,END))
    assert r['orders']==[] and r['next_offset']==50 and not r['complete']

@pytest.mark.parametrize('start,end', [('2026-09-01','2026-09-02'),('2026-09-01T00:00:00Z','2026-10-02T00:00:01Z'),(END,START)])
def test_invalid_ranges(start,end):
    with pytest.raises(ValueError):interval(start,end)

def test_wrong_seller():
    r=order();r['seller']['id']=999
    with pytest.raises(ValueError):asyncio.run(orders_page(Client([page([r])]),'237699011',START,END))

def test_short_page():
    with pytest.raises(ValueError):asyncio.run(orders_page(Client([page([order()],51)]),'237699011',START,END))

def test_changing_total():
    rows=[order(i+1) for i in range(50)]
    with pytest.raises(ValueError):asyncio.run(all_orders(Client([page(rows,51),page([order(51),order(52)],52)]),'237699011',START,END))

def test_no_paid_amount_double_count_and_fee_once():
    r=summarize([order()],policy(),DAY)
    assert r['gross']=='200.20' and r['orders'][0]['fee']=='20.02'
    assert r['orders'][0]['margin']=='120.18' and r['net_estimate']=='100.18'

def test_missing_fee_is_not_zero():
    o=order();o['order_items'][0]['sale_fee']=None
    r=summarize([o],policy(),DAY)
    assert r['net_estimate'] is None and r['orders'][0]['fee'] is None

def test_missing_cost_is_not_zero():
    p=policy();p['costs']=[]
    assert summarize([order()],p,DAY)['net_estimate'] is None

def test_historical_cost_not_replaced_by_future_cost():
    p=policy();p['costs'].append({'sku':'S','unit_cost':'300','effective_from':'2026-10-01T00:00:00Z','source':'futuro'})
    assert summarize([order()],p,DAY)['orders'][0]['cogs']=='60.00'

def test_kit_components():
    p=policy();p['kits']={'MLA1:':[{'sku':'S','quantity':3}]}
    assert summarize([order()],p,DAY)['orders'][0]['cogs']=='180.00'

def test_cancellation_preserves_expenses():
    o=order();o['status']='cancelled';p=policy();p['orders']['1'].update(fee='4',cogs='0')
    r=summarize([o],p,DAY)
    assert r['sales_after_known_refunds']=='0.00' and r['cancelled']=='200.20'
    assert r['net_estimate']=='-24.00'

def test_refund_requires_actual_cost_recovery():
    p=policy();p['orders']['1']['refund']='100'
    assert summarize([order()],p,DAY)['net_estimate'] is None
    p['orders']['1']['cogs']='30'
    assert summarize([order()],p,DAY)['net_estimate']=='30.18'

def test_unknown_refunds_block_final_net():
    p=policy();del p['orders']['1']['refund']
    assert summarize([order()],p,DAY)['net_estimate'] is None

def test_duplicate_orders_rejected():
    with pytest.raises(ValueError):summarize([order(),order()],policy(),DAY)

@pytest.mark.parametrize('bad',['NaN','Infinity',True])
def test_invalid_amounts(bad):
    p=policy();p['costs'][0]['unit_cost']=bad
    with pytest.raises(ValueError):validate_policy(p)

def test_one_use_link_cookie_and_revision(tmp_path):
    m=Monitor(tmp_path,None,'237699011','https://nf.example')
    link=m.issue('link',300);cookie=m.exchange(link)
    assert cookie and m.exchange(link) is None
    assert m.authorized(SimpleNamespace(cookies={'nf_monitor':cookie}))
    assert not m.authorized(SimpleNamespace(cookies={'nf_monitor':link}))
    m.configure(policy(),0)
    with pytest.raises(ValueError):m.configure(policy(),0)
    assert Monitor(tmp_path,None,'237699011','https://nf.example').config()['revision']==1

def test_stale_cache_on_failure(tmp_path):
    import json,time
    m=Monitor(tmp_path,None,'237699011','https://nf.example')
    day=datetime.now().date().isoformat()
    with m.db() as c:c.execute('INSERT INTO snapshots VALUES(?,?,?)',(day,json.dumps({'policy_revision':0,'net_estimate':'100','fetched_at':'old'}),time.time()-400))
    assert asyncio.run(m.snapshot(day))['stale'] is True

def test_routes_require_private_session(tmp_path):
    from server import build_app
    from cryptography.fernet import Fernet
    from starlette.testclient import TestClient
    env={'BASE_URL':'https://nf.example','MELI_CLIENT_ID':'123','MELI_CLIENT_SECRET':'test','MELI_SELLER_ID':'237699011',
         'JWT_SIGNING_KEY':'x'*48,'STORAGE_ENCRYPTION_KEY':Fernet.generate_key().decode(),'NF_DATA_DIR':str(tmp_path)}
    app=build_app(env)
    with TestClient(app,base_url='https://nf.example') as c:
        assert c.get('/monitor/data').status_code==401
        assert c.get('/monitor').status_code==200
        assert c.get('/monitor/assets/monitor.js').status_code==200
        assert c.post('/monitor/session',json={'token':'bad'}).status_code==403
        assert c.post('/monitor/session',json={'token':'bad'},headers={'Origin':'https://nf.example'}).status_code==401
