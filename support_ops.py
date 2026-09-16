"""Recovery, internal alerts and daily reports. Never retries uncertain writes."""
import asyncio
import json
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/Argentina/Buenos_Aires')

async def all_orders(client, seller, start, end):
    rows, total = [], None
    for offset in range(0, 10000, 50):
        page = await client.get('/orders/search', {'seller': seller,
            'order.date_created.from': start, 'order.date_created.to': end,
            'sort': 'date_asc', 'offset': offset, 'limit': 50})
        data, count = page.get('results'), page.get('paging', {}).get('total')
        if not isinstance(data, list) or type(count) is not int or count < 0:
            raise ValueError('orders_contract')
        if total is not None and total != count:
            raise ValueError('orders_changed')
        total = count
        if any(str(r.get('seller', {}).get('id')) != seller for r in data):
            raise ValueError('wrong_seller')
        rows.extend(data)
        if len(rows) >= total:
            break
        if len(data) != 50:
            raise ValueError('orders_incomplete')
    ids = [str(r.get('id')) for r in rows]
    if len(rows) != total or len(ids) != len(set(ids)) or any(not x.isdigit() for x in ids):
        raise ValueError('orders_incomplete')
    return rows

class Operations:
    def __init__(self, worker):
        self.w = worker
        with worker.db() as c:
            if 'finished' not in [r[1] for r in c.execute('PRAGMA table_info(jobs)')]:
                c.execute('ALTER TABLE jobs ADD COLUMN finished REAL')
            c.executescript('''CREATE TABLE IF NOT EXISTS alerts(
                key TEXT PRIMARY KEY, body TEXT, created REAL, acknowledged INTEGER DEFAULT 0,
                mailed INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS daily_reports(day TEXT PRIMARY KEY, body TEXT);''')

    def alert(self, key, body):
        with self.w.db() as c:
            c.execute('INSERT OR IGNORE INTO alerts(key,body,created) VALUES (?,?,?)',
                      (key, json.dumps(body, ensure_ascii=False), time.time()))

    def alerts(self, offset=0):
        if type(offset) is not int or offset < 0:
            raise ValueError('offset')
        with self.w.db() as c:
            rows = c.execute('SELECT key,body,created,acknowledged,mailed FROM alerts ORDER BY created DESC LIMIT 50 OFFSET ?', (offset,)).fetchall()
        return {'alerts': [dict(key=r[0], detail=json.loads(r[1]), created=r[2], acknowledged=bool(r[3]), emailed=bool(r[4])) for r in rows],
                'next_offset': offset+50 if len(rows)==50 else None,
                'email_configured': self.mail_ready(), 'email_error': self.w.get('ops_mail_error')}

    def mail_ready(self):
        e = self.w.env
        return all(e.get(k) for k in ('NF_ALERT_EMAIL_TO','NF_SMTP_HOST','NF_SMTP_USER','NF_SMTP_PASSWORD','NF_SMTP_FROM'))

    def send_mail(self, rows):
        e = self.w.env
        message = EmailMessage()
        message['From'], message['To'] = e['NF_SMTP_FROM'], e['NF_ALERT_EMAIL_TO']
        message['Subject'] = 'NorthFitness: alertas y resumen de atención'
        message.set_content('\n\n'.join(r[1] for r in rows))
        with smtplib.SMTP_SSL(e['NF_SMTP_HOST'], int(e.get('NF_SMTP_PORT','465')), timeout=15, context=ssl.create_default_context()) as smtp:
            smtp.login(e['NF_SMTP_USER'], e['NF_SMTP_PASSWORD'])
            smtp.send_message(message)

    def report(self, day):
        date = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=TZ)
        start, end = date.timestamp(), (date+timedelta(days=1)).timestamp()
        with self.w.db() as c:
            counts = c.execute('SELECT topic,state,count(*) FROM jobs WHERE finished>=? AND finished<? GROUP BY topic,state',(start,end)).fetchall()
            money = c.execute('SELECT action,state,amount FROM claim_writes WHERE created>=? AND created<?',(start,end)).fetchall()
        from decimal import Decimal
        refunds = sum((Decimal(r[2]) for r in money if r[0]=='refund' and r[1]=='verified'), Decimal(0))
        return {'day': day, 'timezone': str(TZ), 'processed': [dict(topic=r[0],state=r[1],count=r[2]) for r in counts],
                'verified_refunds_ars': str(refunds), 'claim_operations': [dict(action=r[0],state=r[1],reserved_amount=r[2]) for r in money],
                'note': 'Actividad del sistema, no balance. Jobs anteriores a v0.8 no tienen fecha de finalización. Dinero agrupado por fecha de inicio de operación; importe reservado, no liquidación bancaria.'}

    def enqueue(self, topic, ids):
        with self.w.db() as c:
            for id in ids:
                pattern = r'[0-9]+' if topic=='questions' else r'[A-Za-z0-9_-]+'
                if not re.fullmatch(pattern, str(id)):
                    raise ValueError('invalid_resource')
                resource = '/' + topic + '/' + str(id)
                c.execute("INSERT OR IGNORE INTO jobs(topic,resource,state,reason,created,event_key) VALUES (?,?,'pending','',?,?)",(topic,resource,time.time(),resource))

    async def questions(self, client):
        rows, total = [], None
        for offset in range(0,10000,50):
            page = await client.get('/questions/search', {'seller_id':self.w.seller,'status':'UNANSWERED','api_version':4,'limit':50,'offset':offset})
            data,count = page.get('questions'),page.get('total')
            if not isinstance(data,list) or type(count) is not int or count<0 or (total is not None and count!=total):
                raise ValueError('questions_contract_or_changed')
            total=count
            rows.extend(data)
            if len(rows)>=total: break
            if len(data)!=50: raise ValueError('questions_incomplete')
        ids=[str(r.get('id')) for r in rows]
        if len(rows)!=total or len(set(ids))!=len(ids) or any(str(r.get('seller_id'))!=self.w.seller for r in rows):
            raise ValueError('questions_incomplete_or_owner')
        from support_auto import stamp
        self.enqueue('questions',[r['id'] for r in rows])

    async def messages(self, client):
        # Rotate through every order in a fully paginated 30-day snapshot.
        w = self.w
        ids=w.get('ops_order_ids',[])
        cursor=w.get('ops_order_cursor',0)
        if cursor>=len(ids):
            now=datetime.now(timezone.utc)
            orders=await all_orders(client,w.seller,(now-timedelta(days=30)).isoformat(),now.isoformat())
            ids=[str(r['id']) for r in orders]
            cursor=0
            w.put('ops_order_ids',ids)
        from support_auto import stamp
        for oid in ids[cursor:cursor+20]:
            try:
                conv=await w.conversation_snapshot(client,w.seller,oid)
                msgs=conv['messages']
                if not msgs: continue
                latest=max(msgs,key=lambda x:stamp(x['message_date']['created']))
                if str(latest.get('from',{}).get('user_id'))!=w.seller and stamp(latest['message_date']['created'])>=w.get('cutover',time.time()):
                    self.enqueue('messages',[latest['id']])
            except Exception:
                self.alert('conversation:'+oid,{'order_id':oid,'reason':'No se pudo recuperar la conversación; revisar permisos y contrato.'})
        w.put('ops_order_cursor',min(cursor+20,len(ids)))

    async def tick(self):
        w=self.w
        if time.time()-w.get('ops_attempt',0)<300: return
        w.put('ops_attempt',time.time())
        with w.db() as c:
            reviews=c.execute("SELECT id,topic,resource,reason FROM jobs WHERE state IN ('review','error')").fetchall()
        for r in reviews:
            self.alert('job:'+str(r[0]),dict(job=r[0],topic=r[1],resource=r[2],reason=r[3]))
        for key in ('claims_sweep_error',):
            if w.get(key): self.alert(key+':'+datetime.now(TZ).date().isoformat(),{'error':w.get(key)})
        if w.enabled():
            for name,fn in [('questions',self.questions),('messages',self.messages)]:
                try:
                    await fn(await w.client())
                    w.put('ops_'+name+'_error',None)
                    w.put('ops_'+name+'_success',time.time())
                except Exception:
                    w.put('ops_'+name+'_error','recovery_failed')
                    self.alert(name+':'+datetime.now(TZ).date().isoformat(),{'error':'Falló recuperación de '+name})
        elif not w.get('paused',True):
            self.alert('disabled:'+datetime.now(TZ).date().isoformat(),{'error':'Atención autorizada pero no disponible; revisar configuración y autorización.'})
        day=(datetime.now(TZ)-timedelta(days=1)).date().isoformat()
        report=self.report(day)
        with w.db() as c:
            c.execute('INSERT OR REPLACE INTO daily_reports VALUES (?,?)',(day,json.dumps(report)))
        self.alert('report:'+day,report)
        if self.mail_ready():
            with w.db() as c:
                rows=c.execute('SELECT key,body FROM alerts WHERE mailed=0 ORDER BY created LIMIT 50').fetchall()
            if rows:
                try:
                    await asyncio.to_thread(self.send_mail,rows)
                    with w.db() as c:
                        c.executemany('UPDATE alerts SET mailed=1 WHERE key=?',[(r[0],) for r in rows])
                    w.put('ops_mail_error',None)
                except Exception:
                    w.put('ops_mail_error','smtp_failed_delivery_may_be_uncertain')
