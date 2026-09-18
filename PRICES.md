# Cambio explícito de precios

Herramientas: `nf_precio_consultar` y `nf_precio_fijar`.

1. Enumerar todas las publicaciones con `nf_publicaciones` hasta `complete=true`.
2. Identificar el producto y los colores solicitados con `nf_producto`; excluir kits y otros modelos.
3. Consultar cada item con `nf_precio_consultar` y conservar `snapshot_hash`.
4. Solo con autorización explícita de precio y alcance, llamar `nf_precio_fijar`
   con `precio_ars`, `precio_actual_esperado_ars`, hash y un `operation_id` único.
5. Verificar cada item. No presentar una actualización parcial como completa.

El PUT contiene únicamente precio, o IDs y precios de TODAS las variantes clásicas.
Full no necesita cambios de stock. Los colores separados en User Products requieren
enumerar cada item; las publicaciones vinculadas pueden reflejar cambios y deben releerse.
No se descubren ni modifican automáticamente publicaciones fuera del alcance autorizado.

Se bloquean monedas distintas de ARS, propietarios ajenos, variantes de precios distintos
y `original_price` no nulo (posible descuento). Este último control NO certifica ausencia
de cupones o promociones. El resultado valida `item.price`, no el checkout del comprador.
No se elimina la promoción de segunda unidad.

SQLite registra intención antes del envío y reserva el producto entre procesos. Un timeout,
403, 5xx o discrepancia queda bloqueado para conciliación manual: no reenviar con otro ID,
ni borrar registros para forzar un reintento. No existe endpoint genérico ni manejo adicional
de credenciales. La API no proporciona un compare-and-swap atómico en este flujo: evitar
ediciones simultáneas externas durante la operación.

## Despliegue y pruebas

Desplegar `server.py` junto con `price_tools.py`, manteniendo el directorio persistente
`NF_DATA_DIR`. No se requieren variables ni credenciales nuevas. Actualizar el catálogo
del conector tras desplegar. El inicio de la aplicación no modifica precios.

Pruebas locales: `python -m unittest test_prices -v` (HTTP simulado, sin ventas reales).
La ruta de escritura es `PUT /items/{item_id}`. La documentación oficial web no pudo
recuperarse en esta sesión; por ello la compatibilidad del contrato con la cuenta real
queda pendiente de validación en despliegue. No se cambia a otro endpoint si ML lo rechaza.

Antes de declarar completado el encargo: confirmar que ambas herramientas aparecen,
ejecutar únicamente el precio expresamente autorizado y verificar todas las publicaciones.
