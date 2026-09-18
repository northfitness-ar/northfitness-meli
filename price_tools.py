"""Explicit ARS listing-price writes. No stock, ads or promotion writes."""
import hashlib
import json
import re
import sqlite3
from decimal import Decimal, InvalidOperation

import httpx
from fastmcp.exceptions import ToolError


def amount(value):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or not 0 < d <= 100000000 or d != d.quantize(Decimal('.01')):
            raise ValueError()
        return d
    except (ValueError, InvalidOperation):
        raise ToolError('Precio positivo en ARS, con hasta dos decimales.') from None


def fingerprint(item):
    fields = ('id', 'seller_id', 'currency_id', 'status', 'price', 'original_price',
              'listing_type_id', 'user_product_id', 'catalog_listing', 'channels')
    state = {k: item.get(k) for k in fields}
    state['variations'] = sorted(
        [(str(v['id']), str(v.get('price'))) for v in item.get('variations', [])])
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


async def snapshot(client, seller, item_id):
    if not re.fullmatch(r'MLA[0-9]+', item_id):
        raise ToolError('ID de publicación inválido.')
    item = await client.get('/items/' + item_id)
    if item.get('id') != item_id or str(item.get('seller_id')) != str(seller):
        raise ToolError('Publicación ajena a NorthFitness o respuesta inconsistente.')
    if item.get('currency_id') != 'ARS' or item.get('status') not in ('active', 'paused'):
        raise ToolError('Solo publicaciones argentinas activas/pausadas en ARS.')
    amount(item.get('price'))
    variants = item.get('variations') or []
    if len({v.get('id') for v in variants}) != len(variants) or any(not v.get('id') for v in variants):
        raise ToolError('Variantes inconsistentes.')
    for v in variants:
        amount(v.get('price'))
    return item


def view(item):
    return {k: item.get(k) for k in ('id', 'title', 'price', 'original_price', 'currency_id',
            'status', 'user_product_id', 'attributes', 'variations', 'channels')} | {
        'snapshot_hash': fingerprint(item),
        'warning': 'Precio de publicación, no precio final con cupones/promociones. '
                   'Cada color puede tener otro item_id. No sumar stock de publicaciones vinculadas.'}


