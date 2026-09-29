"""Private inventory view and journal. Never writes inventory to Mercado Libre."""
import asyncio
import copy
import hashlib
import html
import json
import math
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from full_tools import stock_detail, operations_page, LIMITATION

TZ = ZoneInfo('America/Argentina/Buenos_Aires')

def stamp(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Fecha sin zona horaria.')
    return d

def forecast(daily, today, excluded=()):
    """Recent completed days; exclude observed stockouts, shrink weekday noise."""
    excluded = set(excluded)
    rates = []
    for n in (7, 14, 28):
        dates = [today-timedelta(days=i) for i in range(1,n+1)]
        dates = [d for d in dates if d.isoformat() not in excluded]
        rates.append(sum(daily.get(d.isoformat(), 0) for d in dates)/len(dates) if dates else None)
    valid = [(r,w) for r,w in zip(rates,(.6,.3,.1)) if r is not None]
    rate = sum(r*w for r,w in valid)/sum(w for r,w in valid) if valid else 0
    # Sustained growth must not be diluted by older low-volume days.
    if rates[0] is not None and rates[1] is not None and rates[0]>rates[1]:
        rate = max(rate, .85*rates[0])
    total = sum(daily.values())
    factors = []
    for weekday in range(7):
        dates = [today-timedelta(days=i) for i in range(1,29)
                 if (today-timedelta(days=i)).weekday()==weekday
                 and (today-timedelta(days=i)).isoformat() not in excluded]
        avg = sum(daily.get(d.isoformat(),0) for d in dates)/len(dates) if dates else rate
        overall = rates[2] or rate
        factor = .7+.3*avg/overall if overall and len(dates)>=3 and total>=28 else 1
        factors.append(max(.65,min(1.4,factor)))
    norm = sum(factors)/7
    return rate, [f/norm for f in factors], rates

def coverage(quantity, rate, factors, today):
    if quantity is None: return None
    if quantity<=0: return 0
    if rate<=0: return None
    balance = quantity
    for day in range(730):
        demand = rate*factors[(today+timedelta(days=day)).weekday()]
        if balance<=demand: return round(day+balance/demand,1)
        balance-=demand
    return 730

class StockMonitor:
    def __init__(self, monitor, env):
        self.m, self.env = monitor, env
        self.lock = asyncio.Lock()
        self.cache = None
        with self.m.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS stock_events(
                id TEXT PRIMARY KEY, created TEXT, actor TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS stock_sales(order_id TEXT, sku TEXT, created TEXT, quantity INTEGER, PRIMARY KEY(order_id,sku));
                CREATE TABLE IF NOT EXISTS stock_mail(day TEXT PRIMARY KEY, state TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS stock_observations(day TEXT, sku TEXT, qty INTEGER, first_at TEXT, last_at TEXT,
                PRIMARY KEY(day,sku));''')

    def events(self):
        with self.m.db() as c:
            return [dict(id=r[0],created=r[1],actor=r[2],**json.loads(r[3])) for r in
                    c.execute('SELECT id,created,actor,body FROM stock_events ORDER BY created,id')]

    def initial(self):
        return self.m.auto.get('inventory_snapshot', {'revision':0,'data':None})

    def revision(self, snapshot, events):
        return hashlib.sha256(json.dumps([snapshot,events],sort_keys=True).encode()).hexdigest()

    def mail_ready(self):
        return all(self.env.get(k) for k in ('NF_SMTP_HOST','NF_SMTP_USER','NF_SMTP_PASSWORD','NF_SMTP_FROM','NF_ALERT_EMAIL_TO'))

    async def catalog(self, client, rows):
        # Reuse a seller-verified catalog only briefly; shared inventories are deduplicated.
        cached = self.m.auto.get('stock_catalog_v1', {})
        if time.time()-cached.get('time',0)<1800: return cached['items']
        ids, cursor = [], None
        for _ in range(100):
            params={'search_type':'scan','limit':100}
            if cursor: params['scroll_id']=cursor
            page=await client.get(f'/users/{self.m.seller}/items/search',params)
            got=page.get('results')
            if not isinstance(got,list): raise ValueError('Catálogo ML incompleto.')
            ids.extend(got)
            if len(ids)>=page.get('paging',{}).get('total',0) or not got: break
            cursor=page.get('scroll_id')
            if not cursor: raise ValueError('Falta página de catálogo.')
        else: raise ValueError('Catálogo demasiado grande.')
        items=[]
        for item_id in sorted(set(ids)):
            item=await client.get('/items/'+item_id)
            if str(item.get('seller_id'))!=str(self.m.seller): raise ValueError('Publicación ajena.')
            for v in item.get('variations') or [item]:
                attrs=v.get('attributes',[])
                sku=v.get('seller_custom_field') or next((a.get('value_name') for a in attrs if a.get('id')=='SELLER_SKU'),None)
                items.append({'item_id':item_id,'variation_id':v.get('id') if v is not item else None,
                              'sku':sku,'inventory_id':v.get('inventory_id'),
                              'logistic_type':item.get('shipping',{}).get('logistic_type')})
        self.m.auto.put('stock_catalog_v1',{'time':time.time(),'items':items})
        return items

    async def read(self, force=False):
        async with self.lock:
            if not force and self.cache and self.cache.get('snapshot_revision')==self.initial().get('revision') and time.time()-self.cache['time']<300:
                return copy.deepcopy(self.cache['data'])
            snapshot=self.initial()
            if not snapshot.get('data'):
                return {'rows':[],'pending':[],'error':'Inventario inicial pendiente de carga.','email_ready':self.mail_ready()}
            data=snapshot['data']; now=datetime.now(TZ); today=now.date()
            events=self.events(); revision=self.revision(snapshot,events)
            source_rows=copy.deepcopy(data['stock'])
            bysku={r['sku']:r for r in source_rows}
            aliases={s:r['sku'] for r in source_rows for s in [r['sku']]+r.get('aliases',[])}
            bindings={(r['item_id'],str(r.get('variation_id') or '')):r['components'] for r in data.get('listings',[])}
            def components(item):
                key=(item['id'],str(item.get('variation_id') or ''))
                if key in bindings: return bindings[key]
                sku=aliases.get(item.get('seller_sku'))
                return [{'sku':sku,'quantity':1}] if sku else []
            client=await self.m.auto.client()
            start=now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=28)
            orders,_=await self.m.cached_orders(client,start,now,force)
            missing=set(); daily={s:{} for s in bysku}
            warehouse={s:r['warehouse_available'] for s,r in bysku.items()}
            full={}; full_details={}; full_errors=[]; invs={s:set() for s in bysku}; used={}
            catalog=[]
            try:
                catalog=await self.catalog(client,source_rows)
                for entry in catalog:
                    cs=bindings.get((entry['item_id'],str(entry.get('variation_id') or '')))
                    sku=aliases.get(entry.get('sku'))
                    if not cs and sku: cs=[{'sku':sku,'quantity':1}]
                    if not cs or len(cs)!=1 or cs[0]['quantity']!=1: continue
                    sku=cs[0]['sku']; inv=entry.get('inventory_id')
                    if inv and sku in invs:
                        if inv in used and used[inv]!=sku: raise ValueError('Inventario vinculado a dos SKU físicos.')
                        used[inv]=sku; invs[sku].add(inv)
                for sku,inventory_ids in invs.items():
                    if not inventory_ids:
                        full[sku]=None
                        continue
                    count=0; details=[]
                    try:
                        for inv in sorted(inventory_ids):
                            stock=await stock_detail(client, inv)
                            details.append(stock)
                            qty=stock.get('available_quantity')
                            if stock.get('inventory_id')!=inv or type(qty) is not int or qty<0: raise ValueError('Stock inválido.')
                            count+=qty
                        full[sku]=count; full_details[sku]=details
                    except Exception: full[sku]=None; full_errors.append(sku)
            except Exception:
                full={s:None for s in bysku}; full_errors=list(bysku)
            # Warehouse is a dated ledger; Full always comes from ML, never public listing quantity.
            anchors={s:stamp(r.get('warehouse_as_of',data['as_of'])) for s,r in bysku.items()}
            for e in events:
                s=e['sku']
                if s not in bysku: continue
                if e['kind']=='count':
                    warehouse[s]=e['quantity']; anchors[s]=stamp(e['created'])
                    bysku[s]['estimated']=False
            shipping={}; warehouse_unknown=set(); deductions={}
            for o in orders:
                if o['status']!='paid': continue
                created=stamp(o['date_created']); date=created.astimezone(TZ).date().isoformat()
                for line in o['order_items']:
                    cs=components(line['item'])
                    if not cs: missing.add(line['item']['id']); continue
                    for part in cs:
                        s=part['sku']
                        if s not in bysku: missing.add(line['item']['id']); continue
                        n=line['quantity']*part['quantity']
                        if created.astimezone(TZ).date()<today:
                            daily[s][date]=daily[s].get(date,0)+n
                        if created>=anchors[s]:
                            sid=o.get('shipping',{}).get('id')
                            if sid not in shipping:
                                try:
                                    shipment=await client.get(f'/shipments/{sid}') if sid else {}
                                    if str(shipment.get('sender_id'))!=str(self.m.seller): raise ValueError('Envío no verificado.')
                                    shipping[sid]=shipment.get('logistic_type')
                                except Exception: shipping[sid]=None
                            kind=shipping[sid]
                            if kind is None: warehouse_unknown.add(s)
                            elif kind!='fulfillment':
                                key=(str(o['id']),s,created.isoformat()); deductions[key]=deductions.get(key,0)+n
            with self.m.db() as c:
                for (oid,s,created),n in deductions.items():
                    c.execute('INSERT INTO stock_sales VALUES(?,?,?,?) ON CONFLICT(order_id,sku) DO UPDATE SET quantity=MAX(quantity,excluded.quantity)',(oid,s,created,n))
                for s,created,n in c.execute('SELECT sku,created,quantity FROM stock_sales'):
                    if s in anchors and stamp(created)>=anchors[s]: warehouse[s]-=n
            pending=copy.deepcopy(data.get('purchase_orders',[]))
            received={}
            for e in events:
                s=e['sku']
                if s not in bysku: continue
                if stamp(e['created'])>anchors[s] and e['kind'] not in ('count','collection_dispatch'):
                    warehouse[s]+= e['quantity'] if e['kind']=='receive' else -e['quantity']
                if e['kind']=='receive' and e.get('purchase_id'):
                    key=(e['purchase_id'],s); received[key]=received.get(key,0)+e['quantity']
            for p in pending:
                for part in p['items']:
                    part['remaining']=max(0,part['quantity']-received.get((p['id'],part['sku']),0))
            # Only complete, observed full-day outages are excluded; today's observation is not backdated.
            with self.m.db() as c:
                observed=list(c.execute('SELECT day,sku,qty,first_at,last_at FROM stock_observations'))
                for s,q in full.items():
                    if q is not None:
                        c.execute('INSERT INTO stock_observations VALUES(?,?,?,?,?) ON CONFLICT(day,sku) DO UPDATE SET qty=MAX(qty,excluded.qty),last_at=excluded.last_at',(today.isoformat(),s,q,now.isoformat(),now.isoformat()))
            collections=copy.deepcopy(data.get('full_collections',[]))
            dispatched={}
            for e in events:
                if e['kind']=='collection_dispatch' and stamp(e['created'])>stamp(data['as_of']):
                    key=(e['collection_id'],e['sku'])
                    dispatched[key]=dispatched.get(key,0)+e['quantity']
            for c in collections:
                for part in c['items']:
                    part['dispatched']=dispatched.get((c['id'],part['sku']),0)
                    part['reserved_remaining']=max(0,part['quantity']-part['dispatched']) if c['status']=='reserved' else 0
            results=[]
            for s,r in bysku.items():
                excluded=r.get('stockout_days',[])+[d for d,k,q,a,b in observed if k==s and q==0 and d<today.isoformat() and stamp(a).hour==0 and stamp(b).hour==23]
                rate,factors,rates=forecast(daily[s],today,excluded)
                f=full.get(s); w=warehouse[s]
                reserved=sum(i['reserved_remaining'] for c in collections for i in c['items'] if i['sku']==s)
                physical=w+reserved if w>=0 else None
                stale=now-anchors[s]>timedelta(days=28)
                uncertain=r.get('estimated',False) or s in warehouse_unknown or w<0 or stale or bool(missing)
                total=w+reserved+f if f is not None and s not in warehouse_unknown and w>=0 and not stale else None
                fd=coverage(f,rate,factors,today); td=coverage(total,rate,factors,today)
                lead=r['lead_days']; safety=r['safety_days']; target=r['target_days']
                pending_s=sum(part['remaining'] for p in pending for part in p['items'] if part['sku']==s)
                # Avoid double buying: subtract all outstanding purchases, while separately warning about timing gaps.
                suggested=max(0,math.ceil(rate*(lead+safety+target)-(total or 0)-pending_s)) if total is not None and rate else None
                action='Sin demanda suficiente' if rate==0 else 'Revisar datos' if total is None else 'Stock suficiente'
                if rate and td is not None and td<lead+safety:
                    action='Comprar' if suggested else 'Verificar llegada del pedido'
                dated=[p['eta'] for p in pending if p.get('eta') and any(i['sku']==s and i['remaining'] for i in p['items'])]
                next_eta=min(dated) if dated else None
                if rate and total is not None and pending_s and next_eta and datetime.fromisoformat(next_eta).date()>today+timedelta(days=td or 0):
                    action='Verificar llegada del pedido' if not suggested else action
                full_action='Reponer Full' if fd is not None and fd<r.get('full_target_days',10) and w>0 else ''
                if uncertain and action=='Comprar': action='Revisar conteo antes de comprar'
                results.append(dict(sku=s,name=r.get('name',s),variant=r.get('variant',''),full=f,
                    full_snapshot=r['full_available'],full_snapshot_at=data.get('full_as_of',data['as_of']),
                    warehouse=w if w>=0 and not stale else None,
                    warehouse_physical=physical if not stale else None,warehouse_reserved=reserved,
                    available_now=w+f if total is not None else None,total=total,estimated=uncertain,
                    full_details=full_details.get(s,[]),
                    full_listings=[e for e in catalog if e.get('inventory_id') in invs.get(s,set())],
                    full_days=fd,total_days=td,rate=round(rate,2),averages=rates,
                    lead_days=lead,safety_days=safety,full_target_days=r.get('full_target_days',10),
                    pending=pending_s,next_eta=next_eta,action=action,full_action=full_action,suggested=suggested,
                    count_at=anchors[s].isoformat(),excluded_stockout_days=len(excluded)))
            # Read completed Full receptions separately; never infer a scheduled collection
            # or alter warehouse balances merely because a Full quantity increased.
            reception_key='stock_full_receptions_v1'
            receptions=self.m.auto.get(reception_key,{})
            if force or time.time()-receptions.get('time',0)>300:
                try:
                    ids=set(used)
                    value=await operations_page(client,self.m.seller,ids,
                        (today-timedelta(days=7)).isoformat(),(today+timedelta(days=1)).isoformat(),
                        kind='INBOUND_RECEPTION') if ids else {'complete':False,'results':[],'error':'Sin inventarios Full verificados.'}
                except Exception:
                    value={'complete':False,'results':[],'error':'Movimientos Full no disponibles; no interpretar como ausencia de ingresos.'}
                receptions={'time':time.time(),'data':value};self.m.auto.put(reception_key,receptions)
            result={'rows':results,'pending':pending,'revision':revision,'as_of':now.isoformat(),
                'email_ready':self.mail_ready(),'email_state':self.m.auto.get('stock_mail_status','Sin envíos'),
                'missing_listings':sorted(missing),'full_errors':full_errors,
                'history':events[-30:][::-1],
                'full_collections':collections,
                'full_receptions':receptions.get('data'), 'full_access_limitations':LIMITATION,
                'method':'Últimos 7/14/28 días completos, peso 60/30/10 y ajuste moderado por día de semana. Se excluyen días sin stock observados. Etapa del mes: pendiente de historial comparable suficiente. Cambios de precio/promoción se reflejan en el ritmo reciente; no se supone crecimiento adicional.',
                'assumptions':data.get('assumptions',[])}
            self.cache={'time':time.time(),'snapshot_revision':snapshot.get('revision'),'data':copy.deepcopy(result)}
            return result

    async def move(self, payload, actor):
        if not isinstance(payload,dict): raise ValueError('Movimiento inválido.')
        kind=payload.get('kind'); qty=payload.get('quantity'); ident=payload.get('id','')
        if kind not in ('receive','sale','transfer','count','collection_dispatch') or type(qty) is not int or qty<0 or qty>100000 or (kind!='count' and qty==0):
            raise ValueError('Tipo o cantidad inválida.')
        if not re.fullmatch(r'[A-Za-z0-9-]{12,80}',ident): raise ValueError('Identificador inválido.')
        note=payload.get('note','').strip()
        if not 3<=len(note)<=300: raise ValueError('Ingresá un motivo de 3 a 300 caracteres.')
        async with self.lock:
            snapshot=self.initial(); events=self.events()
            if any(e['id']==ident for e in events): return {'saved':True,'duplicate':True}
            if payload.get('revision')!=self.revision(snapshot,events): raise ValueError('El stock cambió. Actualizá antes de guardar.')
            rows=snapshot.get('data',{}).get('stock',[])
            if payload.get('sku') not in {r['sku'] for r in rows}: raise ValueError('SKU desconocido.')
            p_id=payload.get('purchase_id') or None
            if p_id:
                expected=sum(i['quantity'] for p in snapshot['data'].get('purchase_orders',[]) if p['id']==p_id for i in p['items'] if i['sku']==payload['sku'])
                already=sum(e['quantity'] for e in events if e['kind']=='receive' and e.get('purchase_id')==p_id and e['sku']==payload['sku'])
                if kind!='receive' or expected-already<qty: raise ValueError('La recepción excede el saldo del pedido.')
            collection_id=payload.get('collection_id')
            if kind=='collection_dispatch':
                collection=next((c for c in snapshot['data'].get('full_collections',[]) if c['id']==collection_id and c['status']=='reserved'),None)
                expected=sum(i['quantity'] for i in (collection or {}).get('items',[]) if i['sku']==payload['sku'])
                sent=sum(e['quantity'] for e in events if e['kind']=='collection_dispatch' and e.get('collection_id')==collection_id and e['sku']==payload['sku'] and stamp(e['created'])>stamp(snapshot['data']['as_of']))
                if expected-sent<qty: raise ValueError('No hay esa cantidad reservada en la colecta.')
            if kind in ('sale','transfer'):
                cached=next((r for r in (self.cache or {}).get('data',{}).get('rows',[]) if r['sku']==payload['sku']),{})
                if cached.get('warehouse') is None or cached['warehouse']<qty: raise ValueError('Saldo insuficiente o desconocido. Recontá el depósito.')
            body={k:payload.get(k) for k in ('sku','kind','quantity')}; body.update(note=note,purchase_id=p_id)
            if kind=='collection_dispatch': body['collection_id']=collection_id
            with self.m.db() as c:
                c.execute('INSERT INTO stock_events VALUES(?,?,?,?)',(ident,datetime.now(TZ).isoformat(),actor,json.dumps(body)))
            self.cache=None
            return {'saved':True}

    def send_mail(self, rows, now):
        message=EmailMessage(); message['From']=self.env['NF_SMTP_FROM']; message['To']=self.env['NF_ALERT_EMAIL_TO']
        message['Subject']='NorthFitness · reposición y compras · '+now.date().isoformat()
        lines=[f"{r['name']} {r['variant']}: {r['action']}. Full {r['full_days']} días; total {r['total_days']} días. Pedido sugerido: {r['suggested']} pares/unidades. Pendiente: {r['pending']}. {r['full_action']}" for r in rows]
        message.set_content('\n'.join(lines))
        message.add_alternative('<h1>NorthFitness · Stock</h1><p>Revisar compras y reposición. Las cantidades estimadas requieren validar el conteo.</p><ul>'+''.join('<li>'+html.escape(line)+'</li>' for line in lines)+'</ul>',subtype='html')
        with smtplib.SMTP_SSL(self.env['NF_SMTP_HOST'],int(self.env.get('NF_SMTP_PORT','465')),timeout=15,context=ssl.create_default_context()) as smtp:
            smtp.login(self.env['NF_SMTP_USER'],self.env['NF_SMTP_PASSWORD']); smtp.send_message(message)

    async def alerts(self, result):
        now=datetime.now(TZ)
        if not self.mail_ready() or now.hour<9: return
        rows=[r for r in result['rows'] if r['action'] in ('Comprar','Verificar llegada del pedido','Revisar conteo antes de comprar') or r['full_action']]
        if not rows: return
        # At most one digest/day. Reserve before SMTP; an uncertain delivery is not blindly resent.
        with self.m.db() as c:
            inserted=c.execute('INSERT OR IGNORE INTO stock_mail VALUES(?,?,?)',(now.date().isoformat(),'sending',time.time())).rowcount
        if not inserted: return
        try:
            await asyncio.to_thread(self.send_mail,rows,now); status='Enviado '+now.isoformat()
        except Exception: status='Entrega incierta o fallida; revisar configuración SMTP antes de reintentar'
        self.m.auto.put('stock_mail_status',status)
        with self.m.db() as c: c.execute('UPDATE stock_mail SET state=? WHERE day=?',(status,now.date().isoformat()))

    async def run(self):
        while True:
            try:
                result=await self.read(); await self.alerts(result)
            except Exception: pass
            await asyncio.sleep(300)


def register(mcp, monitor, env, headers):
    from starlette.responses import JSONResponse
    stock=StockMonitor(monitor,env); monitor.stock=stock
    @mcp.custom_route('/monitor/stock',methods=['GET'])
    async def stock_data(request):
        if not monitor.authorized(request): return JSONResponse({'error':'Ingresá al monitor.'},status_code=401,headers=headers)
        try: return JSONResponse(await stock.read(request.query_params.get('refresh')=='1'),headers=headers)
        except Exception: return JSONResponse({'error':'No se pudo actualizar el stock. Reintentá; los datos anteriores no son actuales.'},status_code=503,headers=headers)
    @mcp.custom_route('/monitor/stock/movement',methods=['POST'])
    async def stock_movement(request):
        actor=monitor.auth.user(request)
        if not actor: return JSONResponse({'error':'Ingresá al monitor.'},status_code=401,headers=headers)
        if request.headers.get('origin')!=monitor.base_url: return JSONResponse({'error':'Origen no autorizado.'},status_code=403,headers=headers)
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>4096: return JSONResponse({'error':'Solicitud demasiado grande.'},status_code=413,headers=headers)
        try: return JSONResponse(await stock.move(json.loads(raw),actor),headers=headers)
        except (ValueError,KeyError,TypeError): return JSONResponse({'error':'No se guardó. Revisá cantidad, motivo y saldo, y actualizá el stock antes de reintentar.'},status_code=409,headers=headers)
    return stock
