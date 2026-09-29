import asyncio
from datetime import datetime,timedelta
from types import SimpleNamespace
import pytest
from monitor import Monitor
from stock_monitor import StockMonitor,forecast,coverage,TZ

class Auto:
    def __init__(self, data, client): self.data=data; self.api=client; self.config={}
    def get(self,k,default=None): return {'revision':1,'data':self.data} if k=='inventory_snapshot' else self.config.get(k,default)
    def put(self,k,v): self.config[k]=v
    async def client(self): return self.api

class Client:
    fail=False
    async def get(self,path,params=None):
        if path.startswith('/inventories/'):
            if self.fail: raise ValueError('unavailable')
            return {'inventory_id':'INV','available_quantity':5}
        if path.startswith('/shipments/'): return {'sender_id':123,'logistic_type':'cross_docking'}
        raise AssertionError(path)

def setup(tmp_path):
    now=datetime.now(TZ); anchor=now-timedelta(hours=2)
    data={'as_of':anchor.isoformat(),'stock':[{'sku':'A','warehouse_available':10,'full_available':4,'lead_days':45,'safety_days':10,'target_days':30}], 'listings':[],
    'purchase_orders':[{'id':'P','status':'En fabricación','items':[{'sku':'A','quantity':100}]}]}
    client=Client(); auto=Auto(data,client); m=Monitor(tmp_path,auto,'123','https://test.example')
    auto.put('stock_catalog_v1',{'time':__import__('time').time(),'items':[{'item_id':'I1','sku':'A','inventory_id':'INV'},{'item_id':'I2','sku':'A','inventory_id':'INV'}]})
    order={'id':1,'date_created':(now-timedelta(hours=1)).isoformat(),'status':'paid','shipping':{'id':10},'order_items':[{'item':{'id':'I1','seller_sku':'A'},'quantity':2}]}
    async def orders(*a): return [order],0
    m.cached_orders=orders
    return StockMonitor(m,{}),order,client

def test_shared_full_and_nonfull_deductions_and_cancellation(tmp_path):
    s,o,c=setup(tmp_path)
    d=asyncio.run(s.read());r=d['rows'][0]
    assert r['full']==5 and r['warehouse']==8 and r['total']==13
    assert r['pending']==100 # not available
    o['status']='cancelled'
    d=asyncio.run(s.read(True))
    assert d['rows'][0]['warehouse']==8 # no automatic physical return
    c.fail=True
    d=asyncio.run(s.read(True))
    assert d['rows'][0]['full'] is None and d['rows'][0]['total'] is None

def test_receipt_idempotency_and_outstanding_balance(tmp_path):
    s,o,c=setup(tmp_path)
    d=asyncio.run(s.read())
    p={'id':'request-123456789','revision':d['revision'],'kind':'receive','quantity':30,'sku':'A','purchase_id':'P','note':'Receipt UPS'}
    assert asyncio.run(s.move(p,'salvador'))['saved']
    assert asyncio.run(s.move(p,'salvador'))['duplicate']
    d=asyncio.run(s.read(True)); assert d['rows'][0]['warehouse']==38 and d['rows'][0]['pending']==70
    p.update(id='request-99999999',quantity=71,revision=d['revision'])
    with pytest.raises(ValueError): asyncio.run(s.move(p,'salvador'))

def test_count_and_transfer_does_not_increase_full(tmp_path):
    s,o,c=setup(tmp_path);d=asyncio.run(s.read())
    p={'id':'count-123456789','revision':d['revision'],'kind':'count','quantity':20,'sku':'A','note':'Recount physical'}
    asyncio.run(s.move(p,'maxi'));d=asyncio.run(s.read(True));assert d['rows'][0]['warehouse']==20
    p.update(id='transfer-123456',revision=d['revision'],kind='transfer',quantity=3)
    asyncio.run(s.move(p,'maxi'));d=asyncio.run(s.read(True))
    assert d['rows'][0]['warehouse']==17 and d['rows'][0]['full']==5
    p.update(id='stale-123456789')
    with pytest.raises(ValueError): asyncio.run(s.move(p,'maxi'))

def test_forecast_observed_stockouts_and_growth():
    now=datetime.now(TZ).date()
    daily={(now-timedelta(days=i)).isoformat():10 for i in range(8,29)}
    excluded=[(now-timedelta(days=i)).isoformat() for i in range(1,8)]
    rate,factors,_=forecast(daily,now,excluded)
    assert rate==10 and coverage(100,rate,factors,now)==10
    assert coverage(0,0,factors,now)==0 and coverage(100,0,factors,now) is None
    assert abs(sum(factors)-7)<.00001

def test_no_email_without_configuration(tmp_path):
    s,o,c=setup(tmp_path)
    assert not s.mail_ready()
    asyncio.run(s.alerts({'rows':[{'action':'Comprar'}]}))
    with s.m.db() as db: assert db.execute('SELECT count(*) FROM stock_mail').fetchone()[0]==0

def test_reserved_collection_stays_physical_and_dispatch_never_deducts_twice(tmp_path):
    s,o,c=setup(tmp_path)
    row=s.m.auto.data['stock'][0];row['warehouse_available']=25;row['warehouse_reserved']=65
    s.m.auto.data['full_collections']=[{'id':'COL','name':'Thursday','status':'reserved','date':'2026-10-01','items':[{'sku':'A','quantity':65}]}]
    d=asyncio.run(s.read());r=d['rows'][0]
    assert (r['warehouse'],r['warehouse_physical'],r['warehouse_reserved'],r['total'],r['available_now'])==(23,88,65,93,28)
    p={'id':'dispatch-123456','revision':d['revision'],'kind':'collection_dispatch','quantity':50,'sku':'A','collection_id':'COL','note':'Carrier picked up'}
    assert asyncio.run(s.move(p,'maxi'))['saved']
    assert asyncio.run(s.move(p,'maxi'))['duplicate']
    d=asyncio.run(s.read(True));r=d['rows'][0]
    assert (r['warehouse'],r['warehouse_physical'],r['warehouse_reserved'],r['full'])==(23,38,15,5)
    assert d['full_collections'][0]['items'][0]['dispatched']==50
    p.update(id='dispatch-999999',revision=d['revision'],quantity=16)
    with pytest.raises(ValueError):asyncio.run(s.move(p,'maxi'))
    p.update(quantity=15,collection_id='OTHER')
    with pytest.raises(ValueError):asyncio.run(s.move(p,'maxi'))

def test_collection_validation_and_future_purchase_not_physical(tmp_path):
    from replenishment import validate
    s,o,c=setup(tmp_path);data=s.m.auto.data
    data['full_collections']=[{'id':'FUTURE','status':'awaiting_stock','items':[{'sku':'A','quantity':200}]}]
    validate(data)
    d=asyncio.run(s.read());assert d['rows'][0]['warehouse_reserved']==0 and d['rows'][0]['total']==13
    data['stock'][0]['warehouse_reserved']=200
    with pytest.raises(ValueError):validate(data)
    data['full_collections'][0]['status']='reserved'
    validate(data)
