import asyncio
import copy
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from sales_data import orders_page, all_orders, interval
from profitability import summarize, validate_policy, amount
from monitor import Monitor, TZ

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
    result=summarize([order()],p,DAY)
    assert result['orders'][0]['cogs']=='180.00'
    assert result['sold_products'][0]['variants'][0]['unit_cost']=='90.00'
    assert result['sold_products'][0]['total_cost']=='180.00'

def test_products_group_only_with_explicit_mapping_and_exclude_cancelled():
    first=order()
    first['order_items'][0]['item'].update(title='Guantes',variation_id=10,
        variation_attributes=[{'name':'Color','value_name':'Negro'},{'name':'Talle','value_name':'M'}])
    second=order(2);second['order_items'][0]['item'].update(id='MLA2',title='Guantes NF',variation_id=20)
    cancelled=order(3);cancelled['status']='cancelled'
    p=policy();p['orders'].update({
        '2':{'refund':'0','logistics':'0','tax_adjustment':'0','source':'liquidacion'},
        '3':{'fee':'0','cogs':'0','logistics':'0','tax_adjustment':'0','source':'liquidacion'}})
    p['products']={'MLA1:10':{'name':'Guantes genéricos','variant':'Negro · M'},
                   'MLA2:20':{'name':'Guantes NF','variant':'Negro · M'}}
    result=summarize([first,second,cancelled],p,DAY)
    assert [(x['product'],x['units']) for x in result['sold_products']]==[
        ('Guantes genéricos',2),('Guantes NF',2)]
    assert result['sold_units']==4 and result['merchandise_cost']=='120.00'

def test_missing_product_cost_never_returns_partial_total():
    p=policy();p['costs']=[]
    result=summarize([order()],p,DAY)
    variant=result['sold_products'][0]['variants'][0]
    assert variant['unit_cost'] is None and variant['total_cost'] is None
    assert result['sold_products'][0]['total_cost'] is None
    assert result['merchandise_cost'] is None

def test_publications_unify_only_through_verified_product_map():
    one=order();one['order_items'][0]['item'].update(title='Producto',variation_id=1)
    two=order(2);two['order_items'][0]['item'].update(id='MLA2',title='Producto',variation_id=2)
    p=policy();p['orders']['2']={'refund':'0','logistics':'0','tax_adjustment':'0','source':'liquidacion'}
    separate=summarize([one,two],p,DAY)
    assert len(separate['sold_products'])==2
    p['products']={'MLA1:1':{'name':'Producto verificado','variant':'Rojo · M'},
                   'MLA2:2':{'name':'Producto verificado','variant':'Rojo · M'}}
    unified=summarize([one,two],p,DAY)
    assert len(unified['sold_products'])==1
    assert unified['sold_products'][0]['variants'][0]['units']==4

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


def test_permanent_link_is_stable_and_reusable(tmp_path):
    secret = 'stable-signing-key'
    first = Monitor(tmp_path, None, '237699011', 'https://nf.example', secret)
    link = first.permanent_url()
    token = link.split('#', 1)[1]
    first_cookie = first.exchange(token)
    second_cookie = first.exchange(token)
    assert first_cookie and second_cookie and first_cookie != second_cookie
    assert first.authorized(SimpleNamespace(cookies={'nf_monitor': first_cookie}))
    assert first.authorized(SimpleNamespace(cookies={'nf_monitor': second_cookie}))
    restarted = Monitor(tmp_path, None, '237699011', 'https://nf.example/', secret)
    assert restarted.permanent_url() == link
    assert restarted.exchange(token)
    assert Monitor(tmp_path, None, '237699011', 'https://nf.example', 'rotated').permanent_url() != link

def test_stale_cache_on_failure(tmp_path):
    import json,time
    m=Monitor(tmp_path,None,'237699011','https://nf.example')
    day=datetime.now(TZ).date().isoformat()
    with m.db() as c:c.execute('INSERT INTO snapshots VALUES(?,?,?)',(day,json.dumps({'policy_revision':0,'net_estimate':'100','fetched_at':'old'}),time.time()-400))
    assert asyncio.run(m.snapshot(day))['stale'] is True

def test_snapshot_recalculates_old_schema_and_matches_period_logistics(tmp_path, monkeypatch):
    import json, time
    class Auto:
        async def client(self): return object()
    async def read(*args): return [order()], 0
    async def shipping(self, client, rows, configured):
        enriched = copy.deepcopy(configured)
        enriched['orders']['1']['logistics'] = '19.00'
        return enriched
    monkeypatch.setattr('monitor.all_orders', read)
    monkeypatch.setattr(Monitor, 'shipping_policy', shipping)
    m = Monitor(tmp_path, Auto(), '237699011', 'https://nf.example')
    m.configure(management_policy(), 0)
    with m.db() as c:
        c.execute('INSERT INTO snapshots VALUES(?,?,?)', (DAY, json.dumps({'policy_revision': 1, 'fetched_at': 'old'}), time.time()))
    daily = asyncio.run(m.snapshot(DAY))
    period = asyncio.run(m.period(DAY))
    assert daily['calculation_version'] == 2
    assert daily['orders'][0]['logistics'] == '19.00'
    assert daily['management_estimate']['result'] == period['management_estimate']['result']

