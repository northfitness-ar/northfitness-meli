"""Explicit, seller-bound Mercado Libre listing-title writes."""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

import httpx
from fastmcp.exceptions import ToolError


def _valid_item_id(value):
    if not re.fullmatch(r"MLA[0-9]+", value or ""):
        raise ToolError("ID de publicación inválido.")


def _valid_operation_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", value or ""):
        raise ToolError("operation_id único de 8 a 100 caracteres.")


def _valid_title(value):
    if not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 60:
        raise ToolError("Título de 1 a 60 caracteres, sin espacios al inicio o final.")
    if re.search(r"[\r\n\t]", value):
        raise ToolError("El título no puede contener saltos de línea ni tabulaciones.")
    return value


def fingerprint(item):
    fields = ("id", "seller_id", "title", "status", "category_id", "domain_id",
              "user_product_id", "catalog_listing", "listing_type_id", "channels")
    stable = {key: item.get(key) for key in fields}
    stable["variation_ids"] = sorted(str(v.get("id")) for v in item.get("variations", []))
    stable["picture_ids"] = [str(p.get("id")) for p in item.get("pictures", [])]
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


async def snapshot(client, seller, item_id):
    _valid_item_id(item_id)
    item = await client.get("/items/" + item_id)
    if item.get("id") != item_id or str(item.get("seller_id")) != str(seller):
        raise ToolError("Publicación ajena a NorthFitness o respuesta inconsistente.")
    if item.get("status") not in ("active", "paused"):
        raise ToolError("La publicación no está activa o pausada.")
    title = item.get("title")
    if not isinstance(title, str) or not title:
        raise ToolError("Mercado Libre devolvió un título inválido.")
    return item


def view(item):
    return {
        "item_id": item.get("id"),
        "title": item.get("title"),
        "status": item.get("status"),
        "category_id": item.get("category_id"),
        "user_product_id": item.get("user_product_id"),
        "catalog_listing": item.get("catalog_listing"),
        "snapshot_hash": fingerprint(item),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "warning": "Cambiar el título no modifica precio, stock, promociones, Ads, fotos ni descripción.",
    }


class TitleChanges:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS title_changes(
                    operation_id TEXT PRIMARY KEY,
                    request TEXT NOT NULL,
                    result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS title_locks(
                    resource TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL
                );
            """)

    def previous(self, operation_id, request):
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT request,result FROM title_changes WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if not row:
            return None
        if row[0] != request:
            raise ToolError("operation_id ya usado para otro cambio.")
        return json.loads(row[1])

    def reserve(self, operation_id, request, resource, base):
        with sqlite3.connect(self.path, timeout=15) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request,result FROM title_changes WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row:
                if row[0] != request:
                    raise ToolError("operation_id ya usado para otro cambio.")
                return json.loads(row[1])
            if connection.execute(
                "SELECT 1 FROM title_locks WHERE resource=?", (resource,)
            ).fetchone():
                raise ToolError("Hay un cambio de título pendiente para esta publicación. Conciliar; no reenviar.")
            connection.execute("INSERT INTO title_locks VALUES (?,?)", (resource, operation_id))
            connection.execute(
                "INSERT INTO title_changes VALUES (?,?,?)",
                (operation_id, request, json.dumps(dict(base, state="unknown"))),
            )
        return None

    def finish(self, operation_id, result):
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE title_changes SET result=? WHERE operation_id=?",
                (json.dumps(result), operation_id),
            )
            if result.get("state") in ("verified", "unchanged", "precondition_failed", "rejected"):
                connection.execute("DELETE FROM title_locks WHERE operation_id=?", (operation_id,))
        return result

    async def set(self, client, seller, item_id, title, expected_title,
                  snapshot_hash, operation_id):
        _valid_item_id(item_id)
        _valid_operation_id(operation_id)
        target = _valid_title(title)
        if not isinstance(expected_title, str) or not expected_title:
            raise ToolError("Título actual esperado inválido.")
        if not re.fullmatch(r"[a-f0-9]{64}", snapshot_hash or ""):
            raise ToolError("Consultar nf_titulo_consultar y usar su snapshot_hash.")

        request = json.dumps(
            [str(seller), item_id, target, expected_title, snapshot_hash],
            ensure_ascii=False,
        )
        old = self.previous(operation_id, request)
        if old is not None:
            return old

        before = await snapshot(client, seller, item_id)
        if before.get("title") != expected_title or fingerprint(before) != snapshot_hash:
            raise ToolError("La publicación cambió: consultar nuevamente antes de escribir.")

        resource = str(seller) + ":" + item_id
        base = {
            "operation_id": operation_id,
            "item_id": item_id,
            "before": expected_title,
            "requested": target,
            "status_before": before.get("status"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        old = self.reserve(operation_id, request, resource, base)
        if old is not None:
            return old
        if target == expected_title:
            return self.finish(operation_id, dict(base, state="unchanged", observed=target))

        try:
            current = await snapshot(client, seller, item_id)
        except Exception:
            return self.finish(operation_id, dict(
                base, state="unknown", warning="Falló la prelectura; conciliar y no reenviar."
            ))
        if fingerprint(current) != snapshot_hash:
            return self.finish(operation_id, dict(
                base, state="precondition_failed", warning="Cambió antes del envío; no se envió PUT."
            ))

        try:
            response = await client.request("PUT", "/items/" + item_id, json={"title": target})
            status = response.status_code
            state = "accepted" if 200 <= status < 300 else ("unknown" if status >= 500 else "rejected")
        except httpx.RequestError:
            status, state = None, "unknown"
        result = dict(base, state=state, http_status=status)
        if state == "rejected":
            result["warning"] = "Mercado Libre rechazó el título. No se modificaron precio, stock, promociones ni Ads."
            return self.finish(operation_id, result)

        try:
            after = await snapshot(client, seller, item_id)
            result["observed"] = after.get("title")
            result["status_observed"] = after.get("status")
            result["other_settings_preserved"] = all(
                before.get(key) == after.get(key)
                for key in ("status", "price", "available_quantity", "listing_type_id",
                            "currency_id", "user_product_id", "channels")
            )
            if state == "accepted":
                result["state"] = (
                    "verified" if after.get("title") == target and result["other_settings_preserved"]
                    else "verification_mismatch"
                )
            result["warning"] = "No modifica precio, stock, promociones, Ads, fotos ni descripción."
        except Exception:
            result["state"] = "unknown"
            result["warning"] = "No se pudo verificar el título. No reenviar; conciliar primero."
        return self.finish(operation_id, result)


def register(mcp, api, seller, data):
    changes = TitleChanges(data / "title_changes.sqlite3")

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    async def nf_titulo_consultar(item_id: str) -> dict:
        """Consulta título y snapshot antes de editar una publicación de NorthFitness."""
        return view(await snapshot(api(), seller, item_id))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                          "idempotentHint": True, "openWorldHint": True})
    async def nf_titulo_fijar(item_id: str, titulo: str, titulo_actual_esperado: str,
                             snapshot_hash: str, operation_id: str) -> dict:
        """Fija el título de una publicación NF solo por orden explícita.
        Leer nf_titulo_consultar primero. Máximo 60 caracteres. No modifica precio,
        stock, promociones, Ads, fotos ni descripción. Reutilizar operation_id;
        unknown/verification_mismatch exigen conciliación y nunca un ID nuevo.
        Solo verified confirma el cambio observado.
        """
        return await changes.set(api(), seller, item_id, titulo, titulo_actual_esperado,
                                 snapshot_hash, operation_id)
