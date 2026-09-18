"""Validated order reads; intervals are [start, end), compared as instants."""
from datetime import datetime, timedelta, timezone

FIELDS = ('id', 'date_created', 'date_closed', 'status', 'status_detail',
          'pack_id', 'total_amount', 'paid_amount', 'currency_id', 'order_items', 'shipping')


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('La fecha necesita zona horaria.')
    return parsed.astimezone(timezone.utc)


def interval(desde, hasta):
    start, end = instant(desde), instant(hasta)
    if not timedelta(0) < end - start <= timedelta(days=31):
        raise ValueError('Rango mayor a cero y de hasta 31 días.')
    return start, end


async def orders_page(client, seller, desde, hasta, offset=0):
    start, end = interval(desde, hasta)
    if type(offset) is not int or offset < 0 or offset > 9900 or offset % 50:
        raise ValueError('Offset múltiplo de 50 entre 0 y 9900.')
    # Canonical UTC avoids ambiguous provider timezone parsing. Still enforce locally.
    data = await client.get('/orders/search', {
        'seller': seller, 'order.date_created.from': start.isoformat(timespec='milliseconds'),
        'order.date_created.to': end.isoformat(timespec='milliseconds'),
        'offset': offset, 'limit': 50, 'sort': 'date_asc'})
    raw, total = data.get('results'), data.get('paging', {}).get('total')
    if not isinstance(raw, list) or type(total) is not int or total < 0:
        raise ValueError('Paginación inválida; no totalizar.')
    if len(raw) > 50 or (not raw and offset < total) or (raw and offset + len(raw) > total):
        raise ValueError('Página inconsistente; repetir lectura completa.')
    if offset + len(raw) < total and len(raw) != 50:
        raise ValueError('Página intermedia corta; no saltar operaciones.')
    rows, excluded, seen = [], 0, set()
    for row in raw:
        if str(row.get('seller', {}).get('id')) != str(seller):
            raise ValueError('Vendedor inesperado.')
        key = row.get('id')
        if not key or key in seen:
            raise ValueError('Orden sin identificador o duplicada.')
        seen.add(key)
        stamp = instant(row['date_created'])
        if not start <= stamp < end:
            excluded += 1
            continue
        from financial_reads import payment_summary
        safe = {k: row.get(k) for k in FIELDS}
        safe['payments'] = payment_summary(row)
        safe['last_updated'] = row.get('last_updated')
        rows.append(safe)
    complete = offset + len(raw) >= total
    return {'desde': desde, 'hasta': hasta, 'orders': rows, 'reported_total': total,
            'complete': complete, 'next_offset': None if complete else offset + 50,
            'source_rows': len(raw), 'excluded_out_of_range': excluded,
            'interval': '[desde,hasta)', 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'warning': 'Página de órdenes, no utilidad. Total informado por proveedor antes del filtro horario; completar paginación y conciliar devoluciones.'}


async def all_orders(client, seller, desde, hasta):
    rows, seen, offset, expected, excluded = [], set(), 0, None, 0
    while True:
        page = await orders_page(client, seller, desde, hasta, offset)
        if expected is not None and page['reported_total'] != expected:
            raise ValueError('Las ventas cambiaron durante la paginación; repetir consulta.')
        expected = page['reported_total']
        for row in page['orders']:
            if str(row['id']) in seen:
                raise ValueError('Orden repetida entre páginas; repetir consulta.')
            seen.add(str(row['id']))
            rows.append(row)
        excluded += page['excluded_out_of_range']
        if page['complete']:
            return rows, excluded
        offset = page['next_offset']