class PriceChanges:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS price_changes(
                operation_id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS price_locks(
                resource TEXT PRIMARY KEY, operation_id TEXT NOT NULL);''')

    def previous(self, op, request):
        with sqlite3.connect(self.path) as c:
            row = c.execute('SELECT request,result FROM price_changes WHERE operation_id=?', (op,)).fetchone()
        if row:
            if row[0] != request:
                raise ToolError('operation_id ya usado para otro cambio.')
            return json.loads(row[1])

    def reserve(self, op, request, resource, base):
        with sqlite3.connect(self.path, timeout=15) as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT request,result FROM price_changes WHERE operation_id=?', (op,)).fetchone()
            if row:
                if row[0] != request:
                    raise ToolError('operation_id ya usado para otro cambio.')
                return json.loads(row[1])
            if c.execute('SELECT 1 FROM price_locks WHERE resource=?', (resource,)).fetchone():
                raise ToolError('Hay un cambio pendiente/incierto para este producto. Conciliar; no reenviar.')
            c.execute('INSERT INTO price_locks VALUES (?,?)', (resource, op))
            c.execute('INSERT INTO price_changes VALUES (?,?,?)',
                      (op, request, json.dumps(dict(base, state='unknown'))))

    def finish(self, op, result):
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE price_changes SET result=? WHERE operation_id=?', (json.dumps(result), op))
            if result['state'] in ('verified', 'unchanged'):
                c.execute('DELETE FROM price_locks WHERE operation_id=?', (op,))
        return result

    async def set(self, client, seller, item_id, price, expected, snapshot_hash, op):
        target, expected = amount(price), amount(expected)
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', op):
            raise ToolError('operation_id único de 8 a 100 caracteres.')
        if not re.fullmatch(r'[a-f0-9]{64}', snapshot_hash):
            raise ToolError('Consultar nf_precio_consultar y usar su snapshot_hash.')
        request = json.dumps([str(seller), item_id, str(target), str(expected), snapshot_hash])
        old = self.previous(op, request)
        if old is not None:
            return old
        before = await snapshot(client, seller, item_id)
        if amount(before['price']) != expected or fingerprint(before) != snapshot_hash:
            raise ToolError('La publicación cambió: consultar nuevamente antes de escribir.')
        if before.get('original_price') is not None:
            raise ToolError('Posible promoción activa: revisar precio efectivo antes de modificar. No se quitaron promociones.')
        variants = before.get('variations') or []
        if any(amount(v['price']) != expected for v in variants):
            raise ToolError('Variantes con precios diferentes: no se unifican automáticamente.')
        base = {'operation_id': op, 'item_id': item_id, 'currency_id': 'ARS',
                'before': str(expected), 'requested': str(target),
                'variation_ids': [v['id'] for v in variants]}
        # User-product aliases share the same lock, although each listing is verified separately.
        resource = str(seller) + ':' + str(before.get('user_product_id') or item_id)
        old = self.reserve(op, request, resource, base)
        if old is not None:
            return old
        if target == expected:
            return self.finish(op, dict(base, state='unchanged', observed=str(expected)))
        # Recheck after reserving: another worker may have finished since the first GET.
        try:
            current = await snapshot(client, seller, item_id)
        except Exception:
            return self.finish(op, dict(base, state='unknown', warning='Fallo de prelectura; requiere conciliación.'))
        if fingerprint(current) != snapshot_hash:
            return self.finish(op, dict(base, state='precondition_failed', warning='Cambió antes del envío; no se envió PUT.'))
        payload = ({'variations': [{'id': v['id'], 'price': float(target)} for v in variants]}
                   if variants else {'price': float(target)})
        try:
            r = await client.request('PUT', '/items/' + item_id, json=payload)
            status = r.status_code
            state = 'accepted' if 200 <= status < 300 else ('unknown' if status >= 500 else 'rejected')
        except httpx.RequestError:
            status, state = None, 'unknown'
        result = dict(base, state=state, http_status=status)
        if state == 'rejected':
            result['warning'] = 'No reintentar ni cambiar endpoint. Revisar permisos/contrato y conciliar.'
            return self.finish(op, result)
        try:
            after = await snapshot(client, seller, item_id)
            result['observed'] = str(amount(after['price']))
            after_variants = after.get('variations') or []
            preserved = ({v['id'] for v in variants} == {v['id'] for v in after_variants}
                         and all(before.get(k) == after.get(k) for k in
                                 ('status', 'listing_type_id', 'currency_id', 'user_product_id', 'channels')))
            exact = amount(after['price']) == target and all(amount(v['price']) == target for v in after_variants)
            result['other_settings_preserved'] = preserved
            if state == 'accepted':
                result['state'] = 'verified' if exact and preserved else 'verification_mismatch'
            # A timeout stays uncertain even when target is observed: never resend automatically.
            result['warning'] = 'Promociones, cupones y otros item_id no se verifican con esta operación.'
        except Exception:
            result['state'] = 'unknown'
            result['warning'] = 'No se pudo verificar el precio. No reenviar.'
        return self.finish(op, result)


def register(mcp, api, seller, data):
    changes = PriceChanges(data / 'price_changes.sqlite3')

    @mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
    async def nf_precio_consultar(item_id: str) -> dict:
        """Consulta precio ARS, variantes y snapshot_hash antes de cambiar precio.
        Los colores User Product pueden tener distintos item_id. No cambia nada.
        """
        return view(await snapshot(api(), seller, item_id))

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': True,
                          'idempotentHint': True, 'openWorldHint': True})
    async def nf_precio_fijar(item_id: str, precio_ars: str, precio_actual_esperado_ars: str,
                             snapshot_hash: str, operation_id: str) -> dict:
        """Fija precio ARS SOLO con orden explícita de importe y publicación/variantes.
        Leer nf_precio_consultar primero. Afecta TODAS las variantes clásicas del item.
        Para todos los colores, enumerar TODOS sus item_id y verificar cada uno; no es atómico.
        No modifica stock/Ads/promociones. No garantiza precio final de promociones o cupones.
        Reutilizar operation_id; unknown/rejected NO permiten repetir con otro ID.
        401/403: detenerse y revisar autorización. Solo verified confirma el resultado observado.
        """
        return await changes.set(api(), seller, item_id, precio_ars,
                                 precio_actual_esperado_ars, snapshot_hash, operation_id)
