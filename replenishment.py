"""Read-only replenishment proposals from explicit inventory and all sales pages."""
import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from support_ops import all_orders

TZ=ZoneInfo('America/Argentina/Buenos_Aires')

def validate(data):
    asof=datetime.fromisoformat(data['as_of'])
    if asof.tzinfo is None or asof>datetime.now(timezone.utc)+timedelta(minutes=5):
        raise ValueError('Fecha de inventario inválida o sin zona horaria.')
    rows=data['stock']; recipes=data['listings']
    if not isinstance(rows,list) or not rows or not isinstance(recipes,list): raise ValueError('stock/listings requeridos')
    skus=set()
    for r in rows:
        sku=r['sku']
        if not isinstance(sku,str) or not sku or sku in skus: raise ValueError('SKU inválido/duplicado')
        skus.add(sku)
        for field in ('warehouse_available','full_available','lead_days','safety_days','target_days'):
            if type(r[field]) is not int or r[field]<0: raise ValueError('Cantidades/plazos enteros no negativos')
        if r['target_days']<1: raise ValueError('target_days debe ser positivo')
        for inbound in r.get('inbound',[]):
            if type(inbound['quantity']) is not int or inbound['quantity']<=0: raise ValueError('Ingreso inválido')
            eta=datetime.strptime(inbound['eta'],'%Y-%m-%d').date()
            if eta<datetime.now(TZ).date(): raise ValueError('Actualizar ingreso vencido')
    bindings=set()
    for recipe in recipes:
        key=(recipe['item_id'],str(recipe.get('variation_id') or ''))
        if key in bindings: raise ValueError('Publicación/variante duplicada')
        bindings.add(key)
        if not recipe['components']: raise ValueError('Receta vacía')
        for component in recipe['components']:
            if component['sku'] not in skus or type(component['quantity']) is not int or component['quantity']<=0:
                raise ValueError('Componente inválido')
    return data

async def plan(client,seller,data):
    validate(data)
    now=datetime.now(TZ)
    if now-datetime.fromisoformat(data['as_of'])>timedelta(hours=48):
        raise ValueError('Inventario de más de 48 horas: actualizar antes de recomendar compras.')
    end=now.replace(hour=0,minute=0,second=0,microsecond=0)
    start=end-timedelta(days=28)
    orders=await all_orders(client,seller,start.isoformat(),(end-timedelta(microseconds=1)).isoformat())
    bindings={(r['item_id'],str(r.get('variation_id') or '')):r['components'] for r in data['listings']}
    totals={r['sku']:[0,0] for r in data['stock']}
    missing=set()
    for order in orders:
        if order['status']!='paid': continue
        created=datetime.fromisoformat(order['date_created'].replace('Z','+00:00'))
        if created.tzinfo is None or not start<=created<end: raise ValueError('Venta fuera de rango')
        for line in order['order_items']:
            item=line['item']; key=(item['id'],str(item.get('variation_id') or ''))
            if key not in bindings: missing.add(key); continue
            quantity=line['quantity']
            if type(quantity) is not int or quantity<1: raise ValueError('Cantidad de venta inválida')
            for c in bindings[key]:
                n=quantity*c['quantity']; totals[c['sku']][0]+=n
                if created>=end-timedelta(days=7): totals[c['sku']][1]+=n
    if missing:
        return {'complete':False,'unmapped_listings':[dict(item_id=k[0],variation_id=k[1]) for k in sorted(missing)],
                'recommendations':[], 'reason':'Completar equivalencias, incluidos kits, antes de recomendar.'}
    rows=[]
    today=now.date()
    for stock in data['stock']:
        sold28,sold7=totals[stock['sku']]
        rate=max(sold28/28,sold7/7)
        available=stock['warehouse_available']+stock['full_available']
        coverage=available/rate if rate else None
        lead,safety,target=stock['lead_days'],stock['safety_days'],stock['target_days']
        horizon=lead+safety+target
        balance=float(available); shortage=None; min_before_arrival=float(available)
        for d in range(horizon+1):
            date=today+timedelta(days=d)
            balance+=sum(i['quantity'] for i in stock.get('inbound',[]) if i['eta']==date.isoformat())
            if d>0: balance-=rate
            if d<=lead: min_before_arrival=min(min_before_arrival,balance)
            if balance<0 and shortage is None: shortage=date.isoformat()
        # An early gap cannot be erased by a later incoming shipment.
        reorder_in=0 if min_before_arrival<rate*safety else max(0,math.floor((min_before_arrival-rate*safety)/rate)) if rate else None
        qty=max(0,math.ceil(-balance)) if rate else 0
        rows.append({'sku':stock['sku'],'units_28_days':sold28,'units_7_days':sold7,
          'planning_units_per_day':round(rate,3),'available_now':available,
          'days_coverage_without_inbound':round(coverage,1) if coverage is not None else None,
          'first_projected_shortage':shortage,'order_within_days':reorder_in,
          'suggested_quantity':qty,'bridge_units_before_arrival':max(0,math.ceil(-min_before_arrival)),'new_order_arrival_if_ordered_today':(today+timedelta(days=lead)).isoformat(),
          'gap_before_new_arrival':min_before_arrival<0})
    return {'complete':True,'sales_from':start.isoformat(),'sales_to_exclusive':end.isoformat(),
       'inventory_as_of':data['as_of'],'orders_read':len(orders),'recommendations':rows,
       'assumptions':['Demanda: mayor promedio de 7 y 28 días completos; no corrige días sin stock.',
       'Ingreso previsto no es stock disponible hasta su fecha. Actualizar retrasos.',
       'Depósito y Full deben ser cantidades libres, separadas; tránsito no debe repetirse.',
       'Cobertura combinada no garantiza disponibilidad por canal. No crea compras ni envíos Full.']}
