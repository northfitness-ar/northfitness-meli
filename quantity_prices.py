"""Conservative updates of EXISTING B2B quantity prices; no arbitrary API proxy.

Contract reference: https://a2systems.co/blog/blog-2/actualizando-precios-mayoristas-en-mercadolibre-341
Live eligibility must be established by reading the existing price records.
"""
import hashlib
import json
import re
from decimal import Decimal, ROUND_HALF_UP

import httpx
from fastmcp.exceptions import ToolError
from price_tools import PriceChanges, amount, snapshot, fingerprint

CONTEXT = ['channel_marketplace', 'user_type_business']


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def split_prices(data):
    if not isinstance(data, dict) or not isinstance(data.get('prices'), list):
        raise ToolError('Respuesta de precios no reconocida; no escribir.')
    tiers, other = [], []
    for p in data['prices']:
        if not isinstance(p, dict):
            raise ToolError('Registro de precio inválido.')
        c = p.get('conditions') or {}
        if not isinstance(c, dict):
            raise ToolError('Condiciones no reconocidas.')
        q = c.get('min_purchase_unit')
        if q is None:
            other.append(p)
            continue
        if (p.get('type') != 'standard' or p.get('currency_id') != 'ARS'
                or sorted(c.get('context_restrictions') or []) != CONTEXT
                or set(c) - {'context_restrictions', 'min_purchase_unit', 'start_time', 'end_time'}
                or c.get('start_time') is not None or c.get('end_time') is not None
                or type(q) is not int or q < 2):
            raise ToolError('Escala distinta de B2B estándar existente; no se modifica.')
        tiers.append({'quantity': q, 'amount': format(amount(p.get('amount')), '.2f')})
    tiers.sort(key=lambda t: t['quantity'])
    if not 1 <= len(tiers) <= 5 or len({t['quantity'] for t in tiers}) != len(tiers):
        raise ToolError('Se requieren entre 1 y 5 escalas existentes únicas.')
    return tiers, sorted(other, key=canonical)


async def read(client, seller, item_id):
    item = await snapshot(client, seller, item_id)
    if item.get('variations'):
        raise ToolError('Elegir publicación de un solo color, sin variantes clásicas.')
    data = await client.get('/items/' + item_id + '/prices')
    if data.get('id') != item_id:
        raise ToolError('Respuesta de precios de otra publicación.')
    tiers, other = split_prices(data)
    state = {'item_id': item_id, 'title': item.get('title'),
             'retail_price': str(amount(item['price'])), 'tiers': tiers,
             'item_fingerprint': fingerprint(item), 'other_prices': other,
             'resource': str(seller) + ':' + str(item.get('user_product_id') or item_id)}
    state['snapshot_hash'] = hashlib.sha256(canonical(state).encode()).hexdigest()
    return state


def targets(tiers, rows, retail):
    if not isinstance(rows, list) or len(rows) != len(tiers):
        raise ToolError('Incluir exactamente las escalas existentes; no se agregan ni eliminan.')
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'quantity', 'discount_percent'}:
            raise ToolError('Cada escala requiere quantity y discount_percent.')
        q = row['quantity']
        d = amount(row['discount_percent'])
        if type(q) is not int or d >= 100:
            raise ToolError('Cantidad entera y descuento mayor a 0 y menor a 100.')
        price = (amount(retail) * (1 - d / 100)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        amount(price)
        result.append({'quantity': q, 'amount': str(price)})
    result.sort(key=lambda t: t['quantity'])
    if [t['quantity'] for t in result] != [t['quantity'] for t in tiers]:
        raise ToolError('No cambiar cantidades mínimas ni duplicar escalas.')
    if any(amount(a['amount']) <= amount(b['amount']) for a, b in zip(result, result[1:])):
        raise ToolError('El precio debe disminuir al aumentar la cantidad.')
    return result


async def update(changes, client, seller, item_id, rows, expected, op):
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', op) or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ToolError('operation_id o snapshot_hash inválido.')
    request = canonical(['quantity', str(seller), item_id, rows, expected])
    previous = changes.previous(op, request)
    if previous is not None:
        return previous
    before = await read(client, seller, item_id)
    if before['snapshot_hash'] != expected:
        raise ToolError('Los precios cambiaron; consultar nuevamente.')
    target = targets(before['tiers'], rows, before['retail_price'])
    base = {'operation_id': op, 'item_id': item_id, 'before': before['tiers'], 'requested': target}
    previous = changes.reserve(op, request, before['resource'], base)
    if previous is not None:
        return previous
    if target == before['tiers']:
        return changes.finish(op, dict(base, state='unchanged'))
    try:
        current = await read(client, seller, item_id)
        if current['snapshot_hash'] != expected:
            return changes.finish(op, dict(base, state='precondition_failed', sent=False))
    except Exception:
        return changes.finish(op, dict(base, state='precondition_failed', sent=False))
    payload = {'prices': [{'currency_id': 'ARS', 'amount': float(amount(t['amount'])),
               'conditions': {'context_restrictions': CONTEXT, 'min_purchase_unit': t['quantity']}}
              for t in target]}
    try:
        r = await client.request('POST', '/items/' + item_id + '/prices/standard/quantity', json=payload)
        status = r.status_code
    except httpx.RequestError:
        return changes.finish(op, dict(base, state='unknown', retry_allowed=False))
    if not 200 <= status < 300:
        return changes.finish(op, dict(base, state='unknown' if status >= 500 else 'rejected',
                                      http_status=status, retry_allowed=False))
    try:
        after = await read(client, seller, item_id)
        exact = after['tiers'] == target
        preserved = (before['item_fingerprint'] == after['item_fingerprint']
                     and before['other_prices'] == after['other_prices'])
        result = dict(base, state='verified' if exact and preserved else 'verification_mismatch',
                      observed=after['tiers'], other_prices_preserved=preserved, retry_allowed=False)
    except Exception:
        result = dict(base, state='unknown', retry_allowed=False)
    return changes.finish(op, result)


def register(mcp, api, seller, data):
    # Share retail-price locks: aliases and concurrent price edits must not race.
    changes = PriceChanges(data / 'price_changes.sqlite3')

    @mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
    async def nf_mayorista_consultar(item_id: str) -> dict:
        """Lee escalas B2B existentes y precio minorista. No escribe. 401/403: detenerse.
        Sin escalas compatibles no se habilita escritura. Cada color puede tener otro ID.
        """
        return await read(api(), seller, item_id)

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': True,
                          'idempotentHint': True, 'openWorldHint': True})
    async def nf_mayorista_fijar(item_id: str, escalas_json: str, snapshot_hash: str,
                               operation_id: str) -> dict:
        """Solo por orden explícita del titular: actualiza descuentos B2B existentes.
        Leer nf_mayorista_consultar; escalas_json=[{quantity:5,discount_percent:17.18},...].
        Incluir TODAS las cantidades existentes. No crea/elimina escalas, ni cambia público.
        No cambia precio minorista, stock, Ads o promociones. Revisar cada resultado.
        Solo verified confirma. unknown/rejected/mismatch: no reenviar con otro ID.
        En 401/403 detenerse y revisar permisos; no probar otras rutas o credenciales.
        """
        try:
            rows = json.loads(escalas_json)
        except (TypeError, ValueError):
            raise ToolError('escalas_json inválido.') from None
        return await update(changes, api(), seller, item_id, rows, snapshot_hash, operation_id)
