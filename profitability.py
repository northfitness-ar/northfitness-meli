"""Decimal-based, explicit-coverage profitability. Unknown is never zero.
Amounts use a cash-inclusive management basis; tax_adjustment is an independently
reconciled signed amount, not an automatic VAT or withholding calculation.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from sales_data import instant
import re

ZERO = Decimal('0')


def amount(value):
    if value is None or isinstance(value, bool):
        raise ValueError('Importe requerido.')
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Importe inválido.') from None
    if not out.is_finite():
        raise ValueError('Importe no finito.')
    return out


def money(value):
    return None if value is None else str(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def validate_policy(policy):
    if not isinstance(policy, dict) or policy.get('currency') != 'ARS':
        raise ValueError('La configuración debe indicar currency=ARS.')
    estimate = policy.get('management_estimate')
    if estimate is not None:
        from datetime import date
        date.fromisoformat(estimate['effective_from'])
        if not estimate.get('source') or estimate.get('refunds') != 'exclude':
            raise ValueError('Estimación requiere fuente y refunds=exclude.')
        if estimate.get('ads', 'include') not in ('include', 'exclude', 'closed_day'):
            raise ValueError('Ads debe ser include, exclude o closed_day.')
        for key in ('check_tax_rate', 'iibb_rate'):
            if not ZERO <= amount(estimate[key]) <= 1:
                raise ValueError('Tasa de estimación inválida.')
    for cost in policy.get('costs', []):
        if not cost.get('sku') or not cost.get('source') or amount(cost['unit_cost']) < 0:
            raise ValueError('Costo requiere SKU, importe no negativo y fuente.')
        instant(cost['effective_from'])
    keys = [(c['sku'], instant(c['effective_from'])) for c in policy.get('costs', [])]
    if len(keys) != len(set(keys)):
        raise ValueError('Costo duplicado para SKU y vigencia.')
    for key, parts in policy.get('kits', {}).items():
        if not parts or any(not p.get('sku') or type(p.get('quantity')) is not int or p['quantity'] <= 0 for p in parts):
            raise ValueError('Kit requiere componentes con cantidades enteras positivas.')
    for key, product in policy.get('products', {}).items():
        if (not re.fullmatch(r'MLA[0-9]+:[0-9]*', key) or not isinstance(product, dict)
                or not isinstance(product.get('name'), str) or not product['name'].strip()
                or len(product['name']) > 200 or not isinstance(product.get('variant'), str)
                or not product['variant'].strip() or len(product['variant']) > 200):
            raise ValueError('Producto requiere clave item_id:variation_id, nombre y variante explícitos.')
    for order_id, facts in policy.get('orders', {}).items():
        if not order_id.isdigit() or not facts.get('source'):
            raise ValueError('Conciliación requiere ID de orden y fuente.')
        for key in ('refund', 'fee', 'logistics', 'tax_adjustment', 'cogs'):
            if key in facts:
                val = amount(facts[key])
                if key in ('refund', 'cogs') and val < 0:
                    raise ValueError('Reintegro y costo deben ser no negativos.')
    for day, facts in policy.get('days', {}).items():
        from datetime import date
        date.fromisoformat(day)
        if not facts.get('source'):
            raise ValueError('Gasto diario requiere fuente.')
        for key in ('ads', 'fixed_costs'):
            if key in facts and amount(facts[key]) < 0:
                raise ValueError('Gasto negativo.')
    return policy


def unit_cost(policy, sku, stamp):
    candidates = [c for c in policy.get('costs', []) if c['sku'] == sku and instant(c['effective_from']) <= stamp]
    if not candidates:
        return None
    return amount(max(candidates, key=lambda c: instant(c['effective_from']))['unit_cost'])


def variant_label(item):
    """Use only explicit marketplace attributes; never infer from similar titles."""
    values = []
    for attribute in item.get('variation_attributes') or item.get('attribute_combinations') or []:
        name = str(attribute.get('name') or attribute.get('id') or '').strip()
        value = str(attribute.get('value_name') or '').strip()
        if name and value:
            values.append(f'{name}: {value}')
    return ' · '.join(values) or 'Sin variante'


def sold_products_add(groups, policy, item, qty, stamp):
    listing_key = str(item['id']) + ':' + str(item.get('variation_id') or '')
    mapped = policy.get('products', {}).get(listing_key)
    is_kit = listing_key in policy.get('kits', {})
    if mapped:
        product, variant = mapped['name'].strip(), mapped['variant'].strip()
        product_key = ('kit:' if is_kit else 'mapped:') + (listing_key if is_kit else product)
    else:
        title = str(item.get('title') or 'Producto sin nombre').strip()
        product = f'{title} ({listing_key})'
        variant = variant_label(item)
        product_key = 'listing:' + listing_key
    parts = policy.get('kits', {}).get(listing_key, [{
        'sku': item.get('seller_sku') or item.get('seller_custom_field'), 'quantity': 1}])
    unit = ZERO
    components = []
    known = True
    for part in parts:
        cost = unit_cost(policy, part['sku'], stamp)
        components.append({'sku': part['sku'], 'quantity': part['quantity'], 'unit_cost': money(cost)})
        if cost is None:
            known = False
        else:
            unit += cost * part['quantity']
    group = groups.setdefault(product_key, {'product': product, 'variants': {}})
    variant_key = listing_key if is_kit else variant
    row = group['variants'].setdefault(variant_key, {'variant': variant, 'units': 0,
        'unit_cost': unit if known else None, 'total_cost': ZERO if known else None,
        'components': components, 'listing_keys': set()})
    # An explicit product/variant map may unify listings only when their computed unit cost agrees.
    if row['unit_cost'] != (unit if known else None):
        row['unit_cost'] = row['total_cost'] = None
    row['units'] += qty
    row['listing_keys'].add(listing_key)
    if row['total_cost'] is not None:
        row['total_cost'] += unit * qty


def sold_products_result(groups):
    products, general_units, general_cost, complete = [], 0, ZERO, True
    for group in groups.values():
        variants, product_units, product_cost, product_complete = [], 0, ZERO, True
        for row in group['variants'].values():
            product_units += row['units']
            if row['total_cost'] is None:
                product_complete = False
            else:
                product_cost += row['total_cost']
            variants.append({**row, 'unit_cost': money(row['unit_cost']),
                'total_cost': money(row['total_cost']), 'listing_keys': sorted(row['listing_keys'])})
        general_units += product_units
        complete = complete and product_complete
        if product_complete:
            general_cost += product_cost
        products.append({'product': group['product'], 'variants': variants, 'units': product_units,
                         'total_cost': money(product_cost) if product_complete else None})
    return products, general_units, money(general_cost) if complete else None


def summarize(orders, policy, day, ads_reported=None):
    """No estimated fee/tax rates. Refunded goods use explicitly reconciled COGS."""
    validate_policy(policy)
    gross = cancelled = sales = known_margin = ZERO
    missing, entries, seen = [], [], set()
    net_orders = ZERO
    complete_orders = 0
    product_groups = {}
    for order in orders:
        oid = str(order['id'])
        if oid in seen:
            raise ValueError('Orden duplicada.')
        seen.add(oid)
        if order.get('currency_id') != 'ARS':
            raise ValueError('Moneda inesperada.')
        status = order.get('status')
        if status not in ('paid', 'cancelled'):
            entries.append({'id': oid, 'status': status, 'revenue': None, 'margin': None, 'missing': ['estado_no_liquidado']})
            missing.append(oid + ':estado_no_liquidado')
            continue
        lines = order.get('order_items')
        if not isinstance(lines, list) or not lines:
            raise ValueError('Orden sin renglones.')
        revenue = fee = cogs = ZERO
        fee_known = cost_known = True
        stamp = instant(order['date_created'])
        for line in lines:
            qty = line.get('quantity')
            if type(qty) is not int or qty <= 0:
                raise ValueError('Cantidad inválida.')
            price = amount(line['unit_price'])
            if price < 0:
                raise ValueError('Precio negativo.')
            revenue += price * qty
            if line.get('sale_fee') is None:
                fee_known = False
            else:
                fee += amount(line['sale_fee']) * qty
            item = line['item']
            sku = item.get('seller_sku') or item.get('seller_custom_field')
            listing_key = str(item['id']) + ':' + str(item.get('variation_id') or '')
            parts = policy.get('kits', {}).get(listing_key, [{'sku': sku, 'quantity': 1}])
            if status == 'paid':
                sold_products_add(product_groups, policy, item, qty, stamp)
            for part in parts:
                cost = unit_cost(policy, part['sku'], stamp)
                if cost is None:
                    cost_known = False
                else:
                    cogs += cost * part['quantity'] * qty
        gross += revenue
        if status == 'cancelled':
            cancelled += revenue
        facts = policy.get('orders', {}).get(oid, {})
        gaps = []
        if status == 'cancelled':
            net_revenue = ZERO
            # A cancelled shipment may still incur non-refunded fees or damaged stock.
            fee_known = 'fee' in facts
            cost_known = 'cogs' in facts
        else:
            refund = amount(facts['refund']) if 'refund' in facts else ZERO
            if refund > revenue:
                raise ValueError('Reintegro supera venta.')
            net_revenue = revenue - refund
            if 'refund' not in facts:
                gaps.append('devoluciones_sin_conciliar')
            if refund > 0 and 'cogs' not in facts:
                cost_known = False
        if 'fee' in facts:
            fee, fee_known = amount(facts['fee']), True
        if 'cogs' in facts:
            cogs, cost_known = amount(facts['cogs']), True
        if not fee_known:
            gaps.append('comision')
        if not cost_known:
            gaps.append('costo')
        logistics = amount(facts['logistics']) if 'logistics' in facts else ZERO
        tax = amount(facts['tax_adjustment']) if 'tax_adjustment' in facts else ZERO
        for key in ('logistics', 'tax_adjustment'):
            if key not in facts:
                gaps.append(key)
        sales += net_revenue
        margin = net_revenue - fee - cogs if fee_known and cost_known else None
        if margin is not None:
            known_margin += margin
        net = net_revenue - fee - cogs - logistics - tax if not gaps else None
        if net is not None:
            net_orders += net
            complete_orders += 1
        missing.extend(oid + ':' + gap for gap in gaps)
        entries.append({'id': oid, 'status': status, 'revenue': money(net_revenue),
                        'fee': money(fee) if fee_known else None, 'cogs': money(cogs) if cost_known else None,
                        'margin': money(margin), 'net': money(net), 'missing': gaps})
    daily = policy.get('days', {}).get(day, {})
    ads = amount(daily['ads']) if 'ads' in daily else ads_reported
    fixed = amount(daily['fixed_costs']) if 'fixed_costs' in daily else None
    if ads is None:
        missing.append('publicidad')
    if fixed is None:
        missing.append('gastos_fijos')
    net = net_orders - ads - fixed if not missing else None
    sold_products, sold_units, merchandise_cost = sold_products_result(product_groups)
    result = {'day': day, 'gross': money(gross), 'cancelled': money(cancelled),
            'sales_after_known_refunds': money(sales), 'ads': money(ads),
            'ads_status': 'conciliado' if 'ads' in daily else 'reportado_provisorio' if ads is not None else 'pendiente',
            'fixed_costs': money(fixed), 'known_product_margin': money(known_margin),
            'net_estimate': money(net), 'orders_count': len(orders), 'complete_orders': complete_orders,
            'coverage_percent': round(100 * complete_orders / len(orders), 1) if orders else 100,
            'missing': missing, 'orders': entries, 'sold_products': sold_products,
            'sold_units': sold_units, 'merchandise_cost': merchandise_cost,
            'status': 'provisorio' if net is not None else 'incompleto',
            'basis': 'ARS con importes de caja; ajuste impositivo conciliado separado. No balance contable ni saldo MP.'}
    estimate = policy.get('management_estimate')
    if estimate and day >= estimate['effective_from']:
        # A separate management scenario, never a fabricated reconciliation.
        check_tax = amount(money(gross * amount(estimate['check_tax_rate'])))
        iibb = amount(money(gross * amount(estimate['iibb_rate'])))
        value = gross - cancelled - check_tax - iibb
        blockers, exclusions = [], ['Devoluciones excluidas por instrucción del titular']
        for entry in entries:
            oid = entry['id']
            if entry['status'] == 'cancelled':
                exclusions.append('Cargos residuales de cancelaciones excluidos del escenario')
                continue
            if entry['status'] != 'paid':
                blockers.append(oid + ':estado_no_liquidado')
                continue
            for key in ('fee', 'cogs'):
                if entry[key] is None:
                    blockers.append(oid + ':' + key)
                else:
                    value -= amount(entry[key])
            facts = policy.get('orders', {}).get(oid, {})
            if 'logistics' in facts:
                value -= amount(facts['logistics'])
            else:
                exclusions.append('Envíos pendientes de conciliación excluidos del escenario')
        expenses = [('gastos_fijos', fixed)]
        ads_mode = estimate.get('ads', 'include')
        ads_included = ads_mode == 'include' or (ads_mode == 'closed_day' and 'ads' in daily)
        if not ads_included:
            exclusions.append('Publicidad excluida por instrucción del titular')
            if ads_mode == 'closed_day':
                exclusions.append('Resultado antes de Ads: pendiente del cierre de las 07:00 del día siguiente (Argentina)')
        else:
            expenses.append(('publicidad', ads))
        for key, expense in expenses:
            if expense is None:
                blockers.append(key)
            else:
                value -= expense
        result['management_estimate'] = {
            'ads_included': ads_included,
            'ads_mode': ads_mode,
            'result': money(value) if not blockers else None,
            'check_tax': money(check_tax), 'iibb': money(iibb),
            'tax_base': money(gross), 'sales': money(gross - cancelled),
            'missing': blockers, 'exclusions': sorted(set(exclusions)),
            'basis': 'Escenario de gestión con impuestos estimados sobre bruto, incluidas cancelaciones. No incluye liquidación de IVA.'}
    return result
