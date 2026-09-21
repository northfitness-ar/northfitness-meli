# Reportes de ventas por provincia

El conector genera CSV descargables y resúmenes desde la API autenticada de Mercado
Libre. **No descarga el Excel nativo del panel**: no se verificó un endpoint oficial
para esa exportación. No usa cookies del navegador, rutas internas ni URLs arbitrarias.
El cliente HTTP, OAuth y verificación de vendedor existentes se reutilizan.

## Uso

1. `nf_ventas_reporte_crear(desde="2026-09-01", hasta="2026-09-20")`.
   Fechas inclusivas de Argentina, hasta 31 días completos y hasta ayer.
2. Guardar `report_id` y llamar `nf_ventas_reporte_avanzar(report_id)` hasta
   `complete=true`. Cada llamada descarga 50 órdenes o consulta 20 envíos, con un
   máximo de cinco solicitudes simultáneas. Se puede reanudar después de un reinicio.
3. `nf_ventas_reporte_leer(report_id)` devuelve el resumen completo y hasta 100 filas.
   Para revisar el detalle entero, seguir `next_offset` (límite máximo 200).
4. `nf_ventas_reporte_descargar(report_id, tipo="provincias")` o `tipo="ventas"`.
   Decodificar cada `data_base64` y concatenar los bytes en orden. Seguir `next_offset`
   hasta `download_complete=true`, verificar longitud y SHA256 del archivo completo.
   Guardar el CSV UTF-8 con BOM y separador `;`. No sumar resúmenes repetidos por bloque.

Las cuatro herramientas exigen la sesión OAuth de NorthFitness. La descarga viaja
por MCP autenticado; no crea enlaces públicos. Cada reporte completo es inmutable:
el hash permanece igual entre páginas. Para refrescar ventas o reintentar provincias
no disponibles, crear otro reporte. Retención: 30 días, máximo 100 reportes por cuenta.

## Criterios del informe

- Sólo órdenes con estado `paid` integran el resumen. Canceladas y otros estados
  permanecen en el CSV de auditoría, excluidos de los totales.
- Cantidad de ventas: órdenes distintas (`order_id`), no unidades ni carritos.
  Dos órdenes del mismo pack se cuentan por separado, cada importe una sola vez.
- Importe comercial bruto = suma de precio unitario por cantidad; se contrasta con
  `total_amount`. No se suma `paid_amount`, envío, pagos compartidos ni Ads.
- No es libro de facturas ARCA, facturación neta de notas de crédito, ganancia ni
  base imponible IIBB. Las devoluciones/pagos no aprobados se marcan para revisión;
  no se restan automáticamente porque pueden cubrir packs. `refund_review_count`
  es una advertencia de evidencia de la orden, no una conciliación completa con MP.
- Provincia: `receiver_address.state.name` del envío, con país AR y `sender_id`
  verificado. Es destino logístico, no domicilio fiscal del comprador.
  CABA se mantiene separada de Buenos Aires. No se almacenan nombres, domicilios,
  teléfonos, documentos, títulos de productos ni credenciales en estos reportes.
- Los porcentajes incluyen "Sin provincia verificada" en el denominador. Su importe
  y cantidad se muestran explícitamente. `complete=true` confirma la descarga;
  `summary.geography_complete` confirma si se pudieron asignar todos los destinos.
  Redondeo a dos decimales puede producir una suma de porcentajes distinta de 100.
- Si una página falla, cambia el total, se repite una orden, hay otra moneda o no
  coincide un importe, no se publican totales. Errores de consulta de un envío
  conservan la venta en el grupo sin provincia; nunca se convierten en venta cero.
- Las APIs no proporcionan aquí una transacción atómica de todas las órdenes:
  `created_at` y `updated_at` delimitan la extracción. Para un cierre posterior,
  crear una nueva instantánea y conciliar cambios/reembolsos.

## Despliegue y verificación

Sin dependencias ni variables nuevas. SQLite reside en `NF_DATA_DIR`.
`GET /healthz` incorpora `sales_reports_version: "1"`.
Actualizar el catálogo del conector en ChatGPT después del despliegue si no aparecen
las cuatro herramientas. No requiere nuevos permisos de escritura en Mercado Libre.

Pruebas: `python -m pytest -q test_sales_reports.py` y suite de regresión.
Las pruebas usan un proveedor simulado. Antes de declarar la integración operativa,
completar un reporte real pequeño y comprobar la cobertura provincial con la cuenta.
