"""Seller-bound financial evidence. No money movements or inferred fee rates.

Billing is a secondary, eventually consistent source; an empty result is never
proof that a charge was zero. Provider schemas not yet observed are left pending.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from fastmcp.exceptions import ToolError
from mercadopago_reports import Reports

READ = {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': True}
PAYMENT_FIELDS = ('id', 'status', 'status_detail', 'date_created', 'date_approved',
                  'date_last_updated', 'currency_id', 'transaction_amount',
                  'transaction_amount_refunded', 'total_paid_amount', 'shipping_cost',
                  'marketplace_fee', 'coupon_amount', 'installments')


def identifier(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]{0,24}', str(value)):
        raise ToolError('Identificador numérico positivo inválido.')
    return str(value)


def charge_identifier(value):
    if value is None or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(value)):
        raise ToolError('Cargo sin identificador válido; no totalizar.')
    return str(value)


def fields(value, names):
    if not isinstance(value, dict):
        raise ToolError('Formato financiero inesperado; no totalizar.')
    # Only scalar values: never pass buyer, payer, card or arbitrary nested data.
    return {key: value.get(key) for key in names
            if value.get(key) is None or type(value.get(key)) in (str, int, float, bool)}


def payment_summary(order):
    raw = order.get('payments')
    if raw is None:
        return None
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise ToolError('Pagos de la orden con formato inesperado.')
    seen, result = set(), []
    for row in raw:
        key = identifier(row.get('id'))
        if key in seen:
            raise ToolError('Pago duplicado en la orden; no totalizar.')
        seen.add(key)
        result.append(fields(row, PAYMENT_FIELDS))
    return result


async def owned_order(client, seller, order_id):
    key = identifier(order_id)
    order = await client.get('/orders/' + key)
    if (not isinstance(order, dict) or str(order.get('id')) != key or
            not isinstance(order.get('seller'), dict) or
            str(order['seller'].get('id')) != str(seller)):
        raise ToolError('La orden no corresponde al vendedor autorizado.')
    return order


# Financial billing allowlist. Unknown branches are deliberately omitted rather
# than returning arbitrary upstream data. The projection is evidence, not a total.
BILLING_CONTAINERS = frozenset((
    'results', 'details', 'charges', 'bonuses', 'charge_info', 'discount_info',
    'sales_info', 'shipping_info', 'payment_info', 'document_info', 'marketplace_info',
    'tax_info', 'taxes', 'fee_details', 'sale_fee_details', 'amounts', 'items',
    'paging', 'pagination', 'orders', 'order_info', 'charge_details', 'bonus_info'))
BILLING_SCALARS = frozenset((
    'id', 'order_id', 'order_ids', 'pack_id', 'payment_id', 'item_id', 'shipping_id',
    'seller_id', 'user_id', 'charge_id', 'charge_type', 'charge_name', 'charge_detail',
    'charge_detail_description', 'charge_amount', 'charge_date', 'charge_status',
    'charge_type_id', 'charge_type_description', 'detail_id', 'detail_type',
    'detail_amount', 'detail_description', 'detail_date', 'status', 'status_detail',
    'currency_id', 'currency', 'amount', 'original', 'refunded', 'total', 'offset',
    'limit', 'total_amount', 'sale_fee', 'fixed_fee', 'percentage_fee', 'variable_fee',
    'gross_amount', 'net_amount', 'discount_amount', 'discount_reason', 'bonus_amount',
    'bonus_id', 'bonus_date', 'document_id', 'document_type', 'document_number',
    'date_created', 'last_updated', 'quantity', 'unit_price', 'price', 'percentage',
    'financing_fee', 'financing_add_on_fee', 'listing_type_id', 'site_id',
    'transaction_amount', 'transaction_amount_refunded', 'tax_amount', 'tax_type'))


def billing_projection(data):
    omitted = set()
    def walk(value, depth=0):
        if depth > 12:
            raise ToolError('Facturación demasiado anidada; no totalizar.')
        if isinstance(value, list):
            if len(value) > 2000:
                raise ToolError('Respuesta de facturación demasiado grande.')
            return [walk(row, depth + 1) for row in value]
        if not isinstance(value, dict):
            raise ToolError('Formato de facturación no reconocido.')
        out = {}
        for key, item in value.items():
            if key in BILLING_CONTAINERS and isinstance(item, (dict, list)):
                out[key] = walk(item, depth + 1)
            elif key in BILLING_SCALARS and (item is None or type(item) in (str, int, float, bool)):
                out[key] = item
            else:
                omitted.add(key)
        return out
    projected = walk(data)
    # Unknown names only, not values. Do not expose arbitrary upstream key names.
    return projected, len(omitted)


async def billing(client, seller, order_ids):
    if not isinstance(order_ids, list) or not 1 <= len(order_ids) <= 20:
        raise ToolError('Consultar entre 1 y 20 órdenes por llamada.')
    keys = [identifier(key) for key in order_ids]
    if len(set(keys)) != len(keys):
        raise ToolError('Hay órdenes repetidas.')
    # Validate the complete batch before querying billing.
    orders = [await owned_order(client, seller, key) for key in keys]
    data = await client.get('/billing/integration/group/ML/order/details',
                            {'order_ids': ','.join(keys), 'seller_id': str(seller)})
    if not isinstance(data, (dict, list)):
        raise ToolError('Formato de facturación no reconocido.')
    projected, omitted = billing_projection(data)
    def check_owner(value):
        if isinstance(value, list):
            for row in value:
                check_owner(row)
        elif isinstance(value, dict):
            if value.get('seller_id') is not None and str(value['seller_id']) != str(seller):
                raise ToolError('Facturación asociada a otro vendedor.')
            for row in value.values():
                if isinstance(row, (dict, list)):
                    check_owner(row)
    check_owner(projected)
    return {'order_ids': keys, 'pack_ids': [row.get('pack_id') for row in orders],
            'source': 'ML billing/integration/group/ML/order/details',
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'evidence': projected, 'omitted_field_count': omitted,
            'sha256': hashlib.sha256(json.dumps(projected, sort_keys=True, ensure_ascii=False,
                                               allow_nan=False).encode()).hexdigest(),
            'reconciliation_status': 'pending', 'fixed_fee_total': None,
            'variable_fee_total': None, 'complete': False,
            'warnings': ['Evidencia de facturación, no un total conciliado. Puede tener demora o cargos compartidos por pack.',
                         'Identificar cada charge_id antes de sumar; deduplicar entre órdenes, packs y consultas.',
                         'Separar cargo original y bonificación. No sumar otra vez sale_fee ni cargos MP.',
                         'Sin desglose explícito verificado, mantener Pendiente; no inferir una tasa ni interpretar vacío como cero.']}


def payment_view(payment, payment_id, seller):
    if (not isinstance(payment, dict) or str(payment.get('id')) != payment_id or
            str(payment.get('collector_id')) != str(seller)):
        raise ToolError('El pago no corresponde a la operación/cuenta autorizada.')
    result = fields(payment, PAYMENT_FIELDS)
    result['transaction_details'] = fields(payment.get('transaction_details') or {},
                                         ('net_received_amount', 'total_paid_amount',
                                          'overpaid_amount', 'installment_amount'))
    raw = payment.get('fee_details')
    if raw is not None:
        if not isinstance(raw, list):
            raise ToolError('fee_details inválido.')
        result['fee_details'] = [fields(row, ('type', 'amount', 'fee_payer')) for row in raw]
    else:
        result['fee_details'] = None
    raw = payment.get('charges_details')
    if raw is not None:
        if not isinstance(raw, list):
            raise ToolError('charges_details inválido.')
        seen, charges = set(), []
        for row in raw:
            if not isinstance(row, dict):
                raise ToolError('Cargo con formato inesperado.')
            key = charge_identifier(row.get('id'))
            if key in seen:
                raise ToolError('Cargo duplicado; no totalizar.')
            seen.add(key)
            item = fields(row, ('id', 'name', 'type', 'last_updated'))
            item['amounts'] = fields(row.get('amounts') or {}, ('original', 'refunded'))
            item['accounts'] = fields(row.get('accounts') or {}, ('from', 'to'))
            charges.append(item)
        result['charges_details'] = charges
    else:
        result['charges_details'] = None
    return result


async def reconcile_order(client, mp, seller, order_id):
    order = await owned_order(client, seller, order_id)
    summaries = payment_summary(order)
    result = {'order': fields(order, ('id', 'pack_id', 'status', 'date_created', 'date_closed',
                                     'last_updated', 'currency_id', 'total_amount', 'paid_amount')),
              'payments_from_order': summaries, 'payments': None,
              'financial_status': 'pending',
              'fetched_at': datetime.now(timezone.utc).isoformat(),
              'warnings': ['Cancelar una orden no demuestra reintegro de comisiones ni recuperación de mercadería.',
                           'Pagos y cargos pueden cubrir packs: deduplicar por payment_id/charge_id, sin prorrateo automático.',
                           'transaction_amount_refunded y refunds describen el mismo dinero: no sumar ambos.',
                           'fee_details, charges_details, sale_fee y reporte MP se contrastan; no se suman entre sí.']}
    if summaries is None:
        result['warnings'].append('La orden no expone pagos. No interpretar como cero.')
        return result
    if len(summaries) > 20:
        raise ToolError('Más de 20 pagos en una orden; requiere revisión.')
    if not summaries:
        result['payments'] = []
        result['warnings'].append('Sin pagos en la orden; no prueba ausencia de otros ajustes o compensaciones.')
        return result
    await mp.verify()
    payments = []
    for row in summaries:
        key = identifier(row['id'])
        raw = await mp.request('GET', '/v1/payments/' + key)
        payment = payment_view(raw, key, seller)
        refunds = await mp.request('GET', '/v1/payments/' + key + '/refunds')
        if not isinstance(refunds, list) or len(refunds) > 1000:
            raise ToolError('Reembolsos con formato inesperado; no totalizar.')
        seen, safe = set(), []
        for refund in refunds:
            if not isinstance(refund, dict):
                raise ToolError('Reembolso con formato inesperado.')
            rid = identifier(refund.get('id'))
            if rid in seen or str(refund.get('payment_id')) != key:
                raise ToolError('Reembolso repetido o asociado a otro pago.')
            seen.add(rid)
            safe.append(fields(refund, ('id', 'payment_id', 'amount', 'status', 'date_created',
                                        'refund_mode')))
        payment['refunds'] = safe
        payment['refunds_observed_count'] = len(safe)
        # Separate from financial reconciliation: no automatic balance write.
        payment['source'] = 'MP /v1/payments/{id} + /refunds'
        payments.append(payment)
    result['payments'] = payments
    result['financial_status'] = 'evidence_available_pending_reconciliation'
    return result


def register(mcp, authorize, seller):
    @mcp.tool(annotations=READ)
    async def nf_cargos_consultar(order_ids: list[str]) -> dict:
        """Lee facturación histórica de 1–20 órdenes NF verificadas, incluidos cargos y bonificaciones.
        Puede demorar. No es un total: deduplicar charge_id entre packs y conciliar con sale_fee.
        Sin fijo/variable explícito, mantener Pendiente. Nunca estimar por tarifa vigente.
        """
        return await billing(authorize(), seller, order_ids)

    @mcp.tool(annotations=READ)
    async def nf_venta_conciliar(order_id: str) -> dict:
        """Consulta estado, pagos, cargos MP y reembolsos de una venta del vendedor autorizado.
        No mueve dinero. No deduce compensaciones o devolución de stock del estado cancelled.
        Pagos/cargos compartidos requieren deduplicación; no suma fuentes ni modifica el balance.
        """
        client = authorize()
        mp = Reports(os.environ.get('MP_ACCESS_TOKEN', '').strip(), seller)
        return await reconcile_order(client, mp, seller, order_id)
