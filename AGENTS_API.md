# Agents API: auditor de lectura, piloto de siete sesiones

## Qué cambia

Agrega `nf_agente_diagnostico`, `nf_agente_auditar(fecha)`,
`nf_agente_avanzar(fecha)` y `nf_agente_estado` al MCP autenticado existente.
`/healthz` publica `agents_audit_version: 1-readonly-pilot`.

La sesión usa `/v1/agents/sessions`, `OpenAI-Beta: agents=v1` y un entorno
`none`. Las únicas funciones disponibles son leer una página del snapshot y
consultar evidencia financiera de una orden que pertenece a ese snapshot.
No hay herramientas de escritura comercial, shell, navegador, MCP remoto,
mensajería ni acceso arbitrario a URLs. No se envían claves o datos del comprador
al modelo. Se reutiliza la validación de vendedor existente para pagos y órdenes.

## Configuración en Render

- Conservar `OPENAI_API_KEY` existente sin imprimirla ni trasladarla al repositorio.
- `NF_AGENTS_MODEL`: modelo disponible y compatible con Agents API en ese proyecto.
  La documentación de ejemplo utiliza `gpt-6-astra`; verificar disponibilidad real.
- `NF_AGENTS_ENABLED=true` habilita inicios explícitos y reanudación de sesiones.
  Ausente o false: no se crean sesiones y el worker no las avanza.
- Se mantienen `MP_ACCESS_TOKEN`, la autorización ML de segundo plano y
  `NF_DATA_DIR` del servicio. SQLite debe permanecer en disco persistente.
- La clave necesita `api.agents.read`, `api.agents.write` y
  `api.responses.write`. Un GET exitoso sólo verifica lectura.

No modificar la configuración del agente de atención al cliente. La integración
está apagada por defecto y no crea sesiones durante el despliegue.

## Activación y prueba real

1. Desplegar el commit probado y confirmar la versión en healthz.
2. Actualizar el catálogo MCP y ejecutar `nf_agente_diagnostico`.
3. Configurar modelo y habilitación en Render; no modificar/expandir permisos de
   credenciales sin la autorización que corresponda.
4. Por pedido del titular, ejecutar `nf_agente_auditar` para ayer completo en
   Argentina. `session_id` confirma creación, no finalización.
5. El worker avanza sólo sesiones existentes cada 20 segundos, una consulta
   financiera a la vez. También puede usarse `nf_agente_avanzar` manualmente.
6. `nf_agente_estado` expone el informe cuando el turno está completado y su
   mensaje final fue recuperado con paginación completa. `completed_advisory`
   significa análisis terminado, no utilidad conciliada ni exactitud certificada.
7. Sólo tras la prueba real, adaptar la tarea piloto existente para iniciar una
   sesión por día y consultar resultados. No crear otra tarea periódica.

No hay envíos de correo ni publicación automática a ChatGPT desde Render.
La tarea existente debe recuperar y presentar los resultados. No afirmar que
Agents API reemplazó la tarea hasta verificar esa ruta completa.

## Recuperación, límites y costos

`agents_audit.sqlite3` conserva la reserva por fecha, hash y evidencia inmutable,
sesión, resultados de funciones por call_id, informe y consumo devuelto por OpenAI.
Una fecha nunca crea dos sesiones, incluso tras reinicio. Hay un máximo global
de siete reservas piloto, incluyendo resultados inciertos/rechazados; no se
resetea automáticamente. No usar otra fecha para eludir un resultado incierto.
Una creación con timeout queda `creation_unknown` y necesita revisión en OpenAI;
no se reenvía. Funciones ya leídas reutilizan exactamente su resultado guardado.
Errores dejan `needs_review` y el worker deja de reintentarlos. Revisar la causa
antes de una reanudación manual. Una sesión idle no demuestra éxito.

Máximo 120 lecturas de funciones por sesión, páginas de 20 órdenes, hasta 31 días
de antigüedad y sólo días cerrados. No se generan reportes MP nuevos. Los límites
son operativos, NO un presupuesto monetario garantizado. El modelo/API se factura
por consumo. Usar los controles de gasto del proyecto y evaluar uso real antes
de ampliar el piloto. Deshabilitar el worker no cancela un turno ya ejecutándose
en OpenAI; gestionar la cancelación en el proyecto si fuera necesaria.

## Cobertura inicial y límites

Se audita el snapshot del monitor, preservando null y cobertura pendiente; se
puede profundizar en los pagos/reembolsos mediante las lecturas existentes. El
agente no cambia el snapshot ni marca órdenes conciliadas. No tiene acceso a
Google Sheets, MARGENES en vivo, CSV MP completos o ARCA. La auditoría de ChatGPT
ya programada mantiene el cruce con esas fuentes. No confundir el resultado del
monitor con el resultado del balance mensual (pueden tener diferentes costos,
fijos, vigencias y tratamiento fiscal). Agregar Sheets exige una integración
adicional; no se reutilizan implícitamente las credenciales de ChatGPT.

## Fuentes del contrato

- https://developers.openai.com/api/docs/guides/agents-api/quickstart
- https://developers.openai.com/api/docs/guides/agents-api/tools/functions
- https://developers.openai.com/api/docs/guides/agents-api/sessions
- https://developers.openai.com/api/docs/guides/agents-api/sessions/manage
- https://developers.openai.com/api/docs/guides/agents-api/sessions/events

Pruebas locales con proveedores simulados: no equivalen a acceso real de API.
