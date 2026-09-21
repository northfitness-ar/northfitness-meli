# Monitor de rentabilidad NorthFitness

Implementación sobre el servidor existente. No requiere publicar los datos ni contratar otro hosting.
El panel se sirve en `/monitor`; el acceso se obtiene desde ChatGPT con `nf_monitor_abrir`.
El enlace privado es permanente y reutilizable. Lleva una credencial derivada del secreto de firma
en el fragmento (no enviada en la URL HTTP) y crea una cookie privada de 8 horas en cada apertura.
No compartirlo. La ruta HTML no contiene ventas. Rotar `JWT_SIGNING_KEY` revoca el enlace.

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
8. Ejecutar `nf_monitor_abrir` una vez, guardar el enlace privado como favorito y abrir siempre ese
   mismo acceso. Si vence la cookie, el enlace vuelve a autorizar el navegador. La carpeta del
   proyecto no ejecuta el servicio. Rotar el secreto de firma invalida el enlace anterior.

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
Incluye visitas a publicaciones del vendedor para intervalos de días completos,
con consulta autenticada a `/users/{seller}/items_visits`. Valida vendedor,
total entero no negativo y ambos límites horarios contra Argentina. Cache de
15 minutos, acotado a dos días de consultas. Errores, permisos insuficientes,
respuesta incompleta o distinto corte dejan visitas y conversión pendientes,
sin interrumpir el cálculo financiero ni reemplazar el dato por cero.

La conversión operativa estimada es órdenes pagadas distintas / visitas × 100;
no son unidades ni visitantes únicos. Su equivalencia exacta con el panel de
Mercado Libre NO está verificada y la interfaz lo indica. No se modifica la
política financiera. Con cero visitas no se divide ni se muestra 0% artificial.
Las comparativas de días equivalentes incluyen visitas, órdenes pagadas y
conversión (variación relativa y diferencia en puntos porcentuales). Para
intervalos intradiarios quedan pendientes: no se mezclan visitas de un día
completo con ventas de unas horas.

No incluye caja de Mercado Pago ni comparativo histórico de visitas por hora.

## Validación

`python -m pytest -q` verifica intervalos, paginación, importes, recuperos, costos históricos,
kits, campos pendientes, control de revisión, persistencia, cache y acceso privado.


## Períodos LIVE y criterios aprobados

- Diario, semana lunes a domingo y mes calendario. Fecha de creación en Argentina; sólo estados pagados/cancelados se totalizan. Una cancelación corrige la fecha original de creación. No se promete igualdad exacta con las estadísticas internas de ML.
- Impuesto al cheque 0,6% e IIBB 2% sobre ventas menos cancelaciones, calculados y redondeados por día. Resultado antes de IVA; sin embalaje.
- `fixed_cost_schedule`: reglas `{effective_from: YYYY-MM-DD, daily_cost, source}`. Los gastos explícitos de `days` prevalecen. Configuración autorizada: septiembre 110312/día; desde octubre 47829.71/día, sin monotributo ni CM. Se cobran días completos transcurridos, incluidos días sin ventas; nunca días futuros.
- Ads usa exclusivamente importes guardados por fecha. La automatización de las 07:00 se conserva. En períodos se descuenta lo cargado y se informa cuántos días faltan.
- La vista intenta consultar cada 30 segundos, sin solicitudes superpuestas por pestaña; el reloj avanza cada segundo. El servidor serializa y comparte lecturas concurrentes del mismo período. Las consultas históricas vuelven a consultar órdenes; nunca se presentan snapshots fallidos como LIVE.
- La comparación diaria/semanal se desplaza 7 días; la mensual 28 días, conservando duración, días de semana y hora de corte. La interfaz muestra ambos intervalos exactos. El mes comparativo puede cruzar límites calendario.
- Productos agrupados exclusivamente por mapeo explícito, kits separados y componentes desplegables. Costos históricos por venta; costo unitario del período es promedio ponderado cuando cambian costos. Un costo faltante mantiene pendientes el subtotal y el total general.
- Órdenes: búsqueda y paginación local de 50 filas, con fecha, estado, artículos, cantidades, ingresos, comisiones, mercadería y margen.
- Logística: conciliaciones manuales prevalecen. Se consulta costo del remitente en `/shipments/{id}/costs`, validando vendedor y composición completa con `/shipments/{id}/items`. Packs completos distribuyen costo proporcional a ventas, con ajuste de centavos para conservar el total. Packs incompletos/mixtos, errores y cargos sin importe verificable quedan pendientes. No se infieren importes por `free_shipping`; cargos generales Full no asignados a envíos requieren conciliación aparte.
- Para limitar carga se consultan hasta 8 envíos por lectura y presupuesto de inicio de 8 segundos, con timeout de 2 segundos por solicitud y caché SQLite de una hora. La interfaz diferencia logística registrada y órdenes pendientes; el resultado estimado sólo descuenta lo registrado. El primer período puede requerir varias actualizaciones para completar envíos. Se conserva el pool HTTP compartido y `/healthz`.

Validación local: pruebas de tasas netas de cancelación, costos por fecha, gastos diarios, Ads parciales, límites semanales/mensuales, fallos con timestamp conservado, distribución de envíos y controles HTML. Antes de producción verificar API de envíos, tiempos y RSS en Render bajo tráfico real; no se dispone de Chromium local para captura visual.
