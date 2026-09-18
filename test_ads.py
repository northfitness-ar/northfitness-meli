import asyncio
import copy
import json
import httpx
import pytest
from fastmcp.exceptions import ToolError
from server import AdsChanges, MeliAPI

class Fake:
 def __init__(self):
  self.row={'id':2,'advertiser_id':1,'budget':1000,'currency_id':'ARS','automatic_budget':False,'status':'active','roas_target':5,'strategy':'PROFITABILITY'}
  self.writes=[]; self.accounts=[{'advertiser_id':1,'site_id':'MLA'}]; self.state='accepted'
 async def get(self,path,params=None,headers=None):
  if path.endswith('/advertisers'):return {'advertisers':self.accounts}
  if path.endswith('/search'):return {'results':[copy.deepcopy(self.row)]}
  return copy.deepcopy(self.row)
 async def put_stock(self,path,payload,headers=None):
  self.writes.append((path,payload,headers))
  if self.state=='accepted':self.row.update(payload)
  return {'state':self.state,'http_status':200 if self.state=='accepted' else None}

def test_budget_and_durable_retry(tmp_path):
 async def run():
  f=Fake(); s=AdsChanges(tmp_path/'a.db')
  r=await s.set_budget(f,'1','2','1500','1000','budget001')
  assert r['state']=='verified'
  assert f.writes==[('/advertising/MLA/product_ads/campaigns/2',{'budget':1500.0},{'api-version':'2'})]
  assert await AdsChanges(tmp_path/'a.db').set_budget(f,'1','2','1500','1000','budget001')==r
  assert len(f.writes)==1
 asyncio.run(run())

@pytest.mark.parametrize('case',['account','campaign','currency','auto','stale','nan','negative','zero'])
def test_reject(tmp_path,case):
 async def run():
  f=Fake(); s=AdsChanges(tmp_path/'a.db')
  if case=='account':f.accounts=[]
  if case=='campaign':f.row['advertiser_id']=99
  if case=='currency':f.row['currency_id']='USD'
  if case=='auto':f.row['automatic_budget']=True
  amount={'nan':'NaN','negative':'-1','zero':'0'}.get(case,'1500')
  with pytest.raises(ToolError):await s.set_budget(f,'1','2',amount,'999' if case=='stale' else '1000','budget001')
  assert not f.writes
 asyncio.run(run())

def test_uncertain_not_retried(tmp_path):
 async def run():
  f=Fake(); f.state='unknown'; s=AdsChanges(tmp_path/'a.db')
  assert (await s.set_budget(f,'1','2','1500','1000','budget001'))['state']=='unknown'
  await s.set_budget(f,'1','2','1500','1000','budget001')
  assert len(f.writes)==1
 asyncio.run(run())

def test_transport_header_and_payload():
 async def run():
  def handler(req):
   assert req.headers['api-version']=='2'
   assert req.headers['authorization']=='Bearer test'
   assert json.loads(req.content)=={'budget':1500}
   return httpx.Response(200,json={})
  c=MeliAPI('test',httpx.MockTransport(handler))
  assert (await c.put_stock('/advertising/MLA/product_ads/campaigns/2',{'budget':1500},headers={'api-version':'2'}))['state']=='accepted'
 asyncio.run(run())

@pytest.mark.parametrize('target', ['active', 'paused'])
def test_status_only_and_durable_retry(tmp_path, target):
 async def run():
  f=Fake(); f.row['status']='paused' if target=='active' else 'active'
  expected=f.row['status']; s=AdsChanges(tmp_path/'a.db')
  r=await s.set_status(f,'1','2',target,expected,'1000','5','status001')
  assert r['state']=='verified'
  assert f.writes[0][1]=={'status':target}
  assert f.row['budget']==1000 and f.row['roas_target']==5
  assert await AdsChanges(tmp_path/'a.db').set_status(f,'1','2',target,expected,'1000','5','status001')==r
  assert len(f.writes)==1
 asyncio.run(run())

@pytest.mark.parametrize('field,value', [('status','active'),('budget',2000),('roas_target',6),('automatic_budget',True),('advertiser_id',99),('currency_id','USD')])
def test_activation_rejects_stale_or_unauthorized(tmp_path, field, value):
 async def run():
  f=Fake(); f.row['status']='paused'; f.row[field]=value
  with pytest.raises(ToolError):
   await AdsChanges(tmp_path/'a.db').set_status(f,'1','2','active','paused','1000','5','status001')
  assert not f.writes
 asyncio.run(run())

def test_pause_allowed_with_automatic_budget(tmp_path):
 async def run():
  f=Fake(); f.row['automatic_budget']=True
  r=await AdsChanges(tmp_path/'a.db').set_status(f,'1','2','paused','active','1000','5','status001')
  assert r['state']=='verified'
 asyncio.run(run())

