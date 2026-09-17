"""Seller-bound Mercado Pago reports. No transfers or payment mutations."""
import csv
import hashlib
import io
import re
from datetime import date, datetime, time, timedelta, timezone

import httpx
from fastmcp.exceptions import ToolError

BASE = '/v1/account/settlement_report'
LIMIT = 20 * 1024 * 1024


class Reports:
    def __init__(self, token, seller):
        self.token = token
        self.seller = str(seller)

    async def request(self, method, path, payload=None, raw=False):
        if not self.token:
            raise ToolError('Falta MP_ACCESS_TOKEN en el entorno desplegado.')
        try:
            async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
                async with client.stream(method, 'https://api.mercadopago.com' + path,
                                         headers={'Authorization': 'Bearer ' + self.token},
                                         json=payload) as response:
                    if response.status_code == 203:
                        raise ToolError('Mercado Pago no generó el reporte (HTTP 203).')
                    if not 200 <= response.status_code < 300:
                        raise ToolError(f'Mercado Pago devolvió HTTP {response.status_code}; revisar permisos/configuración.')
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > LIMIT:
                            raise ToolError('Reporte supera 20 MB: generar intervalos menores.')
                        chunks.append(chunk)
                    body = b''.join(chunks)
                    if raw:
                        return body
                    import json
                    return json.loads(body) if body else {}
        except (httpx.HTTPError, ValueError) as exc:
            raise ToolError('No se pudo obtener una respuesta válida de Mercado Pago. Consultar estado antes de reintentar una generación.') from None

    async def verify(self):
        me = await self.request('GET', '/users/me')
        if str(me.get('id')) != self.seller:
            raise ToolError('MP_ACCESS_TOKEN no corresponde a MELI_SELLER_ID. Operación bloqueada.')
        return {'account_verified': True, 'seller_id': self.seller}

    async def listing(self):
        result = await self.request('GET', BASE + '/list')
        if not isinstance(result, list) or any(not isinstance(r, dict) for r in result):
            raise ToolError('Formato inesperado en la lista de reportes.')
        if any(str(r.get('user_id', self.seller)) != self.seller for r in result):
            raise ToolError('Reporte asociado a otra cuenta.')
        return result

    async def status(self):
        result = await self.verify()
        config = await self.request('GET', BASE + '/config')
        # Never expose sftp_info, passwords, or full remote configuration.
        result['report_config'] = {k: config.get(k) for k in
                                   ('include_withdraw', 'display_timezone', 'separator', 'header_language')}
        result['warnings'] = []
        if config.get('include_withdraw') is not True:
            result['warnings'].append('La configuración no confirma inclusión de retiros. No considerar completo el flujo de caja.')
        result['transfers_enabled'] = False
        result['scope'] = 'Reportes de operaciones aprobadas; excluyen pendientes y rechazadas. No calculan utilidad.'
        return result


def interval(desde, hasta):
    try:
        start, end = date.fromisoformat(desde), date.fromisoformat(hasta)
        if end < start or (end-start).days > 30:
            raise ValueError()
        tz = timezone(timedelta(hours=-3))
        def utc(d):
            return datetime.combine(d, time(), tz).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return {'begin_date': utc(start), 'end_date': utc(end + timedelta(days=1))}
    except (ValueError, TypeError, OverflowError):
        raise ToolError('Usar fechas YYYY-MM-DD, desde <= hasta, máximo 31 días.') from None


def page(rows, offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ToolError('offset >= 0; limit entre 1 y 100.')
    end = min(offset + limit, len(rows))
    return {'rows': rows[offset:end], 'total_rows': len(rows),
            'next_offset': end if end < len(rows) else None,
            'all_rows_in_this_page': offset == 0 and end == len(rows)}


def parse_report(body):
    try:
        text = body.decode('utf-8-sig')
        if not text.strip() or text.lstrip().startswith(('<', '{', '[')):
            raise ValueError()
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=';,\t')
        reader = csv.reader(io.StringIO(text, newline=''), dialect, strict=True)
        headers = next(reader)
        if not all(headers) or len(set(headers)) != len(headers):
            raise ValueError()
        rows = []
        for values in reader:
            if not values:
                continue
            if len(values) != len(headers):
                raise ValueError()
            rows.append(dict(zip(headers, values)))
        return rows
    except (UnicodeError, csv.Error, ValueError, StopIteration):
        raise ToolError('CSV inválido o formato no reconocido. No se calcularon totales.') from None


def register(mcp, authorize, seller):
    import os
    def client():
        authorize()  # Require existing seller-bound MCP OAuth before static MP token.
        return Reports(os.environ.get('MP_ACCESS_TOKEN', '').strip(), seller)

    read = {'readOnlyHint': True, 'openWorldHint': True}

    @mcp.tool(annotations=read)
    async def nf_mp_estado() -> dict:
        """Verifica cuenta MP y configuración de reportes. No muestra credenciales."""
        return await client().status()

    @mcp.tool(annotations=read)
    async def nf_mp_reportes_listar(offset: int = 0, limit: int = 20) -> dict:
        """Lista reportes de Todas las transacciones. Recorrer next_offset hasta null."""
        c = client()
        await c.verify()
        rows = await c.listing()
        # Return only documented report metadata, never arbitrary remote fields.
        return page([{k: r.get(k) for k in ('id', 'file_name', 'begin_date', 'end_date', 'status', 'date_created')}
                     for r in rows], offset, limit)

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': True})
    async def nf_mp_reporte_crear(desde: str, hasta: str) -> dict:
        """Solicita reporte asíncrono, fechas inclusivas en Argentina, máximo 31 días.
        No mueve dinero. Luego listar para obtener el CSV. Requiere configuración de reportes en MP.
        """
        payload = interval(desde, hasta)
        c = client()
        await c.verify()
        await c.request('POST', BASE, payload)
        return {'requested': True, 'ready': False, 'interval_utc': payload,
                'next_step': 'Consultar nf_mp_reportes_listar; no repetir creación mientras se genera.'}

    @mcp.tool(annotations=read)
    async def nf_mp_reporte_leer(file_name: str, offset: int = 0, limit: int = 50) -> dict:
        """Lee un CSV de esta cuenta en páginas. Recorrer todas antes de sumar.
        Verificar sha256 igual en cada página. Valores originales; no equivalen a utilidad.
        """
        if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,199}\.csv', file_name):
            raise ToolError('Nombre CSV inválido.')
        c = client()
        await c.verify()
        matches = [r for r in await c.listing() if r.get('file_name') == file_name]
        if not matches:
            raise ToolError('El archivo no figura en los reportes de la cuenta.')
        body = await c.request('GET', BASE + '/' + file_name, raw=True)
        result = page(parse_report(body), offset, limit)
        result.update({'file_name': file_name, 'sha256': hashlib.sha256(body).hexdigest(),
                       'warning': 'Sólo operaciones incluidas en este reporte; excluye pendientes y rechazadas. No es ganancia neta.'})
        return result