def test_forced_snapshot_fetches_new_data_on_every_call(tmp_path, monkeypatch):
    """The web refresh path must never reuse the five-minute background cache."""
    calls = []

    class Auto:
        async def client(self):
            return object()

    async def fresh_orders(client, seller, start, end):
        calls.append((client, seller, start, end))
        current = order(len(calls), datetime.now(timezone.utc).isoformat())
        current['order_items'][0]['unit_price'] = str(100 * len(calls))
        return [current], 0

    async def no_ads(self, client, day):
        return None

    monkeypatch.setattr('monitor.all_orders', fresh_orders)
    monkeypatch.setattr(Monitor, 'ads', no_ads)
    monitor = Monitor(tmp_path, Auto(), '237699011', 'https://nf.example')
    day = datetime.now(TZ).date().isoformat()

    first = asyncio.run(monitor.snapshot(day, force=True))
    second = asyncio.run(monitor.snapshot(day, force=True))

    assert first['gross'] == '200.00'
    assert second['gross'] == '400.00'
    assert len(calls) == 2

def test_closed_day_uses_only_ads_saved_for_requested_date():
    p = policy()
    p['management_estimate'] = {'effective_from': '2026-09-01', 'source': 'criterio gerencial',
                                'refunds': 'exclude', 'ads': 'closed_day',
                                'check_tax_rate': '0', 'iibb_rate': '0'}
    other_day = '2026-09-16'
    p['days'][other_day] = {'fixed_costs': '2', 'source': 'balance'}

    closed = summarize([order()], p, DAY, ads_reported=amount('999'))['management_estimate']
    still_open = summarize([order()], p, other_day, ads_reported=amount('999'))['management_estimate']

    assert closed['ads_included'] is True
    assert closed['result'] == '105.18'
    assert still_open['ads_included'] is False
    assert still_open['result'] == '115.18'

def test_routes_require_private_session(tmp_path, monkeypatch):
    # In-process ASGI tests do not use the execution environment proxy.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    from server import build_app
    from cryptography.fernet import Fernet
    from starlette.testclient import TestClient
    env={'BASE_URL':'https://nf.example','MELI_CLIENT_ID':'123','MELI_CLIENT_SECRET':'test','MELI_SELLER_ID':'237699011',
         'JWT_SIGNING_KEY':'x'*48,'STORAGE_ENCRYPTION_KEY':Fernet.generate_key().decode(),'NF_DATA_DIR':str(tmp_path)}
    app=build_app(env)
    with TestClient(app,base_url='https://nf.example') as c:
        runtime = c.get('/healthz').json()['runtime']
        assert runtime['rss_bytes'] > 0 and runtime['asyncio_tasks'] >= 1
        assert c.get('/monitor/data').status_code==401
        assert c.get('/monitor').status_code==200
        assert c.get('/monitor/assets/monitor.js').status_code==200
        logo=c.get('/monitor/assets/northfitness-logo.jpg')
        assert logo.status_code==200 and logo.headers['content-type']=='image/jpeg'
        assert hashlib.sha256(logo.content).hexdigest()=='52c0a5f2db09d9d36881ff8ba3f8a9f3f7f461e9eaa78bd1b74b36b027b786d6'
        assert "img-src 'self'" in logo.headers['content-security-policy']
        assert c.post('/monitor/session',json={'token':'bad'}).status_code==403
        assert c.post('/monitor/session',json={'token':'bad'},headers={'Origin':'https://nf.example'}).status_code==401
    assert app.state.nf_http_client.is_closed


def management_policy():
    p=policy()
    p['management_estimate']={'effective_from':'2026-09-01','source':'titular','refunds':'exclude','ads':'closed_day','check_tax_rate':'.006','iibb_rate':'.02'}
    return p

def test_net_tax_base_excludes_cancelled():
    p=management_policy();cancelled=order(2);cancelled['status']='cancelled'
    r=summarize([order(),cancelled],p,DAY)
    assert r['management_estimate']['tax_base']=='200.20'
    assert r['management_estimate']['check_tax']=='1.20'
    assert r['management_estimate']['iibb']=='4.00'
    assert r['sold_units']==2

