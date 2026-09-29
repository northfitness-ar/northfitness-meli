"""Seller-bound, read-only Full inventory and operation queries."""
import re
from datetime import date, datetime, timedelta, timezone
from fastmcp.exceptions import ToolError

DOC = 'https://developers.mercadolibre.com.ar/envios-fulfillment'
LIMITATION = ('Stock y movimientos provienen de la API Full. La agenda de colectas, '
              'cupos y cantidades planificadas no están integrados por API; las colectas '
              'declaradas se identifican por separado. Un ingreso no confirma por sí solo una colecta.')

async def item_inventory(client, seller, item_id, variation_id=None):
    if not re.fullmatch(r'MLA[0-9]+', item_id or ''):
        raise ToolError('Publicación inválida.')
    item = await client.get('/items/'+item_id)
    if item.get('id') != item_id or str(item.get('seller_id')) != str(seller):
        raise ToolError('Publicación ajena o respuesta inconsistente.')
    variations = item.get('variations') or []
    if variations:
        match = [v for v in variations if str(v.get('id')) == str(variation_id)]
        if len(match) != 1:
            raise ToolError('Elegir variation_id exacto; consultar nf_producto.')
        inv = match[0].get('inventory_id')
    else:
        if variation_id is not None:
            raise ToolError('Esta publicación no tiene esa variante.')
        inv = item.get('inventory_id')
    if not isinstance(inv, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', inv):
        raise ToolError('No hay inventory_id Full verificable para esta variante.')
    return inv

async def stock_detail(client, inventory_id):
    body = await client.get(f'/inventories/{inventory_id}/stock/fulfillment',
                            {'include_attributes':'conditions'})
    if body.get('inventory_id') != inventory_id:
        raise ToolError('Inventario Full inconsistente.')
    for field in ('available_quantity', 'not_available_quantity', 'total'):
        v = body.get(field)
        if (field == 'available_quantity' or v is not None) and (type(v) is not int or v < 0):
            raise ToolError('Cantidades Full inválidas.')
    # Preserve status/condition breakdown; absent fields stay unknown, not zero.
    return {k:body.get(k) for k in ('inventory_id','total','available_quantity',
                                   'not_available_quantity','not_available_detail')}

async def operations_page(client, seller, inventories, since, until, scroll=None, kind=None):
    try:
        a,b = date.fromisoformat(since),date.fromisoformat(until)
    except (ValueError,TypeError):
        raise ToolError('Fechas YYYY-MM-DD requeridas.') from None
    if not 0 < (b-a).days <= 31:
        raise ToolError('Rango de 1 a 31 días; fecha_hasta es exclusiva.')
    if not inventories or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', i) for i in inventories):
        raise ToolError('Inventarios no verificados.')
    if scroll is not None and (not isinstance(scroll,str) or len(scroll)>4096):
        raise ToolError('Cursor inválido.')
    params = {'seller_id':str(seller),'inventory_id':','.join(sorted(set(inventories))),
              'date_from':since,'date_to':until,'limit':100}
    if scroll: params['scroll']=scroll
    if kind: params['type']=kind
    body = await client.get('/stock/fulfillment/operations/search',params)
    rows = body.get('results'); paging = body.get('paging')
    if not isinstance(rows,list) or not isinstance(paging,dict) or 'scroll' not in paging:
        raise ToolError('Respuesta incompleta de movimientos Full.')
    for row in rows:
        if str(row.get('seller_id')) != str(seller) or row.get('inventory_id') not in inventories:
            raise ToolError('Movimiento ajeno o inventario inconsistente.')
    if kind and any(str(row.get('type','')).upper()!=kind.upper() for row in rows):
        raise ToolError('El proveedor no respetó el filtro de operaciones.')
    cursor = paging.get('scroll') or None
    return {'results':rows,'paging':paging,'next_scroll':cursor,'complete':paging['scroll'] is None,
            'warning':'Cursor vacío: consulta incompleta, no sumar como total.' if paging['scroll']=='' else None,
            'date_from':since,'date_to_exclusive':until,
            'source':'Mercado Libre API Full','fetched_at':datetime.now(timezone.utc).isoformat()}


def register(mcp, api, seller, monitor):
    read = {'readOnlyHint':True,'destructiveHint':False,'openWorldHint':True}
    @mcp.tool(annotations=read)
    async def nf_full_consultar(item_id: str, variation_id: str | None = None) -> dict:
        """Lee Full disponible, no disponible y sus condiciones para una variante NF verificada.
        No es stock de depósito ni agenda de colectas. No modifica Mercado Libre.
        """
        client=api(); inv=await item_inventory(client,seller,item_id,variation_id)
        return {'item_id':item_id,'variation_id':variation_id,
                'stock':await stock_detail(client,inv),'limitations':LIMITATION,
                'fetched_at':datetime.now(timezone.utc).isoformat()}

    @mcp.tool(annotations=read)
    async def nf_full_operaciones(item_id: str, fecha_desde: str, fecha_hasta: str,
                                  variation_id: str | None = None, scroll: str | None = None) -> dict:
        """Lee movimientos Full: ingresos, ventas, devoluciones, ajustes y retiros.
        Fechas YYYY-MM-DD, hasta exclusiva, máximo31 días. Una página: repetir mismo
        rango/variante con next_scroll hasta complete=true; cursor vence en5 minutos.
        No equivale a colectas futuras. Sólo variante verificada de NorthFitness.
        """
        client=api(); inv=await item_inventory(client,seller,item_id,variation_id)
        return await operations_page(client,seller,{inv},fecha_desde,fecha_hasta,scroll)

    @mcp.tool(annotations=read)
    async def nf_full_resumen(actualizar: bool = False) -> dict:
        """Cruza Full API con depósito, reservas y colectas declaradas del monitor.
        Incluye procedencia y límites; ninguna reserva local confirma ingreso a Full.
        """
        api()
        return await monitor.stock.read(force=actualizar)
