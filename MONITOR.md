# Monitor de rentabilidad NorthFitness

Implementación sobre el servidor existente. No requiere publicar los datos ni contratar otro hosting.
El panel se sirve en `/monitor`; el acceso se genera desde ChatGPT con `nf_monitor_abrir`.
El enlace lleva una credencial de un uso en el fragmento (no enviada en la URL HTTP), vence a los
5 minutos y crea una cookie privada de 8 horas. No compartirlo. La ruta HTML no contiene ventas.

## Estado y alcance

Código probado localmente; el despliegue y conciliación contra fuentes reales son etapas separadas.
No hay conexión automática a Google Sheets ni a la liquidación impositiva en esta versión.
No se presenta un número neto si faltan datos. El monitor NO es un balance contable o fiscal.
La clasificación del ajuste impositivo debe validarse con el responsable de la contabilidad.

- Venta por precio efectivamente cobrado y cantidad; no suma `paid_amount` de packs.
- Comisión por unidad multiplicada una sola vez. Comisión ausente queda pendiente.
- Filtro temporal estricto `[desde,hasta)`, comparación UTC y visualización Argentina.
- Paginación completa; rechaza totales cambiantes, páginas truncadas y duplicados.
- Costos versionados por SKU y fecha de vigencia; kits desglosados por publicación/variante.
- Cancelación revierte venta, pero no presupone devolución de comisiones, logística o mercadería.
- Reintegros parciales requieren costo de mercadería conciliado; no se presume recupero de stock.
- Product Ads reportado una sola vez, marcado provisorio; importe conciliado puede sustituirlo.
- Configuración persistente con historial y control de revisión. Snapshots conservan fecha y versión.
- Si falla la consulta se conserva el snapshot anterior y se marca como desactualizado.
- Cada recarga del panel omite la cache de cinco minutos y vuelve a consultar las ventas.
- En modo `closed_day`, Ads se descuenta sólo si existe un importe guardado en `days` para la
  fecha consultada; el valor provisorio informado por la API no cierra el día.

## Puesta en marcha

1. Integrar la rama en la versión ejecutada por el servidor y desplegar con las dependencias existentes.
2. Conservar las variables, secretos, disco persistente y autorización actuales.
3. La consulta bajo demanda funciona con la autorización de fondo existente (`background_authorized`).
   No activa respuestas, reclamos ni movimientos de dinero.
4. Para lectura sin tener el panel abierto: configurar `NF_MONITOR_ENABLED=true` en el servidor.
   Hoy y ayer se revisan por ciclo; también un día anterior rotativo hasta 31 días. Ciclo:
   duración de consultas + 5 minutos. Fuera de esa ventana no hay conciliación automática.
   Una sola instancia/proceso, igual que la arquitectura existente del conector.
5. Actualizar herramientas del complemento si ChatGPT aún no muestra `nf_monitor_*`.
6. Leer `nf_monitor_configuracion`, cargar configuración completa mediante `nf_monitor_configurar`
   con `expected_revision` devuelto. No completar desconocidos con cero.
7. Ejecutar `nf_monitor_resumen` para un día y verificar contra órdenes y liquidación de ML/MP.
8. Ejecutar `nf_monitor_abrir` y abrir el enlace personalmente. Guardar el acceso base al monitor
   en el proyecto NorthFitness; la carpeta no ejecuta el servicio. Tras vencer la sesión,
   pedir un nuevo acceso. No guardar enlaces temporales como acceso permanente.

La carga externa de Ads se ejecuta a las 07:00 de Argentina. El monitor no agrega otro scheduler
para esa carga: consume el importe fechado que ya se haya guardado en la configuración.

## Contrato de configuración

Todos los importes se expresan en ARS mediante cadenas decimales. `currency` debe ser `ARS`.
Se reemplaza la configuración completa, con control de versión e historial: leer antes de editar.

- `costs`: lista `{sku, unit_cost, effective_from, source}`. Costo unitario puesto en depósito,
  con una base consistente con el ajuste impositivo. ISO con zona para la vigencia.
- `kits`: objeto con clave `item_id:variation_id` (variante ausente: `MLA123:`), valor
  lista `{sku, quantity}`. No se calcula con márgenes comerciales históricos fijos.
- `products`: mapeo explícito por `item_id:variation_id` a `{name, variant}`. Sólo este mapeo
  permite unificar publicaciones bajo un producto y variante; nunca se infiere por títulos parecidos.
  Los kits permanecen separados aunque compartan nombre y su costo suma los componentes de `kits`.
- `orders`: objeto por ID de orden **como texto**, con `source` y campos conciliados:
  - `refund`: importe reintegrado de una venta pagada; cero solamente si fue verificado.
  - `fee`: comisión neta **total de la orden**, sustituye `sale_fee × quantity`.
  - `cogs`: costo neto total de mercadería efectivamente consumida/perdida, después de recuperos.
  - `logistics`: costo neto total a cargo del vendedor, evitando duplicar cargos en packs/envíos.
  - `tax_adjustment`: ajuste impositivo firmado sobre la base gerencial con importes de caja.
    No sumar retenciones/percepciones como gasto ni restar otra vez IVA ya depurado en costos.
- `days`: objeto por `YYYY-MM-DD`, con `source`, `ads` conciliado y `fixed_costs` asignado al día.
  Si falta Ads conciliado usa el gasto reportado de Product Ads como provisorio. No incluye Meta Ads.

Resultado completo: ventas menos reintegros, mercadería, comisión, logística, ajuste impositivo,
Product Ads y gastos fijos asignados. Resultado permanece **provisorio**, incluso con cobertura
100%, porque pueden aparecer devoluciones/cargos posteriores. `known_product_margin` abarca
solo renglones calculables y NO representa la utilidad total. `reported_total` de `nf_ventas`
es el total del proveedor antes del filtro horario; usar los pedidos filtrados para comparar cortes.

## Límites pendientes de integración

Para eliminar cargas manuales falta mapear las hojas actuales de costos y gastos, registrar
costos por lote/fecha y conciliar la liquidación real por orden/envío, devoluciones y tributos.
No se supone que un campo genérico «Impuestos» sea IIBB devengado sin verificar su composición.
La lectura por fecha de creación detecta el estado actual del pedido; no ofrece una contabilidad
por fecha del reintegro ni detecta automáticamente devoluciones de ventas de más de 31 días.
No incluye conversión/visitas, caja de Mercado Pago ni comparativo histórico por hora.

## Validación

`python -m pytest -q` verifica intervalos, paginación, importes, recuperos, costos históricos,
kits, campos pendientes, control de revisión, persistencia, cache y acceso privado.