def test_period_cost_dates_weighted_and_fixed_ads():
    from profitability import summarize_period
    p=management_policy();p['products']={'MLA1:':{'name':'NF','variant':'Negro'}}
    p['fixed_cost_schedule']=[{'effective_from':'2026-09-01','daily_cost':'100','source':'titular'}, {'effective_from':'2026-10-01','daily_cost':'40','source':'titular'}]
    p['costs'].append({'sku':'S','unit_cost':'50','effective_from':'2026-09-18T00:00:00-03:00','source':'cambio'})
    start=datetime(2026,9,17,tzinfo=TZ);end=start+timedelta(days=2)
    r=summarize_period([order(),order(2,'2026-09-18T10:00:00-03:00')],p,start,end)
    assert r['merchandise_cost']=='160.00'
    assert r['sold_products'][0]['variants'][0]['unit_cost']=='40.00'
    assert r['fixed_costs']=='102.00'  # explicit daily override preserved
    assert r['ads']=='10.00' and r['ads_missing_days']==1
    r=summarize_period([],p,datetime(2026,10,1,tzinfo=TZ),datetime(2026,10,3,tzinfo=TZ))
    assert r['fixed_costs']=='80.00'

def test_period_missing_cost_not_partial_and_original_cancel_day():
    from profitability import summarize_period
    p=management_policy();old=order(2,'2026-09-16T10:00:00-03:00');old['status']='cancelled'
    start=datetime(2026,9,17,tzinfo=TZ)
    r=summarize_period([order(),old],p,start,start+timedelta(days=1))
    assert r['cancelled']=='0.00' and r['sold_units']==2
    unknown=order(3);unknown['order_items'][0]['item']['seller_sku']='UNKNOWN'
    r=summarize_period([order(),unknown],p,start,start+timedelta(days=1))
    assert r['merchandise_cost'] is None and r['management_estimate']['result'] is None

def test_period_monday_bounds_comparison_and_failed_refresh(tmp_path,monkeypatch):
    calls=[]
    class Auto:
        async def client(self): return object()
    async def read(client,seller,start,end):
        calls.append((start,end));return [],0
    monkeypatch.setattr('monitor.all_orders',read)
    m=Monitor(tmp_path,Auto(),'237699011','https://nf.example');m.configure(management_policy(),0)
    result=asyncio.run(m.period(DAY,'week'))
    assert calls[0][0].startswith('2026-09-14T00:00:00-03:00')
    assert calls[1][0].startswith('2026-09-07T00:00:00-03:00')
    assert datetime.fromisoformat(calls[0][1])-datetime.fromisoformat(calls[1][1])==timedelta(days=7)
    async def fail(*args):raise RuntimeError('offline')
    monkeypatch.setattr('monitor.all_orders',fail)
    stale=asyncio.run(m.period(DAY,'week'))
    assert stale['stale'] and stale['fetched_at']==result['fetched_at']

def test_shipping_pack_cost_once_cached_and_unknown_not_zero(tmp_path):
    class Client:
        def __init__(self):self.calls=0
        async def get(self,path):
            self.calls+=1
            return {'senders':[{'user_id':237699011,'cost':'9.99'}]} if path.endswith('/costs') else [{'order_id':1},{'order_id':2}]
    m=Monitor(tmp_path,None,'237699011','https://nf.example');client=Client()
    rows=[order(),order(2)]
    for o in rows:o['shipping']={'id':123}
    p=policy();p['orders']={}
    enriched=asyncio.run(m.shipping_policy(client,rows,p))
    assert sum(amount(x['logistics']) for x in enriched['orders'].values())==amount('9.99')
    assert p['orders']=={}
    asyncio.run(m.shipping_policy(client,rows,p));assert client.calls==2
    # A pack containing orders outside the selected range is not fully charged to one order.
    (tmp_path/'other').mkdir()
    m2=Monitor(tmp_path/'other',None,'237699011','https://nf.example')
    partial=asyncio.run(m2.shipping_policy(client,rows[:1],p))
    assert 'logistics' not in partial['orders'].get('1', {})

def test_month_calendar_boundaries_and_identical_duration(tmp_path,monkeypatch):
    calls=[]
    class Auto:
        async def client(self):return object()
    async def read(client,seller,start,end):calls.append((start,end));return [],0
    monkeypatch.setattr('monitor.all_orders',read)
    m=Monitor(tmp_path,Auto(),'237699011','https://nf.example');m.configure(management_policy(),0)
    asyncio.run(m.period('2026-08-15','month'))
    assert calls[0]==('2026-08-01T00:00:00-03:00','2026-09-01T00:00:00-03:00')
    assert datetime.fromisoformat(calls[0][0])-datetime.fromisoformat(calls[1][0])==timedelta(days=28)
    assert datetime.fromisoformat(calls[0][1])-datetime.fromisoformat(calls[1][1])==timedelta(days=28)

def test_html_controls_have_script_targets():
    from html.parser import HTMLParser
    from pathlib import Path
    import re
    class IDs(HTMLParser):
        def __init__(self):super().__init__();self.ids=[]
        def handle_starttag(self,tag,attrs):
            self.ids.extend(v for k,v in attrs if k=='id')
    parser=IDs();parser.feed(Path('monitor.html').read_text())
    assert len(parser.ids)==len(set(parser.ids))
    assert set(re.findall(r"\$\('([^']+)'\)",Path('monitor.js').read_text()))<=set(parser.ids)
    assert 'history.replaceState' not in Path('monitor.js').read_text()
