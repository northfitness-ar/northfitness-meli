import asyncio
import pytest
from fastmcp.exceptions import ToolError
from full_tools import item_inventory, stock_detail, operations_page

class Client:
    def __init__(self):
        self.calls=[]
        self.item={'id':'MLA123','seller_id':7,'variations':[{'id':10,'inventory_id':'INV'}]}
        self.stock={'inventory_id':'INV','total':8,'available_quantity':5,'not_available_quantity':3,'not_available_detail':[{'status':'damaged','quantity':3,'conditions':[{'condition':'arrived_damaged','quantity':3}]}]}
        self.ops={'paging':{'scroll':'NEXT','total':2},'results':[{'id':1,'seller_id':7,'inventory_id':'INV','type':'INBOUND_RECEPTION'}]}
    async def get(self,path,params=None):
        self.calls.append((path,params))
        if path.startswith('/items/'):return self.item
        if path.startswith('/inventories/'):return self.stock
        if path.endswith('/search'):return self.ops
        raise AssertionError(path)

def test_full_owned_exact_variant_and_conditions():
    c=Client()
    assert asyncio.run(item_inventory(c,7,'MLA123','10'))=='INV'
    assert asyncio.run(stock_detail(c,'INV'))['not_available_detail'][0]['conditions'][0]['quantity']==3
    with pytest.raises(ToolError):asyncio.run(item_inventory(c,7,'MLA123'))
    with pytest.raises(ToolError):asyncio.run(item_inventory(c,7,'MLA123','11'))
    c.item['seller_id']=8
    with pytest.raises(ToolError):asyncio.run(item_inventory(c,7,'MLA123','10'))
    with pytest.raises(ToolError):asyncio.run(item_inventory(c,7,'../secrets','10'))

def test_operations_pagination_and_ownership():
    c=Client()
    r=asyncio.run(operations_page(c,7,{'INV'},'2026-09-24','2026-09-30'))
    assert not r['complete'] and r['next_scroll']=='NEXT'
    c.ops['paging']['scroll']=None
    r=asyncio.run(operations_page(c,7,{'INV'},'2026-09-24','2026-09-30','NEXT'))
    assert r['complete'] and c.calls[-1][1]['scroll']=='NEXT'
    for field,value in [('seller_id',8),('inventory_id','OTHER')]:
        old=c.ops['results'][0][field];c.ops['results'][0][field]=value
        with pytest.raises(ToolError):asyncio.run(operations_page(c,7,{'INV'},'2026-09-24','2026-09-30'))
        c.ops['results'][0][field]=old
    with pytest.raises(ToolError):asyncio.run(operations_page(c,7,{'INV'},'2026-01-01','2026-09-30'))

def test_unknown_stock_is_not_zero():
    c=Client();c.stock={'inventory_id':'INV','available_quantity':5}
    assert asyncio.run(stock_detail(c,'INV'))['not_available_quantity'] is None
    c.stock['available_quantity']=None
    with pytest.raises(ToolError):asyncio.run(stock_detail(c,'INV'))
