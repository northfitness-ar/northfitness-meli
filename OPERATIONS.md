# Diagnóstico de memoria

## Consultas financieras por operación

La versión `2-release` amplía la salida de `nf_venta_conciliar`, sin cambiar sus
argumentos: expone money_release_date/status/schema y release_timing por pago.
Sólo status approved, refunded=0, money_release_status=released y fecha coherente
habilitan el plazo observado desde la creación de la orden. Pending/held muestran
antigüedad pendiente; fecha programada vencida no demuestra disponibilidad.
La fecha es la informada por MP, no un evento independiente del saldo. Para cerrar
un plazo efectivo de caja, contrastar con movimientos de dinero disponible cuando
haya retenciones o diferencias. Cancelaciones/contracargos/reembolsos se revisan aparte.
Al agregar, deduplicar payment_id, separar monedas, informar cobertura y pendientes;
el promedio de operaciones liberadas no representa automáticamente toda la cohorte.
No calcular pendientes como cero días ni atribuirle el mismo dinero a varias órdenes.

`nf_ventas` agrega `payments` (proyección financiera sin datos del comprador) y
`last_updated`, sin sumar pagos compartidos ni cambiar el criterio de fecha.

`nf_cargos_consultar(order_ids)` verifica la titularidad de 1–20 órdenes y consulta
`GET /billing/integration/group/ML/order/details?order_ids=...&seller_id=...`.
Devuelve evidencia filtrada, identificadores y hash; todavía no clasifica ni suma
automáticamente conceptos. La forma real del desglose debe validarse con la cuenta.
La API de facturación puede tener demora, bonificaciones y cargos compartidos:
`complete=false` y totales `null` son intencionales, incluso ante respuesta vacía.
Fuente: https://global-selling.mercadolibre.com/devsite/en_us/create-application/billing-reports-by-orders-and-packs

`nf_venta_conciliar(order_id)` verifica orden y cuenta MP, consulta los pagos de esa
orden con `GET /v1/payments/{id}` y sus reembolsos con `GET /v1/payments/{id}/refunds`.
Requiere el mismo `MP_ACCESS_TOKEN` que los reportes. No genera ni devuelve dinero.
No devuelve datos de comprador, tarjeta ni credenciales. Sin permiso o con evidencia
inconsistente, falla explícitamente; nunca convierte errores en importes cero.

Controles contables para quien consuma estas lecturas:

- Deduplicar por payment_id, charge_id y refund id, también entre órdenes del mismo pack.
- No sumar `sale_fee`, `fee_details`, `charges_details`, FEE_AMOUNT y MKP_FEE_AMOUNT:
  son representaciones potencialmente superpuestas del mismo cargo.
- No sumar `transaction_amount_refunded` con la lista de refunds.
- Conservar cargo original y monto reintegrado por separado; no presumir bonificación
  total por estado cancelled, ni que mercadería/costo se hayan recuperado.
- Fijo/variable sólo con concepto histórico explícito y reconciliado. Sin evidencia,
  dejar Pendiente; no aplicar retrospectivamente tarifas actuales o porcentajes fijos.
- Estas herramientas no escriben la planilla ni reemplazan sus conciliaciones previas.

Despliegue: `GET /healthz` incorpora `financial_reads_version: "1"`. Esto confirma la
versión publicada, no permisos de facturación ni conciliación financiera. Verificar
las nuevas herramientas con una venta pagada y una cancelada antes de cerrar pendientes.
Si el catálogo MCP conserva el esquema anterior, actualizar la conexión en el cliente.
El acceso sigue sujeto a OAuth/permisos de MELI y a la vigencia del token MP existente.

`GET /healthz` expone un bloque `runtime` sin credenciales, URLs, payloads ni nombres de tareas:

- RSS actual y variación desde la construcción de la aplicación.
- tiempo activo y cantidad total/pendiente de tareas `asyncio`.
- total, fallas, concurrencia actual y pico de solicitudes a Mercado Libre.

Esto permite correlacionar un reinicio de Render con memoria, tareas o concurrencia sin habilitar
logs de autorización. El contador de tareas incluye las tareas internas del servidor y de MCP; no
crece por cada llamada a Mercado Libre.

## Investigación del crecimiento

La ruta de Mercado Libre construía y destruía un `httpx.AsyncClient` completo en cada GET, PUT y
POST. Los ciclos del monitor y de recuperación hacen muchas llamadas paginadas, por lo que también
se recreaban pools, transportes TLS y recursos de conexión repetidamente. Ahora toda la aplicación
comparte un cliente acotado a 20 conexiones (10 keep-alive), y lo cierra durante el apagado del
lifespan. Los encabezados de autorización siguen siendo específicos de cada request y nunca se
guardan en las métricas.

La reproducción local ejecuta 10.000 consultas simuladas en 20 ciclos. Se debe observar una sola
tarea durante todos los ciclos, cero solicitudes activas al terminar y crecimiento acotado luego
de la inicialización del pool. La prueba automatizada agrega además un límite al crecimiento neto
del heap trazado para 1.000 consultas.

## Alcance e incertidumbre

- MCP ya se ejecuta con `stateless_http=True`, por lo que no conserva una sesión en memoria por
  request. Las sesiones privadas del monitor y sus snapshots residen en SQLite, no en una caché RAM.
- No se modificaron los intervalos ni las tareas de atención, reclamos o monitor, ni las reglas de
  cálculo financiero.
- La reproducción usa un transporte HTTP simulado para no enviar credenciales ni depender de APIs
  externas. Antes de considerar cerrado el incidente, observar `runtime` en Render durante un plazo
  mayor a los 12–14 minutos reportados. Si RSS aumenta mientras tareas y solicitudes activas quedan
  estables, tomar un perfil en producción: podría existir retención adicional en FastMCP/Authlib o
  en el allocator nativo que este repositorio no puede reproducir sin el patrón real de tráfico.
