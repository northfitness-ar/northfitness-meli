"""Seller-bound, resumable API sales exports. Not the seller-panel Excel or tax invoices."""
import asyncio
import base64
import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastmcp.exceptions import ToolError
from sales_data import orders_page

VERSION = '1'
AR = timezone(timedelta(hours=-3))
CENT = Decimal('.01')
PROVINCES = ('Buenos Aires', 'CABA', 'Catamarca', 'Chaco', 'Chubut', 'Córdoba',
             'Corrientes', 'Entre Ríos', 'Formosa', 'Jujuy', 'La Pampa', 'La Rioja',
             'Mendoza', 'Misiones', 'Neuquén', 'Río Negro', 'Salta', 'San Juan',
             'San Luis', 'Santa Cruz', 'Santa Fe', 'Santiago del Estero',
             'Tierra del Fuego', 'Tucumán')


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(c)).strip()


NAMES = {normalized(p): p for p in PROVINCES}
NAMES.update({'capital federal': 'CABA', 'ciudad autonoma de buenos aires': 'CABA',
              'bs.as. g.b.a. norte': 'Buenos Aires', 'bs.as. g.b.a. sur': 'Buenos Aires',
              'bs.as. g.b.a. oeste': 'Buenos Aires', 'buenos aires interior': 'Buenos Aires'})


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    text = str(value)
    if not re.fullmatch(r'[0-9]{1,24}', text):
        raise ValueError('Identificador inválido en la fuente.')
    return text


def money(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Importe ausente o inválido; no totalizar.') from None
    if not result.is_finite() or result < 0 or result > Decimal('1000000000000'):
        raise ValueError('Importe fuera de rango; no totalizar.')
    return result


def amount(value):
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), '.2f')


