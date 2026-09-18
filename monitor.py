"""Private read-only dashboard on the existing server. No credentials in URLs."""
import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from starlette.responses import HTMLResponse, JSONResponse
from sales_data import all_orders
from profitability import summarize, validate_policy, amount

TZ = ZoneInfo('America/Argentina/Buenos_Aires')
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
           'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"}


class Monitor:
    def __init__(self, data, auto, seller, base_url):
        self.path = str(data / 'monitor.sqlite3')
        self.auto, self.seller, self.base_url = auto, seller, base_url.rstrip('/')
        self.lock = asyncio.Lock()
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, revision INTEGER, body TEXT);
            CREATE TABLE IF NOT EXISTS policy_history (revision INTEGER PRIMARY KEY, body TEXT, saved_at REAL);
            CREATE TABLE IF NOT EXISTS snapshots (day TEXT PRIMARY KEY, body TEXT, updated_at REAL);
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
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            digest = hashlib.sha256(token.encode()).hexdigest()
            row = c.execute("SELECT expires FROM sessions WHERE hash=? AND kind='link'", (digest,)).fetchone()
            if not row or row[0] < time.time():
                return None
            c.execute('DELETE FROM sessions WHERE hash=?', (digest,))
        return self.issue('cookie', 8 * 3600)

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
        if not today - timedelta(days=31) <= target <= today:
            raise ValueError('Elegí una fecha entre hoy y los últimos 31 días.')
        async with self.lock:
            with self.db() as c:
                cached = c.execute('SELECT body,updated_at FROM snapshots WHERE day=?', (day,)).fetchone()
            cfg = self.config()
            if not force and cached and time.time() - cached[1] < 300:
                body = json.loads(cached[0])
                if body['policy_revision'] == cfg['revision']:
                    return body
            try:
                client = await self.auto.client()
                start = datetime.combine(target, datetime.min.time(), TZ)
                end = min(start + timedelta(days=1), datetime.now(TZ))
                rows, excluded = await all_orders(client, self.seller, start.isoformat(), end.isoformat())
                ads, ads_error = None, None
                try:
                    ads = await self.ads(client, day)
                except Exception:
                    ads_error = 'Publicidad no disponible; no se reemplazó por cero.'
                result = summarize(rows, cfg['policy'], day, ads)
                result.update(fetched_at=datetime.now(TZ).isoformat(), policy_revision=cfg['revision'],
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
    monitor = Monitor(data, auto, seller, env['BASE_URL'])

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
        """Genera acceso privado de un uso al monitor, válido 5 minutos. Sólo para el titular; no compartir."""
        api()
        return {'url': monitor.base_url + '/monitor#' + monitor.issue('link', 300), 'expires_in_seconds': 300}

    @mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
    async def nf_monitor_resumen(fecha: str) -> dict:
        """Monitor por día de Argentina, últimos 31 días. Cache 5 minutos; neto nulo si faltan costos/conciliación."""
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
            result = await monitor.snapshot(request.query_params.get('date', datetime.now(TZ).date().isoformat()), force=True)
            return JSONResponse(result, headers=HEADERS)
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=503, headers=HEADERS)

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

