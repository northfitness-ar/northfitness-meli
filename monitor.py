"""Private read-only dashboard on the existing server. No credentials in URLs."""
import asyncio
import base64
import contextlib
import copy
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from starlette.responses import HTMLResponse, JSONResponse
from sales_data import all_orders
from sales_data import instant
from profitability import summarize, summarize_period, validate_policy, amount

TZ = ZoneInfo('America/Argentina/Buenos_Aires')
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
           'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"}


class Monitor:
    def __init__(self, data, auto, seller, base_url, permanent_secret=None):
        self.path = str(data / 'monitor.sqlite3')
        self.auto, self.seller, self.base_url = auto, seller, base_url.rstrip('/')
        self.permanent_token = None
        if permanent_secret:
            digest = hmac.new(str(permanent_secret).encode(),
                              b'northfitness-monitor-permanent-link-v1', hashlib.sha256).digest()
            self.permanent_token = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
        self.lock = asyncio.Lock()
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, revision INTEGER, body TEXT);
            CREATE TABLE IF NOT EXISTS policy_history (revision INTEGER PRIMARY KEY, body TEXT, saved_at REAL);
            CREATE TABLE IF NOT EXISTS snapshots (day TEXT PRIMARY KEY, body TEXT, updated_at REAL);
            CREATE TABLE IF NOT EXISTS shipping_costs (shipment TEXT PRIMARY KEY, body TEXT, updated_at REAL);
            CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, kind TEXT, expires REAL);
            ''')

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

    def issue(self, kind, seconds):
        token = secrets.token_urlsafe(32)
        with self.db() as c:
            c.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
            c.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), kind, time.time() + seconds))
        return token

    def exchange(self, token):
        if not isinstance(token, str) or len(token) > 200:
            return None
        if self.permanent_token and secrets.compare_digest(token, self.permanent_token):
            return self.issue('cookie', 8 * 3600)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            digest = hashlib.sha256(token.encode()).hexdigest()
            row = c.execute("SELECT expires FROM sessions WHERE hash=? AND kind='link'", (digest,)).fetchone()
            if not row or row[0] < time.time():
                return None
            c.execute('DELETE FROM sessions WHERE hash=?', (digest,))
        return self.issue('cookie', 8 * 3600)

    def permanent_url(self):
        if not self.permanent_token:
            raise ValueError('Acceso permanente no configurado.')
        return self.base_url + '/monitor#' + self.permanent_token

    def authorized(self, request):
        token = request.cookies.get('nf_monitor', '')
        if not token or len(token) > 200:
            return False
        with self.db() as c:
            row = c.execute("SELECT expires FROM sessions WHERE hash=? AND kind='cookie'", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return bool(row and row[0] > time.time())

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
                if body['policy_revision'] == cfg['revision'] and body.get('calculation_version') == 2:
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
                result.update(fetched_at=datetime.now(TZ).isoformat(), policy_revision=cfg['revision'],
                              calculation_version=2,
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

    async def period(self, day, mode='day'):
        from datetime import date
        target, now = date.fromisoformat(day), datetime.now(TZ)
        if mode not in ('day', 'week', 'month'):
            raise ValueError('Período inválido.')
        if not now.date() - timedelta(days=366) <= target <= now.date():
            raise ValueError('Elegí una fecha de los últimos 366 días.')
        first = target if mode == 'day' else target - timedelta(days=target.weekday()) if mode == 'week' else target.replace(day=1)
        start = datetime.combine(first, datetime.min.time(), TZ)
        if mode == 'month':
            stop = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            stop = start + timedelta(days=7 if mode == 'week' else 1)
        end = min(stop, now)
        # Shift by whole weeks: same weekdays and identical elapsed duration.
        shift = timedelta(days=28 if mode == 'month' else 7)
        key = 'period:' + mode + ':' + first.isoformat()
        requested_at = time.time()
        async with self.lock:
            cfg = self.config()
            with self.db() as c:
                cached = c.execute('SELECT body,updated_at FROM snapshots WHERE day=?', (key,)).fetchone()
            if cached and cached[1] >= requested_at:
                shared = json.loads(cached[0])
                if shared.get('policy_revision') == cfg['revision'] and not shared.get('stale'):
                    return shared
            try:
                client = await self.auto.client()
                rows, excluded = await all_orders(client, self.seller, start.isoformat(), end.isoformat())
                effective_policy = await self.shipping_policy(client, rows, cfg['policy'])
                result = summarize_period(rows, effective_policy, start, end)
                result['traffic'] = await self.traffic(client, rows, start, end)
                result.update(fetched_at=datetime.now(TZ).isoformat(), policy_revision=cfg['revision'],
                              stale=False, refresh_seconds=30, mode=mode, excluded_out_of_range=excluded)
                try:
                    previous, _ = await all_orders(client, self.seller, (start-shift).isoformat(), (end-shift).isoformat())
                    comparison = summarize_period(previous, cfg['policy'], start-shift, end-shift)
                    current_sales = amount(result['gross']) - amount(result['cancelled'])
                    previous_sales = amount(comparison['gross']) - amount(comparison['cancelled'])
                    result['comparison'] = {'start': (start-shift).isoformat(), 'end': (end-shift).isoformat(),
                        'sales': str(previous_sales), 'percent': str((current_sales-previous_sales)*100/previous_sales) if previous_sales else None}
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
        if mode not in ('day', 'week'):
            raise ValueError('Elegí vista diaria o semanal para comparar días equivalentes.')
        if (target.year, target.month) != (base.year, base.month) or target.weekday() != base.weekday() or target == base:
            raise ValueError('Elegí otro día de la misma semana y del mismo mes.')
        if not now.date() - timedelta(days=366) <= min(target, base) <= max(target, base) <= now.date():
            raise ValueError('Fecha fuera del período disponible.')
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
        async with self.lock:
            policy = self.config()['policy']
            client = await self.auto.client()
            summaries = []
            for a, b in ((begin, end), (begin-shift, end-shift)):
                rows, _ = await all_orders(client, self.seller, a.isoformat(), b.isoformat())
                effective = await self.shipping_policy(client, rows, policy)
                summary = summarize_period(rows, effective, a, b)
                summary['traffic'] = await self.traffic(client, rows, a, b)
                summary.pop('orders', None)
                summaries.append(summary)
        return {'current': summaries[0], 'reference': summaries[1],
                'fetched_at': datetime.now(TZ).isoformat()}

    async def traffic(self, client, rows, start, end):
        """Seller listing visits, isolated from financial calculations and caches.

        Until reconciliation with the seller UI, conversion is explicitly an
        operational estimate: distinct paid orders / listing visits (not units).
        Never silently claim that this reproduces ML's private dashboard metric.
        """
        sales = len({str(o['id']) for o in rows if o.get('status') == 'paid'
                     and start <= instant(o['date_created']) < end})
        result = {'visits': None, 'paid_orders': sales, 'conversion_percent': None,
                  'status': 'pending', 'formula': 'Órdenes pagadas / visitas × 100',
                  'scope': 'Visitas a publicaciones, no visitantes únicos ni tienda',
                  'official_equivalence_verified': False, 'fetched_at': None}
        # A daily provider total cannot be used for an intraday comparison.
        if start.time() != datetime.min.time() or end.time() != datetime.min.time():
            result['reason'] = 'Visitas disponibles para días completos; elegí un día cerrado.'
            return result
        key = 'traffic:v1:' + start.isoformat() + ':' + end.isoformat()
        with self.db() as c:
            cached = c.execute('SELECT body,updated_at FROM snapshots WHERE day=?', (key,)).fetchone()
        if cached and cached[1] > time.time() - 900:
            result.update(json.loads(cached[0]))
        else:
            payload = {'visits': None, 'status': 'pending',
                       'fetched_at': datetime.now(TZ).isoformat()}
            try:
                data = await asyncio.wait_for(client.get(f'/users/{self.seller}/items_visits', {
                    'date_from': start.isoformat(timespec='milliseconds'),
                    'date_to': (end-timedelta(milliseconds=1)).isoformat(timespec='milliseconds')}), 25)
                payload['provider_shape'] = {'type': type(data).__name__,
                    'keys': sorted(str(k) for k in data)[:20] if isinstance(data, dict) else []}
                if isinstance(data, dict):
                    payload['provider_range'] = {k: data.get(k) for k in ('date_from', 'date_to')}
                if (str(data.get('user_id')) != str(self.seller)
                        or type(data.get('total_visits')) is not int or data['total_visits'] < 0):
                    raise ValueError('Respuesta de visitas incompleta o vendedor diferente.')
                if (instant(data['date_from']) != start
                        or instant(data['date_to']) != end-timedelta(milliseconds=1)):
                    raise ValueError('El corte horario de visitas no coincide con las ventas.')
                payload.update(visits=data['total_visits'], status='available')
            except Exception as error:
                payload['error_type'] = type(error).__name__
                # Do not expose tokens, raw provider payloads or customer data.
                message = str(error)
                http = next((code for code in ('401', '403', '404', '429', '500', '502', '503')
                             if 'HTTP ' + code in message), None)
                payload['reason'] = ('Mercado Libre devolvió HTTP ' + http + ' al consultar visitas.' if http
                    else message if isinstance(error, ValueError) else 'No se pudo verificar la lectura de visitas.')
            with self.db() as c:
                c.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)', (key, json.dumps(payload), time.time()))
                c.execute("DELETE FROM snapshots WHERE day LIKE 'traffic:%' AND updated_at < ?", (time.time()-86400*2,))
            result.update(payload)
        if result['visits']:
            result['conversion_percent'] = str(amount(sales)*100/amount(result['visits']))
        elif result['visits'] == 0:
            result['reason'] = 'Sin visitas: conversión no calculable (no es 0%).'
        return result

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
        """Devuelve el acceso privado permanente al monitor. Sólo para el titular; no compartir."""
        api()
        try:
            return {'url': monitor.permanent_url(), 'permanent': True, 'expires_in_seconds': None}
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
    async def nf_monitor_resumen(fecha: str) -> dict:
        """Monitor por día de Argentina, últimos 366 días. Cache 5 minutos; neto nulo si faltan costos/conciliación."""
        api()
        try:
            return await monitor.snapshot(fecha)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @mcp.custom_route('/monitor', methods=['GET'])
    async def page(request):
        return HTMLResponse((Path(__file__).parent / 'monitor.html').read_text(), headers=HEADERS)

    @mcp.custom_route('/monitor/assets/{name}', methods=['GET'])
    async def asset(request):
        from starlette.responses import Response
        name = request.path_params['name']
        if name not in ('monitor.js', 'monitor.css', 'northfitness-logo.jpg'):
            return Response(status_code=404)
        path = Path(__file__).parent / name
        if name.endswith('.jpg'):
            encoded = (Path(__file__).parent / 'northfitness-logo.b64').read_bytes()
            try:
                content = base64.b64decode(b''.join(encoded.split()), validate=True)
            except ValueError:
                return Response(status_code=500, headers=HEADERS)
            return Response(content, headers=HEADERS, media_type='image/jpeg')
        return Response(path.read_text(), headers=HEADERS,
                        media_type='text/javascript' if name.endswith('.js') else 'text/css')

    @mcp.custom_route('/monitor/session', methods=['POST'])
    async def session(request):
        if request.headers.get('origin') != monitor.base_url:
            return JSONResponse({'error': 'Origen no autorizado.'}, status_code=403, headers=HEADERS)
        raw = await request.body()
        if len(raw) > 512:
            return JSONResponse({'error': 'Solicitud inválida.'}, status_code=400, headers=HEADERS)
        try:
            token = monitor.exchange(json.loads(raw).get('token'))
        except (ValueError, AttributeError):
            token = None
        if not token:
            return JSONResponse({'error': 'Enlace vencido o ya utilizado. Pedí un nuevo acceso desde NorthFitness.'}, status_code=401, headers=HEADERS)
        response = JSONResponse({'ok': True}, headers=HEADERS)
        response.set_cookie('nf_monitor', token, max_age=8*3600, secure=True, httponly=True, samesite='strict', path='/monitor')
        return response

    @mcp.custom_route('/monitor/data', methods=['GET'])
    async def data_route(request):
        if not monitor.authorized(request):
            return JSONResponse({'error': 'Pedí «abrir monitor» en NorthFitness para acceder.'}, status_code=401, headers=HEADERS)
        try:
            result = await monitor.period(request.query_params.get('date', datetime.now(TZ).date().isoformat()), request.query_params.get('period', 'day'))
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
