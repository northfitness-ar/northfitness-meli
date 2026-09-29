"""Private read-only dashboard on the existing server. No credentials in URLs."""
import asyncio
import base64
import contextlib
import copy
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from monitor_auth import MonitorAuth, AuthError, COOKIE
from sales_data import all_orders
from sales_data import instant
from profitability import summarize, summarize_period, validate_policy, amount

TZ = ZoneInfo('America/Argentina/Buenos_Aires')
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'X-Robots-Tag': 'noindex, nofollow',
           'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"}


class Monitor:
    def __init__(self, data, auto, seller, base_url, permanent_secret=None):
        self.path = str(data / 'monitor.sqlite3')
        self.auto, self.seller, self.base_url = auto, seller, base_url.rstrip('/')
        self.lock = asyncio.Lock()
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, revision INTEGER, body TEXT);
            CREATE TABLE IF NOT EXISTS policy_history (revision INTEGER PRIMARY KEY, body TEXT, saved_at REAL);
            CREATE TABLE IF NOT EXISTS snapshots (day TEXT PRIMARY KEY, body TEXT, updated_at REAL);
            CREATE TABLE IF NOT EXISTS shipping_costs (shipment TEXT PRIMARY KEY, body TEXT, updated_at REAL);
            CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, kind TEXT, expires REAL);
            ''')
        self.auth = MonitorAuth(self.db, self.base_url)

    @contextlib.contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def config(self):
        with self.db() as c:
            row = c.execute('SELECT revision,body FROM config WHERE id=1').fetchone()
        return {'revision': row[0], 'policy': json.loads(row[1])} if row else {'revision': 0, 'policy': {'currency': 'ARS'}}

    def configure(self, policy, revision):
        validate_policy(policy)
        body = json.dumps(policy, allow_nan=False)
        if len(body) > 2_000_000:
            raise ValueError('Configuración demasiado grande.')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT revision FROM config WHERE id=1').fetchone()
            current = row[0] if row else 0
            if type(revision) is not int or revision != current:
                raise ValueError('Configuración cambió. Releer antes de guardar.')
            c.execute('INSERT OR REPLACE INTO config VALUES(1,?,?)', (current + 1, body))
            c.execute('INSERT INTO policy_history VALUES(?,?,?)', (current + 1, body, time.time()))
        return {'revision': current + 1, 'saved': True}

    def authorized(self, request):
        return self.auth.user(request) is not None

    async def ads(self, client, day):
        data = await client.get('/advertising/advertisers', {'product_id': 'PADS'}, headers={'api-version': '1'})
        advertisers = data.get('advertisers')
        if not isinstance(advertisers, list):
            raise ValueError('Anunciantes no disponibles.')
        ids = {str(a['advertiser_id']) for a in advertisers if a.get('site_id') == 'MLA'}
        if not ids:
            raise ValueError('No hay anunciante verificable.')
        total_cost = amount('0')
        for aid in ids:
            offset, expected, seen = 0, None, set()
            while True:
                data = await client.get(f'/advertising/MLA/advertisers/{aid}/product_ads/campaigns/search',
                    {'limit': 50, 'offset': offset, 'date_from': day, 'date_to': day, 'metrics': 'cost'},
                    headers={'api-version': '2'})
                rows, total = data.get('results'), data.get('paging', {}).get('total')
                if not isinstance(rows, list) or type(total) is not int or total < 0 or (not rows and offset < total):
                    raise ValueError('Ads incompleto.')
                if expected is not None and expected != total:
                    raise ValueError('Paginación Ads cambió.')
                expected = total
                for row in rows:
                    key = str(row['id'])
                    if str(row.get('advertiser_id')) != aid or key in seen:
                        raise ValueError('Ads duplicado o de otro anunciante.')
                    seen.add(key)
                    cost = amount(row.get('metrics', {}).get('cost'))
                    if cost < 0:
                        raise ValueError('Gasto Ads inválido.')
                    total_cost += cost
                offset += len(rows)
                if offset >= total:
                    break
                if offset > 10000:
                    raise ValueError('Ads excede límite de consulta.')
        return total_cost

    async def snapshot(self, day, force=False):
        from datetime import date
        target = date.fromisoformat(day)
        today = datetime.now(TZ).date()
        if not today - timedelta(days=366) <= target <= today:
            raise ValueError('Elegí una fecha entre hoy y los últimos 366 días.')
        async with self.lock:
            with self.db() as c:
                cached = c.execute('SELECT body,updated_at FROM snapshots WHERE day=?', (day,)).fetchone()
            cfg = self.config()
            if not force and cached and time.time() - cached[1] < 300:
                body = json.loads(cached[0])
                if body['policy_revision'] == cfg['revision'] and body.get('calculation_version') == 4:
                    return body
            try:
                client = await self.auto.client()
                start = datetime.combine(target, datetime.min.time(), TZ)
                end = min(start + timedelta(days=1), datetime.now(TZ))
                rows, excluded = await all_orders(client, self.seller, start.isoformat(), end.isoformat())
                ads, ads_error = None, None
                try:
                    if cfg['policy'].get('management_estimate', {}).get('ads') == 'include':
                        ads = await self.ads(client, day)
                except Exception:
                    ads_error = 'Publicidad no disponible; no se reemplazó por cero.'
                effective_policy = await self.shipping_policy(client, rows, cfg['policy'])
                result = summarize(rows, effective_policy, day, ads)
                result['traffic'] = await self.traffic(client, rows, start, end, live=target == today)
                result.update(fetched_at=datetime.now(TZ).isoformat(), policy_revision=cfg['revision'],
                              calculation_version=4,
                              excluded_out_of_range=excluded, ads_error=ads_error, stale=False,
                              refresh_seconds=300, period_end=end.isoformat())
                with self.db() as c:
                    c.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)', (day, json.dumps(result), time.time()))
                return result
            except Exception:
                if cached:
                    old = json.loads(cached[0])
                    old.update(stale=True, error='Falló la actualización; se conserva la última lectura con su fecha.')
                    return old
                raise ValueError('No se pudo obtener una lectura completa. Revisar autorización de lectura y datos del monitor.') from None

    async def shipping_policy(self, client, rows, policy):
        """Bounded reads. Never duplicate pack freight or infer it from free_shipping."""
        enriched = copy.deepcopy(policy)
        facts = enriched.setdefault('orders', {})
        shipments = {}
        for row in rows:
            sid = (row.get('shipping') or {}).get('id')
            if row.get('status') == 'paid' and sid and 'logistics' not in facts.get(str(row['id']), {}):
                shipments.setdefault(str(sid), []).append(row)
        budget = 8
        deadline = time.monotonic() + 8
        for sid, shipment_orders in shipments.items():
            with self.db() as c:
                cached = c.execute('SELECT body,updated_at FROM shipping_costs WHERE shipment=?', (sid,)).fetchone()
            allocation = None
            if cached and time.time() - cached[1] < 3600:
                allocation = json.loads(cached[0])
            elif budget and time.monotonic() < deadline:
                budget -= 1
                try:
                    costs = await asyncio.wait_for(client.get('/shipments/' + sid + '/costs'), timeout=2)
                    senders = [x for x in costs.get('senders', []) if str(x.get('user_id')) == str(self.seller)]
                    if len(senders) != 1:
                        continue
                    cost = amount(senders[0]['cost'])
                    if cost < 0:
                        continue
                    items = await asyncio.wait_for(client.get('/shipments/' + sid + '/items'), timeout=2)
                    if not isinstance(items, list) or not items:
                        continue
                    ids = {str(x['order_id']) for x in items}
                    available = {str(x['id']): x for x in shipment_orders}
                    # Mixed/out-of-period or cancelled packs require reconciliation.
                    if ids != set(available):
                        continue
                    weights = {oid: sum((amount(l['unit_price']) * l['quantity'] for l in o['order_items']), amount(0)) for oid, o in available.items()}
                    total_weight = sum(weights.values(), amount(0))
                    if total_weight <= 0:
                        continue
                    from profitability import money
                    remaining, allocation = amount(money(cost)), {}
                    ordered = sorted(weights)
                    for oid in ordered[:-1]:
                        value = amount(money(cost * weights[oid] / total_weight))
                        allocation[oid] = str(value)
                        remaining -= value
                    allocation[ordered[-1]] = str(remaining)
                    with self.db() as c:
                        c.execute('INSERT OR REPLACE INTO shipping_costs VALUES(?,?,?)', (sid, json.dumps(allocation), time.time()))
                except Exception:
                    continue
            if allocation:
                for row in shipment_orders:
                    oid = str(row['id'])
                    if oid in allocation:
                        entry = facts.setdefault(oid, {'source': 'Mercado Libre: costo del remitente por envío'})
                        entry['logistics'] = allocation[oid]
        with self.db() as c:
            c.execute('DELETE FROM shipping_costs WHERE updated_at < ?', (time.time()-86400*370,))
        return enriched

    async def cached_orders(self, client, start, end, force=False):
        """Reuse closed history; only poll the open-day tail every 30 seconds.

        Closed history expires after five minutes so later cancellations are read.
        Errors propagate; the caller may show a dated, explicitly stale summary.
        """
        midnight = datetime.combine(datetime.now(TZ).date(), datetime.min.time(), TZ)
        ranges = [(start, end)] if not start < midnight < end else [(start, midnight), (midnight, end)]
        rows, excluded = [], 0
        for a, b in ranges:
            query_end = datetime.combine(b.date()+timedelta(days=1),datetime.min.time(),TZ) if b <= midnight and b.time() != datetime.min.time() else b
            key = 'orders:v1:' + a.isoformat() + ':' + (query_end.isoformat() if b <= midnight else 'live')
            ttl = 300 if b <= midnight else 30
            with self.db() as c:
                cached = c.execute('SELECT body,updated_at FROM snapshots WHERE day=?', (key,)).fetchone()
            payload = json.loads(cached[0]) if cached and not force and time.time()-cached[1] < ttl else None
            if payload is None:
                items, skipped = await all_orders(client, self.seller, a.isoformat(), query_end.isoformat())
                payload = {'rows': items, 'excluded': skipped}
                with self.db() as c:
                    c.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)', (key,json.dumps(payload),time.time()))
                    c.execute("DELETE FROM snapshots WHERE day LIKE 'orders:v1:%' AND updated_at < ?", (time.time()-86400,))
            rows.extend(o for o in payload['rows'] if a <= instant(o['date_created']) < b)
            excluded += payload['excluded']
        return rows, excluded

    async def period(self, day, mode='day', force=False):
        from datetime import date
        target, now = date.fromisoformat(day), datetime.now(TZ)
        if mode not in ('day', 'week', 'month'):
            raise ValueError('Período inválido.')
        if not now.date() - timedelta(days=366) <= target <= now.date():
            raise ValueError('Elegí una fecha de los últimos 366 días.')
        first = target if mode == 'day' else target - timedelta(days=target.weekday()) if mode == 'week' else target.replace(day=1)
        start = datetime.combine(first, datetime.min.time(), TZ)
        if mode == 'month':
            stop = datetime.combine(target + timedelta(days=1), datetime.min.time(), TZ)
        else:
            stop = start + timedelta(days=7 if mode == 'week' else 1)
        end = min(stop, now)
        refresh = 30 if end == now else 300
        key = 'period:v5:' + mode + ':' + day
        async with self.lock:
            cfg = self.config()
            with self.db() as c:
                cached = c.execute('SELECT body,updated_at FROM snapshots WHERE day=?', (key,)).fetchone()
            if cached and not force and time.time()-cached[1] < refresh:
                shared = json.loads(cached[0])
                if shared.get('policy_revision') == cfg['revision'] and not shared.get('stale'):
                    return shared
            try:
                client = await self.auto.client()
                rows, excluded = await self.cached_orders(client, start, end, force)
                effective_policy = await self.shipping_policy(client, rows, cfg['policy'])
                result = summarize_period(rows, effective_policy, start, end)
                result['traffic'] = await self.traffic(client, rows, start, end, live=end == now)
                result.update(fetched_at=datetime.now(TZ).isoformat(), policy_revision=cfg['revision'],
                              stale=False, refresh_seconds=refresh, live=end == now, mode=mode, excluded_out_of_range=excluded)
                try:
                    shift = timedelta(days=7)
                    comp_start, comp_end = start, end
                    if mode == 'month':
                        from calendar import monthrange
                        previous_start = (start-timedelta(days=1)).replace(day=1)
                        common_day = min(target.day, monthrange(previous_start.year, previous_start.month)[1])
                        comp_end = min(end, start+timedelta(days=common_day))
                        previous_end = previous_start+(comp_end-start)
                    else:
                        previous_start, previous_end = start-shift, end-shift
                    previous, _ = await self.cached_orders(client, previous_start, previous_end, force)
                    comparison = summarize_period(previous, cfg['policy'], previous_start, previous_end)
                    matched = result if comp_end == end else summarize_period(rows, effective_policy, comp_start, comp_end)
                    current_sales = amount(matched['gross']) - amount(matched['cancelled'])
                    previous_sales = amount(comparison['gross']) - amount(comparison['cancelled'])
                    result['comparison'] = {'start': previous_start.isoformat(), 'end': previous_end.isoformat(),
                        'current_start': comp_start.isoformat(), 'current_end': comp_end.isoformat(),
                        'current_sales': str(current_sales), 'sales': str(previous_sales),
                        'current_paid_sales': matched['paid_sales'], 'paid_sales': comparison['paid_sales'],
                        'current_units': matched['sold_units'], 'units': comparison['sold_units'],
                        'percent': str((current_sales-previous_sales)*100/previous_sales) if previous_sales else None}
                except Exception:
                    result['comparison'] = None
                with self.db() as c:
                    c.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)', (key, json.dumps(result), time.time()))
                    c.execute("DELETE FROM snapshots WHERE day LIKE 'period:%' AND updated_at < ?", (time.time()-86400*2,))
                return result
            except Exception:
                if cached:
                    old = json.loads(cached[0])
                    old.update(stale=True, error='No se pudo actualizar.')
                    return old
                raise ValueError('No se pudo obtener una lectura completa.') from None

    async def compare(self, day, mode, reference):
        from datetime import date
        target, base = date.fromisoformat(day), date.fromisoformat(reference)
        now = datetime.now(TZ)
        if mode not in ('day', 'week', 'month'):
            raise ValueError('Elegí vista diaria, semanal o mensual.')
        if mode != 'month' and ((target.year, target.month) != (base.year, base.month) or target.weekday() != base.weekday() or target == base):
            raise ValueError('Elegí otro día de la misma semana y del mismo mes.')
        if not now.date() - timedelta(days=366) <= min(target, base) <= max(target, base) <= now.date():
            raise ValueError('Fecha fuera del período disponible.')
        if mode == 'month':
            from calendar import monthrange
            if base.year != target.year or base.month >= target.month:
                raise ValueError('Elegí un mes anterior del mismo año, desde enero.')
            cutoff_day = min(target.day, monthrange(base.year, base.month)[1])
            begin = datetime(target.year, target.month, 1, tzinfo=TZ)
            other = datetime(base.year, base.month, 1, tzinfo=TZ)
            elapsed = timedelta(days=cutoff_day)
            if target == now.date() and cutoff_day == target.day:
                elapsed = now - begin
            end, other_end = begin + elapsed, other + elapsed
            ranges = ((begin, end), (other, other_end))
        else:
            start = datetime.combine(target, datetime.min.time(), TZ)
            other = datetime.combine(base, datetime.min.time(), TZ)
            if mode == 'week':
                start -= timedelta(days=target.weekday())
                other -= timedelta(days=base.weekday())
            shift = start - other
            month_start = datetime(target.year, target.month, 1, tzinfo=TZ)
            month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            # Intersection retains only matched weekdays inside the selected month.
            begin = max(start, month_start, month_start + shift)
            end = min(start + timedelta(days=7 if mode == 'week' else 1),
                      month_end, month_end + shift, now, now + shift)
            if begin >= end:
                raise ValueError('No hay días equivalentes disponibles dentro del mes.')
            ranges = ((begin, end), (begin-shift, end-shift))
        async with self.lock:
            cfg = self.config()
            policy = cfg['policy']
            cache_key = 'compare:v1:' + ':'.join((day, mode, reference))
            ttl = 60 if any(b == now for a,b in ranges) else 300
            with self.db() as c:
                cached = c.execute('SELECT body,updated_at FROM snapshots WHERE day=?',(cache_key,)).fetchone()
            if cached and time.time()-cached[1] < ttl:
                shared = json.loads(cached[0])
                if shared.get('policy_revision') == cfg['revision']:
                    return shared
            client = await self.auto.client()
            summaries = []
            for a, b in ranges:
                rows, _ = await self.cached_orders(client, a, b)
                effective = await self.shipping_policy(client, rows, policy)
                summary = summarize_period(rows, effective, a, b)
                summary['traffic'] = await self.traffic(client, rows, a, b, live=b == now)
                summary.pop('orders', None)
                summaries.append(summary)
            result = {'current': summaries[0], 'reference': summaries[1],
                      'policy_revision': cfg['revision'], 'fetched_at': datetime.now(TZ).isoformat()}
            with self.db() as c:
                c.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)',(cache_key,json.dumps(result),time.time()))
                c.execute("DELETE FROM snapshots WHERE day LIKE 'compare:v1:%' AND updated_at < ?",(time.time()-86400,))
            return result

    async def traffic(self, client, rows, start, end, *, live=False):
        """Seller listing visits, isolated from financial calculations and caches.

        Until reconciliation with the seller UI, conversion is explicitly an
        operational estimate: distinct paid orders / listing visits (not units).
        Never silently claim that this reproduces ML's private dashboard metric.
        """
        sales = len({str(o['id']) for o in rows if o.get('status') == 'paid'
                     and start <= instant(o['date_created']) < end})
        result = {'visits': None, 'paid_orders': sales, 'conversion_percent': None,
                  'status': 'unavailable', 'formula': 'Órdenes pagadas / visitas × 100',
                  'scope': 'Visitas a publicaciones de la cuenta; equivalencia con visitas únicas del panel ML no verificada',
                  'official_equivalence_verified': False, 'fetched_at': None,
                  'refresh_seconds': 30 if live else 900, 'provisional': live, 'stale': False}
        # A historical partial day cannot be reconstructed from daily totals.
        # For the live period query today's entire provider bucket, then cap paid
        # orders at the read cutoff. ML's reporting latency remains unknown.
        if start.time() != datetime.min.time() or (end.time() != datetime.min.time() and not live):
            result['reason'] = 'Mercado Libre no ofrece el corte intradiario histórico; seleccioná días completos.'
            return result
        query_end = datetime.combine(end.date() + timedelta(days=1), datetime.min.time(), TZ) if live else end
        key = 'traffic:v4:' + ('live:' if live else 'closed:') + start.isoformat() + ':' + query_end.isoformat()
        with self.db() as c:
            cached = c.execute('SELECT body,updated_at FROM snapshots WHERE day=?', (key,)).fetchone()
        if cached and cached[1] > time.time() - result['refresh_seconds']:
            result.update(json.loads(cached[0]))
        else:
            payload = {'visits': None, 'paid_orders': None, 'status': 'unavailable',
                       'fetched_at': datetime.now(TZ).isoformat()}
            try:
                data = await asyncio.wait_for(client.get(f'/users/{self.seller}/items_visits', {
                    'date_from': start.date().isoformat(),
                    'date_to': query_end.date().isoformat()}), 12)
                payload['provider_shape'] = {'type': type(data).__name__,
                    'keys': sorted(str(k) for k in data)[:20] if isinstance(data, dict) else []}
                if isinstance(data, dict):
                    payload['provider_range'] = {k: data.get(k) for k in ('date_from', 'date_to')}
                if (str(data.get('user_id')) != str(self.seller)
                        or type(data.get('total_visits')) is not int or data['total_visits'] < 0):
                    raise ValueError('Respuesta de visitas incompleta o vendedor diferente.')
                provider_start = datetime.fromisoformat(data['date_from'].replace('Z', '+00:00'))
                provider_end = datetime.fromisoformat(data['date_to'].replace('Z', '+00:00'))
                if (provider_start.tzinfo is None or provider_end.tzinfo is None
                        or provider_start.date() != start.date() or provider_end.date() != query_end.date()
                        or provider_start.time() != datetime.min.time()
                        or provider_end.time() != datetime.min.time()
                        or provider_start.utcoffset() != provider_end.utcoffset()
                        or provider_end-provider_start != query_end-start):
                    raise ValueError('El período de visitas no coincide con los días solicitados.')
                effective_end = min(provider_end, end) if live else provider_end
                if effective_end <= provider_start:
                    raise ValueError('El día de visitas de Mercado Libre todavía no comenzó.')
                aligned_rows = rows
                if provider_start < start or effective_end > end:
                    aligned_rows, _ = await all_orders(client, self.seller,
                        provider_start.isoformat(), effective_end.isoformat())
                aligned_sales = len({str(o['id']) for o in aligned_rows if o.get('status') == 'paid'
                    and provider_start <= instant(o['date_created']) < effective_end})
                payload.update(visits=data['total_visits'], paid_orders=aligned_sales, status='available',
                    period_start=provider_start.isoformat(), period_end=effective_end.isoformat(),
                    provider_period_end=provider_end.isoformat(),
                    reason='Visitas y ventas alineadas al corte de Mercado Libre ('
                        + provider_start.strftime('UTC%z') + '), distinto del corte financiero argentino.'
                        if provider_start != start else 'Visitas y ventas con el mismo corte horario.')
                if live:
                    payload['reason'] += ' Actualización cada 30 s; acumulado provisorio sujeto a la demora de Mercado Libre.'
            except Exception as error:
                payload['error_type'] = type(error).__name__
                # Do not expose tokens, raw provider payloads or customer data.
                message = str(error)
                http = next((code for code in ('400', '401', '403', '404', '422', '429', '500', '502', '503')
                             if 'HTTP ' + code in message), None)
                payload['reason'] = ('Mercado Libre devolvió HTTP ' + http + ' al consultar visitas.' if http
                    else message if isinstance(error, ValueError) else 'No se pudo verificar la lectura de visitas.')
            if payload['visits'] is None and cached:
                previous = json.loads(cached[0])
                if previous.get('visits') is not None:
                    previous.update(stale=True, status='stale', reason='Falló la consulta; última lectura conservada. ' + payload['reason'])
                    result.update(previous)
                    if result['visits']:
                        result['conversion_percent'] = str(amount(result['paid_orders'])*100/amount(result['visits']))
                    return result
            with self.db() as c:
                c.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)', (key, json.dumps(payload), time.time()))
                c.execute("DELETE FROM snapshots WHERE day LIKE 'traffic:%' AND updated_at < ?", (time.time()-86400*2,))
            result.update(payload)
        if result['visits']:
            result['conversion_percent'] = str(amount(result['paid_orders'])*100/amount(result['visits']))
        elif result['visits'] == 0:
            result['reason'] = 'Sin visitas: conversión no calculable (no es 0%).'
        return result

    async def diagnose_traffic(self, day):
        """Bounded read-only probes of documented ISO date parameters.

        Invoked only by an explicit summary tool call, never by dashboard polling.
        Return aggregate evidence; no credentials or buyer information.
        """
        from datetime import date
        target = date.fromisoformat(day)
        now = datetime.now(TZ)
        if not now.date() - timedelta(days=150) <= target <= now.date():
            return {'status': 'outside_visit_range'}
        start = datetime.combine(target, datetime.min.time(), TZ)
        end = min(start + timedelta(days=1), now)
        client = await self.auto.client()
        probes = []
        for label, stop in (('argentina_elapsed', end), ('argentina_calendar', start + timedelta(days=1))):
            params = {'date_from': start.isoformat(timespec='milliseconds'), 'date_to': stop.isoformat(timespec='milliseconds')}
            entry = {'query': label, 'requested_range': params,
                     'fetched_at': datetime.now(TZ).isoformat()}
            try:
                data = await asyncio.wait_for(client.get(f'/users/{self.seller}/items_visits', params), 8)
                if not isinstance(data, dict) or str(data.get('user_id')) != str(self.seller):
                    raise ValueError('Unexpected seller response')
                entry.update({k: data.get(k) for k in ('total_visits', 'date_from', 'date_to')})
                entry['status'] = 'received'
            except Exception as exc:
                http = next((code for code in (400,401,403,404,422,429,500,502,503) if 'HTTP ' + str(code) in str(exc)), None)
                entry.update(status='unavailable', error_type=type(exc).__name__, http_status=http)
                if http in (401,403,429):
                    probes.append(entry)
                    break
            probes.append(entry)
        return {'probes': probes, 'official_equivalence_verified': False}

    async def run(self):
        # Existing deployment is single-process. Read-only and independent of reply activation.
        older_day = 2
        while True:
            for delta in (0, 1, older_day):
                try:
                    await self.snapshot((datetime.now(TZ).date() - timedelta(days=delta)).isoformat())
                except Exception:
                    pass  # No customer data or credentials in logs; API surfaces missing/stale data.
            older_day = 2 if older_day >= 31 else older_day + 1
            await asyncio.sleep(300)


def register(mcp, api, auto, seller, data, env):
    from fastmcp.exceptions import ToolError
    monitor = Monitor(data, auto, seller, env['BASE_URL'], env.get('JWT_SIGNING_KEY'))

    @mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
    def nf_monitor_configuracion() -> dict:
        """Lee costos fechados y conciliaciones del monitor. No son datos fiscales automáticos."""
        api()
        return monitor.config()

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False})
    def nf_monitor_configurar(datos_json: str, expected_revision: int) -> dict:
        """Guarda configuración explícita del titular: currency ARS, costs [{sku,unit_cost,effective_from,source}],
        kits {item_id:variation_id: [{sku,quantity}]}, orders {id:{refund,fee,cogs,logistics,tax_adjustment,source}},
        products {item_id:variation_id:{name,variant}}, days {YYYY-MM-DD:{ads,fixed_costs,source}}.
        Importes conciliados: jamás completar desconocidos con cero.
        Leer nf_monitor_configuracion antes. Tax_adjustment es ajuste firmado sobre base de caja, no retenciones automáticas.
        """
        api()
        try:
            return monitor.configure(json.loads(datos_json), expected_revision)
        except (ValueError, KeyError, TypeError) as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False})
    def nf_monitor_abrir() -> dict:
        """Devuelve el login del monitor y enlaces de activación de cuentas pendientes.
        Sólo para el titular. Los enlaces son privados, de un uso y duran 24 horas.
        Cada llamada reemplaza los enlaces pendientes anteriores. Nunca solicita contraseñas en el chat.
        """
        api()
        return monitor.auth.activation_links()

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': True, 'openWorldHint': False})
    def nf_monitor_usuario_restablecer(usuario: str, confirmacion: str) -> dict:
        """Restablece acceso de salvador o maxi SOLO por orden explícita del titular.
        Invalida la contraseña y TODAS las sesiones de ese usuario; devuelve enlace privado
        de un uso para elegir nueva contraseña. No cambia ML/MP. Requiere confirmacion=RESTABLECER_ACCESO_MONITOR.
        """
        api()
        if confirmacion != 'RESTABLECER_ACCESO_MONITOR':
            raise ToolError('Falta confirmación explícita de restablecimiento.')
        try:
            return monitor.auth.reset(usuario)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
    async def nf_monitor_resumen(fecha: str) -> dict:
        """Monitor por día de Argentina, últimos 366 días. Cache 5 minutos; neto nulo si faltan costos/conciliación."""
        api()
        try:
            result = copy.deepcopy(await monitor.snapshot(fecha))
            try:
                result['traffic_diagnostic'] = await monitor.diagnose_traffic(fecha)
            except Exception:
                result['traffic_diagnostic'] = {'status': 'unavailable'}
            return result
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @mcp.custom_route('/monitor', methods=['GET'])
    async def page(request):
        if not monitor.authorized(request):
            return RedirectResponse('/monitor/login', status_code=303, headers=HEADERS)
        return HTMLResponse((Path(__file__).parent / 'monitor.html').read_text(), headers=HEADERS)

    @mcp.custom_route('/monitor/login', methods=['GET'])
    async def login_page(request):
        return HTMLResponse((Path(__file__).parent / 'monitor-login.html').read_text(), headers=HEADERS)

    @mcp.custom_route('/monitor/assets/{name}', methods=['GET'])
    async def asset(request):
        from starlette.responses import Response
        name = request.path_params['name']
        if name not in ('monitor.js', 'monitor.css', 'monitor-login.js', 'monitor-login.css', 'northfitness-logo.jpg', 'apple-touch-icon.png'):
            return Response(status_code=404)
        path = Path(__file__).parent / name
        if name.endswith(('.jpg', '.png')):
            encoded_name = 'apple-touch-icon.b64' if name.endswith('.png') else 'northfitness-logo.b64'
            encoded = (Path(__file__).parent / encoded_name).read_bytes()
            try:
                content = base64.b64decode(b''.join(encoded.split()), validate=True)
            except ValueError:
                return Response(status_code=500, headers=HEADERS)
            return Response(content, headers=HEADERS, media_type='image/png' if name.endswith('.png') else 'image/jpeg')
        return Response(path.read_text(), headers=HEADERS,
                        media_type='text/javascript' if name.endswith('.js') else 'text/css')

    @mcp.custom_route('/monitor/session', methods=['POST'])
    async def session(request):
        # Explicitly retired: no fallback to permanent or one-use legacy bearer links.
        return JSONResponse({'error': 'El acceso por enlace fue deshabilitado. Ingresá con tu usuario y contraseña.'},
                            status_code=401, headers=HEADERS)

    async def credentials(request, activation=False):
        if request.headers.get('origin') != monitor.base_url:
            return JSONResponse({'error': 'Origen no autorizado.'}, status_code=403, headers=HEADERS)
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            return JSONResponse({'error': 'Solicitud inválida.'}, status_code=400, headers=HEADERS)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                return JSONResponse({'error': 'Solicitud demasiado grande.'}, status_code=413, headers=HEADERS)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse({'error': 'Solicitud inválida.'}, status_code=400, headers=HEADERS)
        try:
            result = await asyncio.to_thread(monitor.auth.authenticate, payload,
                                            request.client.host if request.client else 'unknown', activation)
        except AuthError as exc:
            headers = dict(HEADERS)
            if exc.status == 429:
                headers['Retry-After'] = '900'
            return JSONResponse({'error': str(exc)}, status_code=exc.status, headers=headers)
        monitor.auth.logout(request)
        response = JSONResponse({'ok': True, 'username': result['username']}, headers=HEADERS)
        response.set_cookie(COOKIE, result['token'], max_age=result['seconds'] if result['remember'] else None,
                            secure=True, httponly=True, samesite='strict', path='/monitor')
        response.delete_cookie('nf_monitor', path='/monitor', secure=True, httponly=True, samesite='strict')
        return response

    @mcp.custom_route('/monitor/login', methods=['POST'])
    async def login(request):
        return await credentials(request)

    @mcp.custom_route('/monitor/activate', methods=['POST'])
    async def activate(request):
        return await credentials(request, activation=True)

    @mcp.custom_route('/monitor/logout', methods=['POST'])
    async def logout(request):
        if request.headers.get('origin') != monitor.base_url:
            return JSONResponse({'error': 'Origen no autorizado.'}, status_code=403, headers=HEADERS)
        monitor.auth.logout(request)
        response = JSONResponse({'ok': True}, headers=HEADERS)
        response.delete_cookie(COOKIE, path='/monitor', secure=True, httponly=True, samesite='strict')
        return response

    @mcp.custom_route('/monitor/me', methods=['GET'])
    async def me(request):
        username = monitor.auth.user(request)
        return JSONResponse({'username': username}, status_code=200 if username else 401, headers=HEADERS)

    @mcp.custom_route('/monitor/data', methods=['GET'])
    async def data_route(request):
        if not monitor.authorized(request):
            return JSONResponse({'error': 'Ingresá con tu usuario y contraseña.'}, status_code=401, headers=HEADERS)
        try:
            result = await monitor.period(request.query_params.get('date', datetime.now(TZ).date().isoformat()), request.query_params.get('period', 'day'), force=request.query_params.get('refresh') == '1')
            return JSONResponse(result, headers=HEADERS)
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=503, headers=HEADERS)

    @mcp.custom_route('/monitor/compare', methods=['GET'])
    async def compare_route(request):
        if not monitor.authorized(request):
            return JSONResponse({'error': 'Acceso privado requerido.'}, status_code=401, headers=HEADERS)
        try:
            result = await monitor.compare(request.query_params.get('date', ''),
                request.query_params.get('period', 'day'), request.query_params.get('reference', ''))
            return JSONResponse(result, headers=HEADERS)
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400, headers=HEADERS)
        except Exception:
            return JSONResponse({'error': 'No se pudo consultar la comparación. Reintentá.'}, status_code=503, headers=HEADERS)

    return monitor


def install(app, monitor, enabled):
    if not enabled:
        return
    original = app.router.lifespan_context
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with original(app):
            task = asyncio.create_task(monitor.run())
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    app.router.lifespan_context = lifespan