def test_roas_only(tmp_path):
 async def run():
  f=Fake(); r=await AdsChanges(tmp_path/'a.db').set_roas(f,'1','2','6','5','roas0001')
  assert r['state']=='verified' and f.writes[0][1]=={'roas_target':6.0}
  assert f.row['budget']==1000 and f.row['status']=='active'
 asyncio.run(run())

@pytest.mark.parametrize('value',['0','-1','NaN','Infinity','5.001'])
def test_invalid_roas(tmp_path,value):
 async def run():
  f=Fake()
  with pytest.raises(ToolError):await AdsChanges(tmp_path/'a.db').set_roas(f,'1','2',value,'5','roas0001')
  assert not f.writes
 asyncio.run(run())

def test_strategy_and_operation_collision(tmp_path):
 async def run():
  f=Fake(); s=AdsChanges(tmp_path/'a.db')
  f.row['strategy']='OTHER'
  with pytest.raises(ToolError):await s.set_roas(f,'1','2','6','5','roas0001')
  f.row['strategy']='PROFITABILITY'
  await s.set_roas(f,'1','2','6','5','roas0001')
  with pytest.raises(ToolError):await s.set_roas(f,'1','2','7','6','roas0001')
  assert len(f.writes)==1
 asyncio.run(run())

@pytest.mark.parametrize('mode',['timeout','500','mismatch','read_failure','403'])
def test_failed_write_not_reported_success_or_retried(tmp_path,mode):
 class Fail(Fake):
  async def put_stock(self,path,payload,headers=None):
   self.writes.append((path,payload,headers))
   if mode=='mismatch':self.row.update(payload);self.row['budget']=999
   if mode=='read_failure':self.row.update(payload)
   return {'state':'unknown' if mode=='timeout' else ('rejected' if mode in ('500','403') else 'accepted'),
           'http_status':{'timeout':None,'500':500,'403':403}.get(mode,200)}
  async def get(self,*args,**kwargs):
   if self.writes and mode in ('read_failure','403'):raise ToolError('Read not allowed')
   return await super().get(*args,**kwargs)
 async def run():
  f=Fail(); f.row['status']='paused'; s=AdsChanges(tmp_path/'a.db')
  r=await s.set_status(f,'1','2','active','paused','1000','5','status001')
  assert r['state']=={'timeout':'unknown','500':'unknown','mismatch':'verification_mismatch','read_failure':'accepted','403':'rejected'}[mode]
  assert await AdsChanges(tmp_path/'a.db').set_status(f,'1','2','active','paused','1000','5','status001')==r
  assert len(f.writes)==1
  if mode in ('timeout','500'):
   with pytest.raises(ToolError):await s.set_budget(f,'1','2','2000','1000','budget002')
   assert len(f.writes)==1
 asyncio.run(run())

def test_unchanged_no_write(tmp_path):
 async def run():
  f=Fake(); r=await AdsChanges(tmp_path/'a.db').set_status(f,'1','2','active','active','1000','5','status001')
  assert r['state']=='unchanged' and not f.writes
 asyncio.run(run())

def test_reservation_across_instances(tmp_path):
 s1=AdsChanges(tmp_path/'a.db'); s2=AdsChanges(tmp_path/'a.db')
 base={'advertiser_id':'1','campaign_id':'2'}
 assert s1.reserve('status001','request',base) is None
 assert s2.reserve('status001','request',base)['state']=='unknown'
 with pytest.raises(ToolError):s2.reserve('status002','other',base)
 with pytest.raises(ToolError):s2.reserve('status001','other',base)

@pytest.mark.parametrize('status,body,codes',[
 (401,{'error':'invalid_token','message':'secret bearer credential'},['invalid_token']),
 (403,{'code':'forbidden','cause':[{'code':'insufficient_scope'}]},['forbidden','insufficient_scope']),
 (401,{'error':'secret-value','access_token':'secret-value'},[]),
 (401,['unexpected','secret-value'],[]),
 (500,{'error':'bad_request'},['bad_request']),
])
def test_write_errors_preserve_only_safe_diagnostics(status,body,codes):
 async def run():
  count=0
  def handler(req):
   nonlocal count
   count+=1
   return httpx.Response(status,json=body)
  r=await MeliAPI('test',httpx.MockTransport(handler)).put_stock('/test',{'budget':2000})
  assert r['state']==('unknown' if status>=500 else 'rejected')
  assert r['provider_error_codes']==codes and count==1
  assert 'secret' not in json.dumps(r)
  if status in (401,403):assert r['retry_allowed'] is False
 asyncio.run(run())