def dates(desde, hasta):
    if not all(isinstance(v, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', v)
               for v in (desde, hasta)):
        raise ValueError('Usar fechas YYYY-MM-DD inclusivas, horario Argentina.')
    start, end = date.fromisoformat(desde), date.fromisoformat(hasta)
    if not 0 <= (end - start).days < 31 or end >= datetime.now(AR).date():
        raise ValueError('Elegir de 1 a 31 días completos, como máximo hasta ayer en Argentina.')
    return (datetime.combine(start, time.min, AR).isoformat(),
            datetime.combine(end + timedelta(days=1), time.min, AR).isoformat())


def project(order):
    """No buyer, product, address or payment payloads enter persistent reports."""
    if order.get('currency_id') != 'ARS':
        raise ValueError('Moneda distinta de ARS; no mezclar importes.')
    items = order.get('order_items')
    if not isinstance(items, list) or not items:
        raise ValueError('Orden sin detalle de importes.')
    gross, units = Decimal(0), 0
    for item in items:
        qty = item.get('quantity')
        if type(qty) is not int or not 0 < qty <= 100000:
            raise ValueError('Cantidad inválida.')
        gross += money(item.get('unit_price')) * qty
        units += qty
    if abs(gross - money(order.get('total_amount'))) > CENT:
        raise ValueError('El detalle no coincide con el total de la orden; conciliar antes de exportar.')
    status = order.get('status')
    if status not in ('paid', 'cancelled', 'confirmed', 'payment_required',
                      'payment_in_process', 'partially_paid', 'invalid'):
        raise ValueError('Estado de orden desconocido; revisar antes de totalizar.')
    payments = order.get('payments')
    review = payments is None or not payments
    for p in payments or []:
        if (p.get('status') != 'approved' or p.get('transaction_amount_refunded') is None
                or money(p['transaction_amount_refunded']) > 0):
            review = True
    sid = (order.get('shipping') or {}).get('id')
    return {'order_id': identifier(order.get('id')),
            'pack_id': identifier(order['pack_id']) if order.get('pack_id') else None,
            'created_at': order['date_created'], 'status': status,
            'shipment_id': identifier(sid) if sid else None,
            'units': units, 'gross_sales_ars': amount(gross), 'included': status == 'paid',
            'refund_review_required': review, 'province': None,
            'province_status': 'pending' if status == 'paid' and sid else
                               ('no_shipment' if status == 'paid' else 'excluded')}


async def destination(client, seller, shipment_id):
    try:
        body = await asyncio.wait_for(client.get('/shipments/' + identifier(shipment_id),
                                                 headers={'x-format-new': 'true'}), timeout=5)
    except (ToolError, TimeoutError):
        return {'province': None, 'province_status': 'unavailable'}
    if (not isinstance(body, dict) or str(body.get('id')) != shipment_id
            or str(body.get('sender_id')) != str(seller)):
        raise ValueError('Envío sin titularidad verificada; se detuvo el reporte.')
    address = body.get('receiver_address') or {}
    country = address.get('country') or {}
    state = address.get('state') or {}
    if country.get('id') != 'AR':
        return {'province': None, 'province_status': 'country_unverified'}
    name = state.get('name')
    province = NAMES.get(normalized(name)) if isinstance(name, str) else None
    return {'province': province, 'province_status': 'verified' if province else 'unknown_state'}


def summary(report):
    rows = report['rows']
    included = [r for r in rows if r['included']]
    total = sum((Decimal(r['gross_sales_ars']) for r in included), Decimal(0))
    groups = {}
    for r in included:
        name = r['province'] or 'Sin provincia verificada'
        g = groups.setdefault(name, {'province': name, 'sales_count': 0, 'units': 0,
                                     'gross_sales_ars': Decimal(0)})
        g['sales_count'] += 1
        g['units'] += r['units']
        g['gross_sales_ars'] += Decimal(r['gross_sales_ars'])
    result = []
    for g in sorted(groups.values(), key=lambda x: (-x['gross_sales_ars'], x['province'])):
        result.append({**g, 'gross_sales_ars': amount(g['gross_sales_ars']),
                       'revenue_share_pct': amount(g['gross_sales_ars'] * 100 / total) if total else None,
                       'sales_share_pct': amount(Decimal(g['sales_count']) * 100 / len(included))})
    missing = [r for r in included if not r['province']]
    return {'sales_count': len(included), 'units': sum(r['units'] for r in included),
            'gross_sales_ars': amount(total), 'by_province': result,
            'unassigned_sales_count': len(missing),
            'unassigned_gross_sales_ars': amount(sum((Decimal(r['gross_sales_ars']) for r in missing), Decimal(0))),
            'geography_complete': not missing,
            'refund_review_count': sum(r['refund_review_required'] for r in included),
            'excluded_orders_count': len(rows) - len(included),
            'cancelled_orders_count': sum(r['status'] == 'cancelled' for r in rows)}


class Reports:
    def __init__(self, path, seller):
        self.path, self.seller = str(path), str(seller)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS sales_reports '
                       '(id TEXT PRIMARY KEY, seller TEXT, version INTEGER, body TEXT)')

    def load(self, report_id):
        if not re.fullmatch(r'[a-f0-9]{32}', report_id):
            raise ValueError('ID de reporte inválido.')
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT version,body FROM sales_reports WHERE id=? AND seller=?',
                             (report_id, self.seller)).fetchone()
        if row is None:
            raise ValueError('Reporte inexistente para esta cuenta.')
        return row[0], json.loads(row[1])

    def create(self, desde, hasta):
        start, end = dates(desde, hasta)
        report = {'report_id': uuid.uuid4().hex, 'desde': desde, 'hasta': hasta,
                  'start': start, 'end_exclusive': end, 'created_at': now(), 'updated_at': now(),
                  'stage': 'orders', 'offset': 0, 'expected_total': None, 'excluded_out_of_range': 0,
                  'rows': [], 'geography': {}}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM sales_reports WHERE seller=? AND '
                       "json_extract(body, '$.created_at') < ?",
                       (self.seller, (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()))
            if db.execute('SELECT COUNT(*) FROM sales_reports WHERE seller=?', (self.seller,)).fetchone()[0] >= 100:
                raise ValueError('Límite de 100 reportes por 30 días alcanzado; reutilizar reportes existentes.')
            db.execute('INSERT INTO sales_reports VALUES (?,?,?,?)',
                       (report['report_id'], self.seller, 1, json.dumps(report)))
        return self.status(report)

    @staticmethod
    def status(r):
        return {k: r[k] for k in ('report_id', 'desde', 'hasta', 'created_at', 'updated_at', 'stage')} | {
            'complete': r['stage'] == 'ready', 'orders_downloaded': len(r['rows']),
            'reported_total': r['expected_total'], 'shipments_checked': len(r['geography']),
            'shipments_pending': len({x['shipment_id'] for x in r['rows']
                                      if x['province_status'] == 'pending'}),
            'source': 'Mercado Libre API /orders/search + /shipments/{id}',
            'native_panel_excel': False, 'retention_days': 30,
            'warning': 'Reporte comercial por fecha de venta y destino del envío. No es facturación fiscal ni base imponible IIBB. '
                       'Importe bruto de productos: sin envío, sin descontar cargos ni reembolsos; sólo órdenes paid. '
                       'Cantidad de ventas = órdenes distintas, no unidades ni carritos. No totalizar antes de complete=true.'}

    async def advance(self, client, report_id):
        version, r = self.load(report_id)
        if r['stage'] == 'ready':
            return self.status(r)
        if r['stage'] == 'orders':
            p = await orders_page(client, self.seller, r['start'], r['end_exclusive'], r['offset'])
            if r['expected_total'] is not None and r['expected_total'] != p['reported_total']:
                raise ValueError('Cambió la cantidad de órdenes durante la descarga; crear un nuevo reporte.')
            if p['reported_total'] > 9900:
                raise ValueError('Más de 9900 órdenes; dividir el período para respetar la paginación.')
            r['expected_total'] = p['reported_total']
            existing = {x['order_id'] for x in r['rows']}
            for order in p['orders']:
                row = project(order)
                if row['order_id'] in existing:
                    raise ValueError('Orden duplicada entre páginas; crear un nuevo reporte.')
                existing.add(row['order_id'])
                r['rows'].append(row)
            r['excluded_out_of_range'] += p['excluded_out_of_range']
            r['offset'] = p['next_offset']
            if p['complete']:
                r['stage'] = 'geography'
        elif r['stage'] == 'geography':
            pending = list(dict.fromkeys(x['shipment_id'] for x in r['rows']
                                        if x['province_status'] == 'pending'))[:20]
            # Four batches of five: bounded runtime, shared HTTP pool, no detached tasks.
            for i in range(0, len(pending), 5):
                ids = pending[i:i+5]
                values = await asyncio.gather(*(destination(client, self.seller, sid) for sid in ids),
                                              return_exceptions=True)
                for sid, value in zip(ids, values):
                    if isinstance(value, BaseException):
                        raise value
                    r['geography'][sid] = value
            for row in r['rows']:
                if row['province_status'] == 'pending' and row['shipment_id'] in r['geography']:
                    row.update(r['geography'][row['shipment_id']])
        if r['stage'] == 'geography' and not any(x['province_status'] == 'pending' for x in r['rows']):
            r['stage'] = 'ready'
        r['updated_at'] = now()
        with closing(sqlite3.connect(self.path)) as db, db:
            changed = db.execute('UPDATE sales_reports SET version=version+1,body=? '
                                 'WHERE id=? AND seller=? AND version=?',
                                 (json.dumps(r), report_id, self.seller, version)).rowcount
            if changed != 1:
                raise ValueError('Otra consulta avanzó el reporte; volver a leerlo antes de continuar.')
        return self.status(r)

    def read(self, report_id, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Offset no negativo y límite entre 1 y 200.')
        _, r = self.load(report_id)
        ready = r['stage'] == 'ready'
        rows = r['rows'][offset:offset+limit] if ready else []
        return self.status(r) | {'summary': summary(r) if ready else None, 'rows': rows,
                                 'excluded_out_of_range': r['excluded_out_of_range'],
                                 'next_offset': offset+limit if ready and offset+limit < len(r['rows']) else None}

    def download(self, report_id, kind='provincias', offset=0):
        if kind not in ('provincias', 'ventas') or type(offset) is not int or offset < 0 or offset % 48000:
            raise ValueError('Tipo provincias/ventas y offset en bytes múltiplo de 48000.')
        _, r = self.load(report_id)
        if r['stage'] != 'ready':
            raise ValueError('Completar nf_ventas_reporte_avanzar antes de descargar.')
        s = summary(r)
        rows = s['by_province'] if kind == 'provincias' else r['rows']
        fields = (['province', 'sales_count', 'units', 'gross_sales_ars', 'revenue_share_pct', 'sales_share_pct']
                  if kind == 'provincias' else
                  ['order_id', 'pack_id', 'created_at', 'status', 'shipment_id', 'units', 'gross_sales_ars',
                   'included', 'refund_review_required', 'province', 'province_status'])
        output = io.StringIO(newline='')
        writer = csv.writer(output, delimiter=';')
        writer.writerow(['desde', 'hasta', *fields])
        for row in rows:
            # Prevent spreadsheet formula execution, even for future provider fields.
            values = [r['desde'], r['hasta'], *(row.get(k) for k in fields)]
            writer.writerow(["'"+v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@'))
                             else v for v in values])
        content = output.getvalue().encode('utf-8-sig')
        if offset >= len(content):
            raise ValueError('Offset fuera del archivo.')
        chunk = content[offset:offset+48000]
        return self.status(r) | {'filename': f'NF_{kind}_{r["desde"]}_{r["hasta"]}.csv',
                                 'mime_type': 'text/csv', 'encoding': 'base64', 'charset': 'utf-8-sig',
                                 'sha256': hashlib.sha256(content).hexdigest(), 'total_bytes': len(content),
                                 'offset': offset, 'data_base64': base64.b64encode(chunk).decode(),
                                 'download_complete': offset+len(chunk) == len(content),
                                 'next_offset': offset+len(chunk) if offset+len(chunk) < len(content) else None,
                                 'summary': s}


def register(mcp, api, seller, data):
    reports = Reports(data / 'sales_reports.sqlite3', seller)
    read = {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}
    write = {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': True}

    def checked(fn, *args):
        try:
            return fn(*args)
        except (ValueError, KeyError, TypeError, InvalidOperation):
            raise ToolError('Reporte inválido o inconsistente. Revisar rango, identificador, paginación y datos; no interpretar como cero.') from None

    @mcp.tool(annotations=write)
    def nf_ventas_reporte_crear(desde: str, hasta: str) -> dict:
        """Crea un reporte comercial API (no Excel del panel), fechas YYYY-MM-DD inclusivas Argentina.
        Máximo 31 días completos hasta ayer. Luego avanzar hasta complete=true. No modifica ventas.
        """
        api()
        return checked(reports.create, desde, hasta)

    @mcp.tool(annotations=write)
    async def nf_ventas_reporte_avanzar(report_id: str) -> dict:
        """Descarga una página de órdenes o consulta hasta 20 envíos y guarda el progreso.
        Repetir hasta complete=true. Reanudable; no descargar totales parciales. No modifica Mercado Libre.
        """
        client = api()
        try:
            return await reports.advance(client, report_id)
        except (ValueError, KeyError, TypeError, InvalidOperation):
            raise ToolError('Descarga inconsistente. Leer progreso y crear otro reporte si cambió la fuente; no totalizar.') from None

    @mcp.tool(annotations=read)
    def nf_ventas_reporte_leer(report_id: str, offset: int = 0, limit: int = 100) -> dict:
        """Lee progreso, resumen por provincia y detalle paginado de un reporte inmutable al completar.
        Separar complete de summary.geography_complete. Sin provincia verificada nunca se estima.
        Ventas=órdenes paid; bruto de productos, sin deducir reembolsos. No acredita facturación fiscal.
        """
        api()
        return checked(reports.read, report_id, offset, limit)

    @mcp.tool(annotations=read)
    def nf_ventas_reporte_descargar(report_id: str, tipo: str = 'provincias', offset: int = 0) -> dict:
        """Exporta CSV provincias/ventas en bloques base64 de 48000 bytes por canal autenticado.
        Seguir next_offset hasta download_complete y verificar SHA256 antes de guardar el archivo.
        Resumen comercial: no es el Excel nativo del panel ni un libro de facturas ARCA.
        """
        api()
        return checked(reports.download, report_id, tipo, offset)

    return reports
