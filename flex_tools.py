"""Seller-bound, explicit per-listing Flex deactivation; no subscription changes."""
import hashlib
import json
import re
from datetime import datetime, timezone

import httpx
from fastmcp.exceptions import ToolError
from listing_tools import TitleChanges, _valid_operation_id, snapshot


def flex_state(item):
    shipping = item.get('shipping') or {}
    tags = set(item.get('tags') or []) | set(shipping.get('tags') or [])
    active, inactive = 'self_service_in' in tags, 'self_service_out' in tags
    return active if active != inactive else None


def fingerprint(item):
    value = {k: item.get(k) for k in ('id', 'seller_id', 'status', 'shipping', 'user_product_id')}
    value['flex_active'] = flex_state(item)
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def inspect(client, seller, item_id):
    item = await snapshot(client, seller, item_id)
    active = flex_state(item)
    status = None
    if active is None:
        response = await client.request('GET', '/sites/MLA/shipping/selfservice/items/' + item_id)
        status = response.status_code
        # A generic 403/404 is NOT proof that Flex is disabled.
        if status == 204:
            active = True
    return item, dict(item_id=item_id, title=item['title'], status=item['status'],
                      flex_active=active, snapshot_hash=fingerprint(item),
                      shipping=item.get('shipping'), check_http_status=status,
                      scope='La publicación completa, incluidas sus variantes clásicas.')


class FlexChanges(TitleChanges):
    async def disable(self, client, seller, item_id, snapshot_hash, operation_id, confirmation):
        if confirmation != 'DESACTIVAR_FLEX':
            raise ToolError('Requiere orden explícita del titular y confirmacion=DESACTIVAR_FLEX.')
        _valid_operation_id(operation_id)
        if not re.fullmatch(r'[a-f0-9]{64}', snapshot_hash or ''):
            raise ToolError('Consultar nf_flex_consultar y usar su snapshot_hash.')
        request = json.dumps([str(seller), item_id, snapshot_hash, 'disable'])
        previous = self.previous(operation_id, request)
        if previous is not None:
            return previous
        before, view = await inspect(client, seller, item_id)
        if view['snapshot_hash'] != snapshot_hash:
            raise ToolError('Cambió la publicación. Consultar nuevamente antes de desactivar.')
        if view['flex_active'] is None:
            raise ToolError('No se pudo determinar Flex. No se envió ninguna modificación.')
        base = dict(operation_id=operation_id, item_id=item_id, seller_id=str(seller),
                    before=view['flex_active'], requested=False,
                    timestamp=datetime.now(timezone.utc).isoformat())
        previous = self.reserve(operation_id, request, 'flex:' + str(seller) + ':' + item_id, base)
        if previous is not None:
            return previous
        if view['flex_active'] is False:
            return self.finish(operation_id, dict(base, state='unchanged', observed=False))
        try:
            current, current_view = await inspect(client, seller, item_id)
        except Exception:
            return self.finish(operation_id, dict(base, state='precondition_failed', write_sent=False))
        if fingerprint(current) != snapshot_hash or current_view['flex_active'] is not True:
            return self.finish(operation_id, dict(base, state='precondition_failed', write_sent=False))
        try:
            response = await client.request('DELETE', '/sites/MLA/shipping/selfservice/items/' + item_id)
            status = response.status_code
            state = 'accepted' if 200 <= status < 300 else ('unknown' if status >= 500 or status == 408 else 'rejected')
        except httpx.RequestError:
            status, state = None, 'unknown'
        result = dict(base, state=state, http_status=status)
        if state == 'rejected':
            return self.finish(operation_id, dict(result, warning='Rechazado. Si HTTP 401/403, detenerse y revisar permisos; no insistir.'))
        try:
            after, after_view = await inspect(client, seller, item_id)
            result['observed'] = after_view['flex_active']
            result['full_preserved'] = ((before.get('shipping') or {}).get('logistic_type') != 'fulfillment'
                                        or (after.get('shipping') or {}).get('logistic_type') == 'fulfillment')
            if state == 'accepted':
                result['state'] = ('verified' if after_view['flex_active'] is False and result['full_preserved']
                                   else 'verification_mismatch')
        except Exception:
            result['state'] = 'unknown'
        result['warning'] = 'Sólo verified confirma el cambio. unknown/verification_mismatch: consultar; nunca reenviar con otro ID.'
        return self.finish(operation_id, result)


def register(mcp, api, seller, data):
    changes = FlexChanges(data / 'flex_changes.sqlite3')

    @mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
    async def nf_flex_consultar(item_id: str) -> dict:
        """Consulta Flex por publicación NF y snapshot antes de desactivarlo.
        null significa desconocido, no desactivado. No modifica nada.
        """
        _, view = await inspect(api(), seller, item_id)
        return view

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                          'idempotentHint': True, 'openWorldHint': True})
    async def nf_flex_desactivar(item_id: str, snapshot_hash: str,
                                 operation_id: str, confirmacion: str) -> dict:
        """Desactiva Flex SOLO por orden explícita del titular para esa publicación.
        Leer nf_flex_consultar. confirmacion=DESACTIVAR_FLEX reconoce la orden;
        una orden clara no necesita otra confirmación. Identificar IDs exactos.
        Afecta todas las variantes clásicas del item; colores User Product pueden
        ser items distintos. Si pide varias/todas, enumerar nf_publicaciones hasta
        complete y resolver productos/colores, luego consultar y ejecutar cada ID
        autorizado secuencialmente. No ampliar una selección ambigua; aclararla.
        No modifica Full, stock, precios, Ads, fotos ni suscripción general Flex.
        Sólo verified confirma el cambio; unchanged ya estaba desactivado.
        Reutilizar operation_id. unknown/verification_mismatch exige consultar,
        nunca repetir con otro ID. En 401/403 detener el lote y revisar permisos.
        Informar éxitos, fallas y pendientes por separado; no es atómico.
        """
        return await changes.disable(api(), seller, item_id, snapshot_hash, operation_id, confirmacion)
